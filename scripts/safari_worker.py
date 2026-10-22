#!/usr/bin/env python3
"""OECD Safari application worker. Submission requires durable local authorization."""
import argparse
import json
import subprocess
import time
import fcntl
import re
from urllib.parse import urljoin,urlparse
from pathlib import Path
from safari_application import evaluate, SAFARI_GUARD
from fill_application import LOCAL,ROOT,SCAN_FIELDS,SNAPSHOT,read_job
from application_records import ensure_employment,repair_education,select_autocomplete
from application_store import ApplicationStore,save,now

def final_control(mode,endpoint=None):
    return r'''() => {
      if(window.__applicationReviewGuard!==4)throw Error('Current submission guard required');
      const matches=[...document.querySelectorAll('spl-button,button')].filter(e=>e.textContent.trim()==='Submit');
      if(matches.length!==1||matches[0].disabled||matches[0].hasAttribute('disabled'))throw Error('No unique enabled Submit');
      window.__reviewBlockedRequests=[];window.__workerNetworkResponses=[];
      window.__workerSubmission={mode:MODE,endpoint:ENDPOINT,expires:Date.now()+30000};
      matches[0].click();return true;
    }'''.replace('MODE',json.dumps(mode)).replace('ENDPOINT',json.dumps(endpoint))

def probe_submit(pid,job_id):
    from safari_validation import inspect
    live=inspect(pid)
    if '/screening' not in live['url'] or not live['client_valid']:
        raise RuntimeError('Live final form is not valid')
    evaluate(final_control('probe'),pid)
    time.sleep(2)
    observed=evaluate('() => {window.__workerSubmission=null;return window.__reviewBlockedRequests}',pid)
    expected='https://jobs.smartrecruiters.com/oneclick-ui/api/company/OECD/publication/'+pid+'/application'
    urls={urljoin('https://jobs.smartrecruiters.com',r['url']) for r in observed
          if r.get('method','').upper()=='POST' and urljoin('https://jobs.smartrecruiters.com',r['url'])==expected}
    if len(urls)!=1:raise RuntimeError('Expected one blocked submission endpoint; observed '+json.dumps(observed))
    endpoint=urls.pop()
    parsed=urlparse(endpoint)
    if parsed.scheme!='https' or parsed.netloc!='jobs.smartrecruiters.com' or not parsed.path.startswith('/oneclick-ui/api/') or '/attachment/' in parsed.path:
        raise RuntimeError('Unsupported application submission endpoint')
    save(LOCAL/'applications'/job_id/'safari/submit-endpoint.json',{'endpoint':endpoint,'observed_at':now(),'blocked_requests':observed})
    return {'endpoint':endpoint,'submitted':False}

def submit(pid,job_id):
    from safari_validation import inspect
    from prepare_application import _load_dotenv
    job=read_job(job_id)
    profile=json.loads((LOCAL/'profile/applicant.json').read_text())
    permission=profile.get('application_permissions',{}).get(job_id,{})
    if not permission.get('submit'):raise RuntimeError('Submission has not been authorized')
    dest=LOCAL/'applications'/job_id/'safari'
    validation=json.loads((dest/'validation.json').read_text())
    live=inspect(pid)
    if not validation.get('final_client_valid') or not live['client_valid'] or '/screening' not in live['url']:
        raise RuntimeError('Live final validation failed')
    import hashlib
    upload=json.loads((dest/'resume-upload.json').read_text())
    if not upload.get('upload_verified') or upload.get('sha256')!=hashlib.sha256((ROOT/'outputs'/job_id/'resume.pdf').read_bytes()).hexdigest():
        raise RuntimeError('Current tailored resume upload is not verified')
    first=json.loads((dest/'review-profile.json').read_text())
    message=[f for f in first['fields'] if 'let the company know' in f['label'].lower()]
    if len(message)!=1 or not message[0]['value'].endswith(profile['writing']['signature']) or '\n\n______________' in message[0]['value']:
        raise RuntimeError('Hiring-team message or signature is incomplete')
    for filename,key,status in [('record-repairs.json','employment','Saved and verified confirmed dates'),('education-repairs.json','education','Saved and verified degree record')]:
        records=json.loads((dest/filename).read_text())
        if len(records)!=len(profile[key]) or any(r['status']!=status for r in records):raise RuntimeError('Record verification incomplete')
    # Refresh the final evidence immediately before making the durable intent.
    save(dest/'submission-fields.json',live)
    (dest/'submission-form.html').write_text(evaluate(SNAPSHOT,pid))
    endpoint=json.loads((dest/'submit-endpoint.json').read_text())['endpoint']
    if endpoint!='https://jobs.smartrecruiters.com/oneclick-ui/api/company/OECD/publication/'+pid+'/application':
        raise RuntimeError('Submission endpoint does not match the selected job')
    store=ApplicationStore(job_id)
    store.begin(job,validation)
    try:
        evaluate(final_control('submit',endpoint),pid)
        for _ in range(20):
            time.sleep(1)
            evidence=evaluate('() => ({url:location.href,text:document.body.innerText,responses:window.__workerNetworkResponses||[],blocked:window.__reviewBlockedRequests||[]})',pid)
            save(store.directory/'submission-observation.json',evidence)
            # Both the actual request and a positive portal message are needed.
            positive=re.search(r'(?:your application (?:has been|was) (?:successfully )?(?:submitted|sent)|thank you for (?:your application|applying)|application (?:successfully )?submitted)',evidence['text'],re.I)
            successful=any(200<=r.get('status',0)<300 and urljoin('https://jobs.smartrecruiters.com',r['url'])==endpoint for r in evidence['responses'])
            if positive and successful:
                store.confirm({'confirmed':True,'observed_at':now(),'url':evidence['url'],
                               'message':positive.group(0),'responses':evidence['responses']})
                _load_dotenv(ROOT/'.env')
                store.notify()
                return {'submitted':True,'confirmed':True,'archive':store.state['archive']}
        raise RuntimeError('No confirmed portal receipt within 20 seconds')
    except Exception as error:
        if store.state['status']!='confirmed':store.uncertain(str(error))
        raise

def prepare_records(pid,job_id):
    profile=json.loads((LOCAL/'profile/applicant.json').read_text())
    evaluate(SAFARI_GUARD,pid)
    # Discard only an empty diagnostic editor; never discard entered records.
    evaluate(r'''() => {
      for(const form of document.querySelectorAll('oc-education-edit-form')){
        if(!form.querySelector('oc-institution-autocomplete spl-autocomplete')?.value)
          form.querySelector('[data-test=education-cancel] spl-button')?.click();
      }return true;
    }''',pid)
    employment=ensure_employment(profile,evaluate,pid)
    education=repair_education(profile,evaluate,pid)
    dest=LOCAL/'applications'/job_id/'safari'
    (dest/'record-repairs.json').write_text(json.dumps(employment,indent=2))
    (dest/'education-repairs.json').write_text(json.dumps(education,indent=2))
    if len(education)!=len(profile['education']) or any(r['status']!='Saved and verified degree record' for r in education):
        raise RuntimeError('Degree verification failed')
    return {'employment':len(employment),'education':len(education),'verified':True}

def city(pid):
    profile=json.loads((LOCAL/'profile/applicant.json').read_text())
    # Paris, France is explicitly provided. Select only one exact location.
    select_autocomplete(evaluate,pid,'oc-personal-information','oc-location-autocomplete spl-autocomplete',
                        profile['personal']['city']+', '+profile['personal']['country'],location=profile['personal'])
    result=evaluate('() => document.querySelector("oc-personal-information oc-location-autocomplete spl-autocomplete").value',pid)
    if not isinstance(result,dict) or result.get('city')!=profile['personal']['city'] or result.get('country')!=profile['personal']['country']:
        raise RuntimeError('Residence selection did not commit correctly')
    return result

def inspect_editors(pid):
    evaluate(SAFARI_GUARD,pid)
    return evaluate(r'''() => {
      for(const [tag,test] of [['oc-experience-edit-form','add-experience'],['oc-education-edit-form','add-education']]){
        if(!document.querySelector(tag))document.querySelector('[data-test="'+test+'"] spl-button')?.click();
      }
      return [...document.querySelectorAll('oc-experience-edit-form,oc-education-edit-form')].map(f=>({tag:f.tagName,html:f.outerHTML}));
    }''',pid)

def discover(numeric_id,job_id):
    links=evaluate(r'''() => [...document.querySelectorAll('a[href]')].map(a=>a.href).filter(h=>h.includes('/oneclick-ui/company/OECD/publication/'))''',numeric_id)
    matches={url for url in links if re.fullmatch(r'https://jobs\.smartrecruiters\.com/oneclick-ui/company/OECD/publication/[a-f0-9-]{36}(?:\?[^#]*)?',url)}
    if len(matches)!=1:raise RuntimeError('No unique Easy Apply URL on this job listing')
    url=matches.pop();pid=re.search('/publication/([a-f0-9-]{36})',url)[1]
    save(LOCAL/'applications'/job_id/'publication.json',{'publication_id':pid,'url':url})
    return {'publication_id':pid,'url':url}

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--publication-id')
    parser.add_argument('--inspect-editors',action='store_true')
    parser.add_argument('--tabs',action='store_true')
    parser.add_argument('--prepare-records',action='store_true')
    parser.add_argument('--city',action='store_true')
    parser.add_argument('--job-id')
    parser.add_argument('--reload',action='store_true')
    parser.add_argument('--probe-submit',action='store_true')
    parser.add_argument('--submit',action='store_true')
    parser.add_argument('--inspect-location',action='store_true')
    parser.add_argument('--discover',action='store_true')
    args=parser.parse_args()
    if args.reload:
        evaluate('() => {location.reload();return true}',args.publication_id)
        time.sleep(4)
    if args.inspect_editors:
        print(json.dumps(inspect_editors(args.publication_id),indent=2))
    if args.prepare_records:
        print(json.dumps(prepare_records(args.publication_id,args.job_id),indent=2))
    if args.city:
        city(args.publication_id)
    if args.inspect_location:
        print(json.dumps(evaluate(r'''() => {
          const root=document.querySelector('oc-personal-information oc-location-autocomplete'),all=[];
          const walk=r=>{for(const e of r.querySelectorAll('*')){if(e.matches('input,[role=option]'))all.push({tag:e.tagName,text:e.textContent,host:e.getRootNode().host?.outerHTML,hostText:e.getRootNode().host?.textContent,rects:e.getClientRects().length,value:e.value,html:e.outerHTML});if(e.shadowRoot)walk(e.shadowRoot)}};
          walk(root);return {all,selectedHost:window.__workerAutocomplete?.outerHTML};
        }''',args.publication_id),indent=2))
    if args.probe_submit:
        print(json.dumps(probe_submit(args.publication_id,args.job_id),indent=2))
    if args.discover:
        print(json.dumps(discover(args.publication_id,args.job_id),indent=2))
    if args.submit:
        lockdir=LOCAL/'locks';lockdir.mkdir(parents=True,exist_ok=True)
        with (lockdir/(args.job_id+'.lock')).open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            print(json.dumps(submit(args.publication_id,args.job_id),indent=2))
    if args.tabs:
        print(subprocess.run(['osascript','-'],input='tell application "Safari"\nset links to {}\nrepeat with w in windows\nrepeat with t in tabs of w\nset end of links to URL of t\nend repeat\nend repeat\nreturn links\nend tell',text=True,capture_output=True,check=True).stdout)
