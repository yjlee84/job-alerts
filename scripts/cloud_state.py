"""Authenticated encrypted snapshots with durable GitHub checkpoints.

State is encrypted BEFORE leaving the runner, including the submission intent.
A failed checkpoint prevents the subsequent submit click or paid API call.
"""
import argparse
import base64
import csv
import io
import json
import os
import subprocess
import tarfile
from pathlib import Path,PurePosixPath
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import Request,urlopen

from cryptography.fernet import Fernet

ROOT=Path(__file__).resolve().parents[1]
BRANCH='application-state'
FILE='cloud-state.enc'
_backend=None

def allowed(name):
    p=PurePosixPath(name)
    if p.is_absolute() or '..' in p.parts:return False
    parts=p.parts
    return (len(parts)>2 and parts[:2] in {('.local','applications'),('.local','profile')}
            or name in {'.local/automation.json','.local/automation-status.json','.local/cloud-browser.json',
                        '.local/cloud-probe.json','reports/job_tracker.csv','data/job_listings.csv','data/fetch_runs.csv'}
            or len(parts)>1 and parts[0]=='outputs' and p.suffix in {'.pdf','.tex','.bib','.txt'})

def pack(root,key):
    root=Path(root);buffer=io.BytesIO()
    with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
        candidates=[]
        for relative in ('.local/applications','.local/profile','outputs'):
            path=root/relative
            if path.exists():candidates.extend(p for p in path.rglob('*') if p.is_file())
        for relative in ('.local/automation.json','.local/automation-status.json','.local/cloud-browser.json',
                         '.local/cloud-probe.json','reports/job_tracker.csv','data/job_listings.csv','data/fetch_runs.csv'):
            if (root/relative).exists():candidates.append(root/relative)
        for path in sorted(set(candidates)):
            name=path.relative_to(root).as_posix()
            if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()) or not allowed(name):continue
            archive.add(path,arcname=name,recursive=False)
    encrypted=Fernet(key).encrypt(buffer.getvalue())
    if len(encrypted)>64*1024*1024:raise RuntimeError('Encrypted state exceeds the checkpoint size limit; archive storage needs expansion')
    return encrypted

def unpack(payload,root,key):
    root=Path(root).resolve();content=Fernet(key).decrypt(payload)
    with tarfile.open(fileobj=io.BytesIO(content),mode='r:gz') as archive:
        members=archive.getmembers()
        if len(members)>50000 or sum(m.size for m in members)>512*1024*1024:raise ValueError('State archive exceeds extraction limits')
        # Validate every entry before writing any of them.
        for member in members:
            target=root/member.name
            if not member.isfile() or not allowed(member.name) or not target.resolve().is_relative_to(root):
                raise ValueError('Unsafe encrypted state member')
        for member in members:
            target=root/member.name;target.parent.mkdir(parents=True,exist_ok=True)
            target.write_bytes(archive.extractfile(member).read());target.chmod(0o600)

class GitHubState:
    def __init__(self,repo,key,token,root=ROOT):
        if not repo or len(repo.split('/'))!=2:raise ValueError('GitHub repository is required')
        self.repo=repo;self.key=key;self.token=token;self.root=Path(root);self.sha=None

    def api(self,path,method='GET',body=None,raw=False):
        request=Request('https://api.github.com/repos/'+self.repo+'/'+path,
                        data=json.dumps(body).encode() if body is not None else None,method=method,
                        headers={'Authorization':'Bearer '+self.token,'Accept':'application/vnd.github.raw+json' if raw else 'application/vnd.github+json',
                                 'X-GitHub-Api-Version':'2022-11-28','User-Agent':'job-alerts-state'})
        with urlopen(request,timeout=60) as response:
            return response.read() if raw else json.load(response)

    def restore(self):
        tracker=self.root/'reports/job_tracker.csv'
        public_rows=[]
        if tracker.exists():
            with tracker.open(newline='') as f:public_rows=list(csv.DictReader(f))
        public_outputs={p.relative_to(self.root):p.read_bytes() for p in (self.root/'outputs').rglob('*')
                        if p.is_file() and not p.is_symlink() and allowed(p.relative_to(self.root).as_posix())}
        path='contents/'+FILE+'?ref='+BRANCH
        metadata=self.api(path);self.sha=metadata['sha']
        payload=base64.b64decode(metadata['content']) if metadata.get('content') else self.api(path,raw=True)
        unpack(payload,self.root,self.key)
        # Main remains the user's editable tracker. Preserve manual statuses,
        # while recovering a confirmed application if its final public push
        # was interrupted and main still says Review.
        if tracker.exists():
            with tracker.open(newline='') as f:reader=csv.DictReader(f);fields=reader.fieldnames;restored=list(reader)
            indexed={r['Job ID']:r for r in restored}
            for row in public_rows:
                previous=indexed.get(row['Job ID'])
                if previous is None:restored.append(row);indexed[row['Job ID']]=row;continue
                if previous.get('Status','').lower() not in {'','review'} and row.get('Status','').lower() in {'','review'}:continue
                previous.update(row)
            buffer=io.StringIO();writer=csv.DictWriter(buffer,fieldnames=fields);writer.writeheader();writer.writerows(restored)
            tracker.write_text(buffer.getvalue())
        for relative,content in public_outputs.items():
            state=self.root/'.local/applications'/relative.parts[1]/'submission.json'
            if state.exists() and json.loads(state.read_text()).get('status') in {'submitting','uncertain','confirmed'}:continue
            target=self.root/relative;target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(content)

    def checkpoint(self):
        payload=pack(self.root,self.key)
        body={'message':'Checkpoint encrypted application state','branch':BRANCH,'content':base64.b64encode(payload).decode()}
        if self.sha:body['sha']=self.sha
        result=self.api('contents/'+FILE,'PUT',body)
        self.sha=result['content']['sha']

    def seed(self):
        try:self.api('git/ref/heads/'+BRANCH)
        except HTTPError as error:
            if error.code!=404:raise
            main=self.api('git/ref/heads/main')
            self.api('git/refs','POST',{'ref':'refs/heads/'+BRANCH,'sha':main['object']['sha']})
        try:
            self.sha=self.api('contents/'+FILE+'?ref='+BRANCH)['sha']
            raise RuntimeError('Cloud state already exists. Download it before changing the profile; never replace the application ledger with a stale local copy.')
        except HTTPError as error:
            if error.code!=404:raise
        self.checkpoint()

def activate(backend):
    global _backend
    _backend=backend

def checkpoint():
    if _backend is not None:_backend.checkpoint()
    elif os.environ.get('JOB_ALERTS_CLOUD')=='1':raise RuntimeError('Cloud persistence is not initialized; refusing side effects')

def local_backend():
    key=(ROOT/'.local/cloud/state-key').read_bytes().strip()
    token=subprocess.run(['gh','auth','token'],text=True,capture_output=True,check=True).stdout.strip()
    return GitHubState('yjlee84/job-alerts',key,token)

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('command',choices=['download'])
    args=parser.parse_args();local_backend().restore()
    print('Downloaded encrypted tracker, application receipts and interview archives into this project.')
