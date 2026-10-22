#!/usr/bin/env python3
"""Provision encrypted cloud state and existing credentials without printing secrets."""
import os
import subprocess
from cryptography.fernet import Fernet
from cloud_state import ROOT,local_backend
from prepare_application import _load_dotenv

def main():
    key_path=ROOT/'.local/cloud/state-key';key_path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if not key_path.exists():key_path.write_bytes(Fernet.generate_key());key_path.chmod(0o600)
    _load_dotenv(ROOT/'.env')
    secrets={'JOB_ALERTS_STATE_KEY':key_path.read_text().strip()}
    for name in ('OPENAI_API_KEY','JOB_ALERTS_EMAIL_TO','JOB_ALERTS_EMAIL_FROM','JOB_ALERTS_SMTP_HOST',
                 'JOB_ALERTS_SMTP_PORT','JOB_ALERTS_SMTP_USERNAME','JOB_ALERTS_SMTP_PASSWORD'):
        if os.environ.get(name):secrets[name]=os.environ[name]
    for name,value in secrets.items():
        subprocess.run(['gh','secret','set',name,'--repo','yjlee84/job-alerts'],input=value,text=True,check=True,capture_output=True)
        print('Configured secret:',name,flush=True)
    backend=local_backend();backend.seed()
    print('Seeded encrypted cloud state with the current tracker, documents and existing application receipts.')

if __name__=='__main__':main()
