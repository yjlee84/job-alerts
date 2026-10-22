#!/usr/bin/env python3
"""Local login pipeline: discover, tailor, Easy Apply, archive and notify.

Only a confirmed portal receipt updates Applied. Uncertain attempts never retry.
The private automation.json switch is enabled only after deployment validation.
"""
import argparse
import csv
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime,timezone
from pathlib import Path

from application_store import ApplicationStore,save,now,attention_email,posting_key
from fill_application import ROOT,LOCAL,read_job
from prepare_application import _load_dotenv

def rows(path):
    with path.open(newline='') as f:return list(csv.DictReader(f))

def command(script,*args,job_id=None):
    result=subprocess.run([sys.executable,str(ROOT/'scripts'/script),*args],cwd=ROOT,
                          text=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=900)
    # Browser diagnostics contain personal details: keep them out of launchd logs.
    if job_id:
        directory=LOCAL/'applications'/job_id/'logs';directory.mkdir(parents=True,exist_ok=True)
        (directory/(script+'.log')).write_text(result.stdout)
    if result.returncode:raise RuntimeError(script+' failed'+(' (see private job log)' if job_id else ': '+result.stdout[-600:]))
    return result.stdout

def authorize(job_id):
    path=LOCAL/'profile/applicant.json';profile=json.loads(path.read_text())
    if not profile.get('automation_authorization',{}).get('apply_to_filtered_jobs'):
        raise RuntimeError('Automatic applications have not been authorized')
    profile.setdefault('application_permissions',{})[job_id]={'submit':True,'upload_tailored_resume':True,'privacy_certification':True}
    save(path,profile)

def fill_and_submit(job_id):
    job=read_job(job_id)
    if job.get('source_id')!='oecd_smartrecruiters':raise RuntimeError('Only OECD Easy Apply is supported')
    authorize(job_id)
    if os.environ.get('JOB_ALERTS_CLOUD')=='1':
        from cloud_application import fill_and_submit as cloud_apply
        return cloud_apply(job_id)
    directory=LOCAL/'applications'/job_id
    publication=directory/'publication.json'
    if publication.exists():
        pid=json.loads(publication.read_text())['publication_id']
    else:
        command('safari_application.py','--job-id',job_id,'--open','--no-gpt',job_id=job_id)
        numeric=re.search(r'/OECD/(\d+)',job['job_url'])[1]
        command('safari_worker.py','--job-id',job_id,'--publication-id',numeric,'--discover',job_id=job_id)
        pid=json.loads(publication.read_text())['publication_id']
    common=['--job-id',job_id,'--publication-id',pid]
    command('safari_application.py',*common,'--open','--no-gpt',job_id=job_id)
    command('safari_worker.py',*common,'--prepare-records',job_id=job_id)
    command('safari_application.py',*common,'--fill','--refresh-confirmed',job_id=job_id)
    command('safari_worker.py',*common,'--city',job_id=job_id)
    command('safari_application.py',*common,'--no-gpt',job_id=job_id)
    upload=directory/'safari/resume-upload.json'
    command('safari_resume.py',*common,job_id=job_id)
    observed=json.loads(upload.read_text())['blocked']
    from urllib.parse import urljoin
    endpoints={urljoin('https://jobs.smartrecruiters.com',r.get('url','')) for r in observed
               if r.get('method')=='POST' and r.get('bodyType')=='FormData' and '/attachment/resume?' in r.get('url','')}
    if len(endpoints)!=1:raise RuntimeError('No unique observed resume upload endpoint')
    command('safari_resume.py',*common,'--endpoint',endpoints.pop(),job_id=job_id)
    command('safari_validation.py',*common,'--next','--check-privacy',job_id=job_id)
    command('safari_application.py',*common,'--fill','--refresh-confirmed',job_id=job_id)
    command('safari_validation.py',*common,'--check-privacy',job_id=job_id)
    validation=json.loads((directory/'safari/validation.json').read_text())
    if not validation.get('final_client_valid'):raise RuntimeError('Final form needs an answer or has validation errors')
    command('safari_worker.py',*common,'--probe-submit',job_id=job_id)
    command('safari_worker.py',*common,'--submit',job_id=job_id)

def drain_outbox():
    for path in (LOCAL/'applications').glob('*/submission.json'):
        store=ApplicationStore(path.parent.name)
        receipt=store.directory/'interview/receipt.json'
        if store.state['status'] in {'submitting','uncertain'} and receipt.exists():
            saved=json.loads(receipt.read_text())
            if saved.get('confirmed'):store.confirm(saved)
        if store.state['status']=='confirmed':
            store.update_tracker()
            try:store.notify()
            except Exception as error:print('Application email pending:',path.parent.name,type(error).__name__,flush=True)
    for path in (LOCAL/'applications').glob('*/automation-result.json'):
        outcome=json.loads(path.read_text());store=ApplicationStore(path.parent.name)
        if store.state['status']=='confirmed':continue
        try:attention_email(path.parent.name,outcome.get('title',path.parent.name),outcome['reason'],path.parent)
        except Exception as error:print('Attention email pending:',path.parent.name,type(error).__name__,flush=True)

def archive_descriptions():
    for row in rows(ROOT/'reports/job_tracker.csv'):
        job_id=row.get('Job ID','')
        if not re.fullmatch(r'[a-zA-Z0-9_-]+',job_id):continue
        path=LOCAL/'applications'/job_id/'discovered-job.json'
        if not path.exists():save(path,{'saved_at':now(),'tracker':row})
        # Keep the familiar project outputs authoritative and easy to browse.
        output=ROOT/'outputs'/job_id;output.mkdir(parents=True,exist_ok=True)
        description=output/'job_description.txt'
        if row.get('Description') and not description.exists():description.write_text(row['Description']+'\n')

def run(force=False,existing_only=False):
    _load_dotenv(ROOT/'.env')
    config_path=LOCAL/'automation.json'
    config=json.loads(config_path.read_text()) if config_path.exists() else {}
    if not config.get('enabled'):
        print('Automation is not enabled; no applications attempted.',flush=True);return
    drain_outbox()
    status_path=LOCAL/'automation-status.json'
    status=json.loads(status_path.read_text()) if status_path.exists() else {}
    last=status.get('last_scan_at')
    if last and not force and time.time()-datetime.fromisoformat(last).timestamp()<config.get('minimum_interval_seconds',0):
        print('Scan interval has not elapsed.',flush=True);return
    archive_descriptions()
    if not existing_only:
        before=len(rows(ROOT/'data/fetch_runs.csv'))
        command('fetch_jobs.py')
        fresh=rows(ROOT/'data/fetch_runs.csv')[before:]
        if not fresh or any(r['status']!='ok' for r in fresh):
            raise RuntimeError('Discovery failed or offline; preserving tracker and retrying next scheduled run')
        command('filter_jobs.py');command('sync_job_tracker.py')
    archive_descriptions()
    outcomes=[]
    listings={r['job_id'] for r in rows(ROOT/'data/job_listings.csv')}
    # Permission entries are generated per job. Their growth must not cause
    # unrelated blocked forms to be retried as if applicant facts had changed.
    profile=json.loads((LOCAL/'profile/applicant.json').read_text());profile.pop('application_permissions',None)
    profile_hash=hashlib.sha256(json.dumps(profile,sort_keys=True).encode()).hexdigest()
    tracker=rows(ROOT/'reports/job_tracker.csv')
    closed_postings={posting_key(r.get('Website')) for r in tracker if r.get('Status','').strip().lower() not in {'','review'}}
    for row in tracker:
        job_id=row['Job ID']
        if row['Status'].strip().lower()!='review' or job_id not in listings:continue
        if posting_key(row['Website']) in closed_postings:
            outcomes.append({'job_id':job_id,'status':'duplicate','note':'Posting already applied to or closed under another tracker ID'})
            continue
        store=ApplicationStore(job_id)
        if store.state['status']!='unsubmitted':
            outcomes.append({'job_id':job_id,'status':store.state['status'],'note':'Existing attempt; never resubmit automatically'})
            continue
        previous=store.directory/'automation-result.json'
        if previous.exists() and not force:
            old=json.loads(previous.read_text())
            if old.get('profile_hash')==profile_hash:
                outcomes.append(old);continue
        try:
            output=ROOT/'outputs'/job_id
            if not (output/'resume.pdf').exists() or not (output/'cover_letter.pdf').exists():
                command('prepare_application.py','--job-id',job_id,job_id=job_id)
            fill_and_submit(job_id)
            outcomes.append({'job_id':job_id,'status':'confirmed'})
        except Exception as error:
            state=ApplicationStore(job_id).state['status']
            outcome={'job_id':job_id,'title':row['Position'],'status':state if state!='unsubmitted' else 'needs_attention',
                     'reason':str(error),'at':now(),'profile_hash':profile_hash}
            save(LOCAL/'applications'/job_id/'automation-result.json',outcome)
            outcomes.append(outcome)
            print('Application needs attention:',job_id,str(error),flush=True)
    save(status_path,{'last_scan_at':now(),'outcomes':outcomes})
    drain_outbox()
    print(json.dumps({'completed_at':now(),'outcomes':outcomes}),flush=True)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--force',action='store_true',help='Retry pre-submission jobs that previously needed attention')
    parser.add_argument('--existing-only',action='store_true',help='Use current filtered listings without fetching')
    args=parser.parse_args()
    LOCAL.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (LOCAL/'automation.lock').open('w') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another automation run is active.');sys.exit(0)
        run(args.force,args.existing_only)
