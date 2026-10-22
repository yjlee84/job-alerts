#!/usr/bin/env python3
"""Publish only the established project outputs; never private applicant records."""
import subprocess
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]

def git(*args,**kwargs):return subprocess.run(['git',*args],cwd=ROOT,check=True,text=True,**kwargs)

def main():
    paths=['data/fetch_runs.csv','data/job_listings.csv','reports/job_tracker.csv']
    for file in (ROOT/'outputs').rglob('*'):
        if file.is_file() and file.suffix in {'.pdf','.tex','.bib','.txt'}:paths.append(str(file.relative_to(ROOT)))
    git('add','--',*[p for p in paths if (ROOT/p).exists()])
    check=subprocess.run(['git','diff','--cached','--quiet'],cwd=ROOT)
    if check.returncode==0:print('No public project output changes.');return
    if check.returncode!=1:raise RuntimeError('Cannot inspect staged output changes')
    git('-c','user.name=github-actions[bot]','-c','user.email=41898282+github-actions[bot]@users.noreply.github.com',
        'commit','-m','Update job applications and tailored documents')
    # No force-push or automatic rebase over manual tracker edits. A conflict
    # fails visibly; the encrypted receipt and ledger remain durable.
    git('push','origin','HEAD:main')

if __name__=='__main__':main()
