#!/usr/bin/env python3
"""Install the authorized local scheduler, preserving the previous deployment."""
import json
import plistlib
import shutil
from datetime import datetime
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

def install():
    home=Path.home();stamp=datetime.now().strftime('%Y%m%d-%H%M%S')
    backup=ROOT/'.local/deployment-backups'/stamp;backup.mkdir(parents=True,mode=0o700)
    wrapper=home/'bin/job-alerts-sync.sh';plist=home/'Library/LaunchAgents/com.yongjai.job-alerts-sync.plist'
    for file in (wrapper,plist,home/'Desktop/Job Alerts.md',home/'Desktop/Job_Alerts.md'):
        if file.exists():shutil.copy2(file,backup/file.name)
    wrapper.parent.mkdir(parents=True,exist_ok=True)
    shutil.copy2(ROOT/'deployment/job-alerts-sync.sh',wrapper);wrapper.chmod(0o700)
    (ROOT/'logs').mkdir(exist_ok=True);plist.parent.mkdir(parents=True,exist_ok=True)
    settings={'Label':'com.yongjai.job-alerts-sync','ProgramArguments':[str(wrapper)],
              'RunAtLoad':True,'ProcessType':'Background',
              'EnvironmentVariables':{'JOB_ALERTS_REPO':str(ROOT)},
              'StandardOutPath':str(ROOT/'logs/automation_stdout.log'),
              'StandardErrorPath':str(ROOT/'logs/automation_stderr.log')}
    plist.write_bytes(plistlib.dumps(settings));plist.chmod(0o600)
    for file in (home/'Desktop/Job Alerts.md',home/'Desktop/Job_Alerts.md'):
        if file.exists():file.unlink() # User requested removal; backed up above.
    config=ROOT/'.local/automation.json'
    config.write_text(json.dumps({'enabled':True,'minimum_interval_seconds':0,
                                  'schedule':'login_only','installed_at':stamp},indent=2)+'\n');config.chmod(0o600)
    print('Installed login-only automation; no periodic or wake runs.')
    print('Backup:',backup);print('LaunchAgent:',plist)

if __name__=='__main__':install()
