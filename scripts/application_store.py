"""Durable submission intent, interview archives, tracker updates and email outbox.

Callers hold the pipeline/job lock. Any intent without a confirmed receipt
requires reconciliation and can NEVER be automatically resubmitted.
"""
import csv
import hashlib
import json
import os
import re
import shutil
import smtplib
import tempfile
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
LOCAL=ROOT/'.local'

def posting_key(url):
    from urllib.parse import urlparse
    parsed=urlparse(url or '')
    match=re.match(r'^/([^/]+)/(\d+)(?:-|/|$)',parsed.path)
    if parsed.hostname=='jobs.smartrecruiters.com' and match:
        return parsed.hostname+'/'+match[1].lower()+'/'+match[2]
    return (parsed.hostname or '')+parsed.path.rstrip('/')

def now():
    return datetime.now(timezone.utc).isoformat()

def atomic(path,content):
    path.parent.mkdir(parents=True,exist_ok=True)
    fd,tmp=tempfile.mkstemp(dir=path.parent,prefix='.'+path.name)
    try:
        with os.fdopen(fd,'w') as f:
            f.write(content);f.flush();os.fsync(f.fileno())
        os.replace(tmp,path)
        directory=os.open(path.parent,os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def save(path,value):
    atomic(path,json.dumps(value,ensure_ascii=False,indent=2)+'\n')

def attention_email(job_id,title,reason,directory):
    """Deduplicated exception notice, distinct from a confirmed application."""
    fingerprint=hashlib.sha256(reason.encode()).hexdigest()
    path=directory/'attention-email.json'
    state=json.loads(path.read_text()) if path.exists() else {}
    if state.get('fingerprint')==fingerprint and state.get('sent'):return
    save(path,{'fingerprint':fingerprint,'sent':False,'reason':reason})
    username=os.environ.get('JOB_ALERTS_SMTP_USERNAME','');password=os.environ.get('JOB_ALERTS_SMTP_PASSWORD','')
    if not username or not password:raise RuntimeError('SMTP credentials missing')
    message=EmailMessage();message['Subject']='Application needs attention: '+title
    message['From']=os.environ.get('JOB_ALERTS_EMAIL_FROM') or username
    message['To']=os.environ.get('JOB_ALERTS_EMAIL_TO') or 'iyongjae6@gmail.com'
    message['Message-ID']='<job-attention-'+job_id+'-'+fingerprint[:12]+'@job-alerts.local>'
    message.set_content('An application needs attention. This is not an application confirmation.\n\n'+title+
                        '\n'+reason+'\n\nSaved details: '+str(directory)+'\n')
    with smtplib.SMTP(os.environ.get('JOB_ALERTS_SMTP_HOST') or 'smtp.gmail.com',int(os.environ.get('JOB_ALERTS_SMTP_PORT') or 587),timeout=30) as smtp:
        smtp.ehlo()
        if os.environ.get('JOB_ALERTS_SMTP_STARTTLS','true').lower() not in {'false','0','no'}:smtp.starttls();smtp.ehlo()
        smtp.login(username,password);smtp.send_message(message)
    save(path,{'fingerprint':fingerprint,'sent':True,'sent_at':now(),'reason':reason})

class ApplicationStore:
    def __init__(self,job_id,root=ROOT):
        if not re.fullmatch(r'[a-zA-Z0-9_-]+',job_id):raise ValueError('Invalid job ID')
        self.root=Path(root);self.job_id=job_id
        self.directory=self.root/'.local/applications'/job_id
        self.path=self.directory/'submission.json'
        self.state=json.loads(self.path.read_text()) if self.path.exists() else {'status':'unsubmitted'}

    def begin(self,job,validation):
        if self.state['status']!='unsubmitted':
            raise RuntimeError('Submission already attempted; reconcile the existing receipt before any retry')
        if not validation.get('final_client_valid'):
            raise ValueError('Live final validation is required')
        key=posting_key(job['job_url'])
        for path in (self.root/'.local/applications').glob('*/submission.json'):
            other=json.loads(path.read_text())
            if path!=self.path and other.get('status')!='unsubmitted' and posting_key(other.get('job_url'))==key:
                raise RuntimeError('This posting already has a submission attempt under another listing ID')
        with (self.root/'reports/job_tracker.csv').open(newline='') as f:
            for row in csv.DictReader(f):
                if row.get('Job ID')!=self.job_id and row.get('Status','').strip().lower() not in {'','review'} and posting_key(row.get('Website'))==key:
                    raise RuntimeError('This posting is already applied to or closed under another tracker ID')
        archive=self.directory/'interview'
        archive.mkdir(parents=True,exist_ok=True)
        save(archive/'job.json',job)
        atomic(archive/'job_description.txt',job['description'])
        source=self.root/'outputs'/self.job_id
        for file in source.rglob('*'):
            if file.is_file():
                target=archive/'materials'/file.relative_to(source)
                target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,target)
        safari=self.directory/'safari'
        if safari.exists():shutil.copytree(safari,archive/'answers',dirs_exist_ok=True)
        resume=archive/'materials/resume.pdf'
        if not resume.exists():raise ValueError('Tailored resume missing from archive')
        self.state={'status':'submitting','job_id':self.job_id,'started_at':now(),
                    'job_title':job['job_title'],'job_url':job['job_url'],
                    'archive':str(archive),'resume_sha256':hashlib.sha256(resume.read_bytes()).hexdigest()}
        save(self.path,self.state)  # Must reach disk BEFORE the first submit click.
        from cloud_state import checkpoint
        checkpoint() # Cloud intent must be acknowledged remotely before Submit.

    def uncertain(self,reason):
        self.state.update(status='uncertain',reason=reason,updated_at=now())
        save(self.path,self.state)
        from cloud_state import checkpoint
        checkpoint()

    def confirm(self,receipt):
        if self.state['status'] not in {'submitting','uncertain'}:raise ValueError('No durable submission intent')
        if not receipt.get('confirmed'):raise ValueError('Portal confirmation required')
        save(self.directory/'interview/receipt.json',receipt)
        self.state.update(status='confirmed',confirmed_at=receipt.get('observed_at') or now(),receipt=receipt,notification_sent=False)
        save(self.path,self.state)
        self.update_tracker()
        from cloud_state import checkpoint
        checkpoint()

    def update_tracker(self):
        if self.state['status']!='confirmed':raise ValueError('Cannot mark an unconfirmed application Applied')
        path=self.root/'reports/job_tracker.csv'
        with path.open(newline='') as f:
            reader=csv.DictReader(f);fields=reader.fieldnames;rows=list(reader)
        matches=[r for r in rows if r['Job ID']==self.job_id]
        if len(matches)!=1:raise RuntimeError('Tracker job is missing or ambiguous; receipt is preserved')
        row=matches[0]
        # Once reconciled, preserve the user's later Interview/Rejected status,
        # follow-up notes and dates instead of overwriting them each login.
        if row.get('Status','').strip().lower() not in {'','review'}:
            if row.get('Application Date'):return
            row['Application Date']=self.state['confirmed_at'][:10]
        else:
            row.update({'Status':'Applied','Application Date':self.state['confirmed_at'][:10],
                        'Next Action':'Await response; interview archive: '+self.state['archive']})
        import io
        buffer=io.StringIO();writer=csv.DictWriter(buffer,fieldnames=fields);writer.writeheader();writer.writerows(rows)
        atomic(path,buffer.getvalue())

    def notify(self):
        if self.state['status']!='confirmed' or self.state.get('notification_sent'):return False
        if os.environ.get('JOB_ALERTS_CONFIRMATION_EMAILS','0').lower() not in {'1','true','yes'}:
            if self.state.get('notification_status')!='disabled':
                self.state['notification_status']='disabled';save(self.path,self.state)
                from cloud_state import checkpoint
                checkpoint()
            return False
        # A failed email leaves an outbox item, not a reason to submit again.
        username=os.environ.get('JOB_ALERTS_SMTP_USERNAME','')
        password=os.environ.get('JOB_ALERTS_SMTP_PASSWORD','')
        sender=os.environ.get('JOB_ALERTS_EMAIL_FROM') or username
        recipient=os.environ.get('JOB_ALERTS_EMAIL_TO') or 'iyongjae6@gmail.com'
        if not username or not password or not sender:raise RuntimeError('Application email SMTP credentials missing')
        message=EmailMessage()
        message['Subject']='Application submitted: '+self.state['job_title']
        message['From']=sender;message['To']=recipient
        message['Message-ID']='<job-application-'+self.job_id+'@job-alerts.local>'
        repository=os.environ.get('GITHUB_REPOSITORY')
        documents=('https://github.com/'+repository+'/tree/main/outputs/'+self.job_id+'/' if repository else 'outputs/'+self.job_id+'/')
        message.set_content('The job portal confirmed your application.\n\n'+self.state['job_title']+'\n'+
                            self.state['job_url']+'\nSubmitted: '+self.state['confirmed_at']+
                            '\n\nJob description and tailored documents: '+documents+
                            '\nTracker: reports/job_tracker.csv\n'+
                            ('Cloud results are synced to your GitHub project. Private answers and receipts are saved in encrypted cloud state.\n'
                             if os.environ.get('JOB_ALERTS_CLOUD')=='1' else 'Private answers and receipt: '+self.state['archive']+'\n'))
        with smtplib.SMTP(os.environ.get('JOB_ALERTS_SMTP_HOST') or 'smtp.gmail.com',
                          int(os.environ.get('JOB_ALERTS_SMTP_PORT') or 587),timeout=30) as smtp:
            smtp.ehlo()
            if os.environ.get('JOB_ALERTS_SMTP_STARTTLS','true').lower() not in {'false','0','no'}:
                smtp.starttls();smtp.ehlo()
            smtp.login(username,password);smtp.send_message(message)
        self.state.update(notification_sent=True,notification_sent_at=now());save(self.path,self.state)
        from cloud_state import checkpoint
        checkpoint()
        return True
