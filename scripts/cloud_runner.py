#!/usr/bin/env python3
"""Daily cloud entrypoint. Restore/checkpoint encrypted state before side effects."""
import argparse
import os
import sys
from pathlib import Path
from cloud_state import GitHubState,activate,checkpoint,ROOT
from application_store import save,attention_email

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--probe',action='store_true',help='Inspect the public Easy Apply form without applying')
    args=parser.parse_args()
    os.umask(0o077);os.environ['JOB_ALERTS_CLOUD']='1'
    backend=GitHubState(os.environ['GITHUB_REPOSITORY'],os.environ['JOB_ALERTS_STATE_KEY'].encode(),os.environ['GITHUB_TOKEN'])
    backend.restore();activate(backend)
    save(ROOT/'.local/automation.json',{'enabled':True,'backend':'cloud','schedule':'09:00 Europe/Berlin','minimum_interval_seconds':0})
    try:
        if args.probe:
            from cloud_application import probe
            print(probe('https://jobs.smartrecruiters.com/oneclick-ui/company/OECD/publication/fec28a70-5a02-4dd7-b4b6-af7c28414091?dcr_ci=OECD'))
        else:
            from run_automation import run
            run()
    except Exception as error:
        # Logs expose a concise failure class; private diagnostics stay encrypted.
        directory=ROOT/'.local/applications/cloud-runner';directory.mkdir(parents=True,exist_ok=True)
        save(directory/'failure.json',{'error':type(error).__name__,'message':str(error)})
        print('Cloud run failed:',type(error).__name__,str(error),file=sys.stderr)
        try:attention_email('cloud-runner','Daily cloud job pipeline',str(error),directory)
        except Exception as email_error:print('Failure notification pending:',type(email_error).__name__,file=sys.stderr)
        raise
    finally:checkpoint()

if __name__=='__main__':main()
