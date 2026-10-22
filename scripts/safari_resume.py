#!/usr/bin/env python3
"""Attach the exact per-job PDF through Safari's normal resume control.

The initial probe keeps writes blocked and records the upload endpoint. The
allowlisted mode permits only that exact file-upload endpoint; submission
requests remain blocked. No HTTP request is constructed from a model output.
"""
from __future__ import annotations
import argparse
import base64
import fcntl
import hashlib
import json
import re
from pathlib import Path
import time
from urllib.parse import urlparse, urljoin, parse_qs

from safari_application import evaluate, SAFARI_GUARD
from fill_application import ROOT, LOCAL, read_job


def validate_upload_endpoint(url: str, observed: list[dict]) -> None:
    endpoint=urlparse(url)
    if (endpoint.scheme!='https' or endpoint.netloc!='jobs.smartrecruiters.com' or
        not re.fullmatch(r'/oneclick-ui/api/company/[0-9a-f]{24}/attachment/resume',endpoint.path)):
        raise ValueError('Only the inspected SmartRecruiters resume-attachment endpoint is permitted')
    if not any(r.get('method','').upper()=='POST' and r.get('bodyType')=='FormData' and
               urljoin('https://jobs.smartrecruiters.com',r.get('url',''))==url for r in observed):
        raise ValueError('Upload URL was not observed in a blocked resume-selection request')


def upload(args):
    read_job(args.job_id)
    profile=json.loads((LOCAL/'profile/applicant.json').read_text())
    if not profile.get('application_permissions',{}).get(args.job_id,{}).get('upload_tailored_resume'):
        raise ValueError('This job has no explicit resume-upload authorization')
    source=ROOT/'outputs'/args.job_id/'resume.pdf'
    content=source.read_bytes()
    if not content.startswith(b'%PDF-') or len(content)>10*1024*1024:
        raise ValueError('Expected a PDF smaller than 10 MiB')
    dest=LOCAL/'applications'/args.job_id/'safari'
    dest.mkdir(parents=True,exist_ok=True)
    job=read_job(args.job_id)
    safe=lambda text:re.sub(r'[^A-Za-z0-9_-]+','_',text).strip('_')
    filename='_'.join(safe(text) for text in (profile['personal']['first_name'],profile['personal']['last_name'],job['company'],args.job_id,'resume'))+'.pdf'
    evaluate(SAFARI_GUARD,args.publication_id)
    if args.endpoint:
        observed=json.loads((dest/'resume-upload.json').read_text()).get('blocked',[])
        validate_upload_endpoint(args.endpoint,observed)
        if parse_qs(urlparse(args.endpoint).query).get('filename')!=[filename]:
            raise ValueError('Upload filename changed; run a fresh blocked probe for the current file')
        # The earlier guard intentionally retained no originals. A fresh, same-
        # origin blank realm supplies native browser methods for this exact
        # allowlist. Final-button and native-submit protection remain installed.
        evaluate(r'''() => {
          const allowed=ENDPOINT;
          if(window.__applicationReviewGuard>=4){window.__reviewUploadEndpoint=allowed;return true}
          const frame=document.createElement('iframe');frame.style.display='none';document.body.appendChild(frame);
          const nativeFetch=frame.contentWindow.fetch;
          const nativeSend=frame.contentWindow.XMLHttpRequest.prototype.send;
          const permit=(method,url)=>['GET','HEAD','OPTIONS'].includes((method||'GET').toUpperCase()) ||
            ((method||'GET').toUpperCase()==='POST'&&new URL(url,location.href).href===allowed);
          window.fetch=function(input,init){const method=init?.method||input?.method||'GET',url=String(input?.url||input);
            if(!permit(method,url)){window.__reviewBlockedRequests.push({method,url,kind:'fetch'});return Promise.reject(Error('Application write blocked'))}
            return nativeFetch.call(window,input,init)};
          const open=XMLHttpRequest.prototype.open;
          XMLHttpRequest.prototype.open=function(method,url,...rest){this.__reviewMethod=method;this.__reviewUrl=String(url);return open.call(this,method,url,...rest)};
          XMLHttpRequest.prototype.send=function(body){
            if(!permit(this.__reviewMethod,this.__reviewUrl)){
              window.__reviewBlockedRequests.push({method:this.__reviewMethod,url:this.__reviewUrl,kind:'xhr',bodyType:body?.constructor?.name});
              throw Error('Application write blocked')}
            return nativeSend.call(this,body)};
          window.__reviewUploadEndpoint=allowed;
          return true;
        }'''.replace('ENDPOINT',json.dumps(args.endpoint)),args.publication_id)
    evaluate('() => {window.__resumeChunks=[];return true}',args.publication_id)
    encoded=base64.b64encode(content).decode()
    for offset in range(0,len(encoded),48000):
        evaluate('() => {window.__resumeChunks.push('+json.dumps(encoded[offset:offset+48000])+');return true}',args.publication_id)
    selected=evaluate(r'''() => {
      const host=document.querySelector('oc-resume-upload spl-dropzone');
      if(!host)throw Error('No resume upload control on this step');
      const deep=root=>{const i=root.querySelector('input[type=file]');if(i)return i;
        for(const e of root.querySelectorAll('*'))if(e.shadowRoot){const i=deep(e.shadowRoot);if(i)return i}};
      const input=deep(document.querySelector('oc-resume-upload'));
      if(!input)throw Error('No resume file input');
      const binary=atob(window.__resumeChunks.join(''));window.__resumeChunks=[];
      const file=new File([Uint8Array.from(binary,c=>c.charCodeAt(0))],FILENAME,{type:'application/pdf'});
      const transfer=new DataTransfer();transfer.items.add(file);input.value='';input.files=transfer.files;
      input.dispatchEvent(new Event('change',{bubbles:true,composed:true}));
      return {name:file.name,size:file.size};
    }'''.replace('FILENAME',json.dumps(filename)),args.publication_id)
    time.sleep(3)
    result=evaluate(r'''() => {
      const host=document.querySelector('oc-resume-upload spl-dropzone');
      return {url:location.href,files:host.files,attribute:host.getAttribute('files'),
        blocked:window.__reviewBlockedRequests||[],text:document.body.innerText.slice(-5000)};
    }''',args.publication_id)
    result.update(source=str(source),sha256=hashlib.sha256(content).hexdigest(),selected=selected,submitted=False)
    result['upload_verified']=bool(args.endpoint and len(result.get('files') or [])==1 and
        result['files'][0].get('fileId') and result['files'][0].get('fileName')==filename)
    (dest/'resume-upload.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False,indent=2))
    if args.endpoint and not result['upload_verified']:
        raise RuntimeError('Portal did not confirm the expected uploaded file; see resume-upload.json')


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id',required=True)
    parser.add_argument('--publication-id',required=True)
    parser.add_argument('--endpoint',help='Exact upload URL observed by the blocked probe')
    args=parser.parse_args()
    read_job(args.job_id)
    lockdir=LOCAL/'locks';lockdir.mkdir(parents=True,exist_ok=True)
    with (lockdir/(args.job_id+'.lock')).open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        upload(args)
