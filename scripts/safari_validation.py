#!/usr/bin/env python3
"""Check client-side application validation; never click Submit."""
import argparse
import fcntl
import hashlib
import json
import time

from safari_application import evaluate, SAFARI_GUARD
from fill_application import ROOT, LOCAL, SCAN_FIELDS, SNAPSHOT, read_job, write_review


CHECK_PAGE=r'''() => {
  const all=[];const walk=root=>{for(const e of root.querySelectorAll('*')){all.push(e);if(e.shadowRoot)walk(e.shadowRoot)}};walk(document);
  const visible=e=>e.getClientRects().length&&getComputedStyle(e).visibility!=='hidden';
  const errors=all.filter(e=>visible(e)&&e.getAttribute('aria-invalid')==='true').map(e=>({id:e.id,label:e.getAttribute('aria-label')||e.getRootNode().host?.label||e.name||e.id}));
  const native=all.filter(e=>visible(e)&&e.willValidate&&e.validity&&!e.validity.valid).map(e=>({id:e.id,message:e.validationMessage}));
  const alerts=all.filter(e=>visible(e)&&e.getAttribute('role')==='alert'&&e.textContent.trim()).map(e=>e.textContent.trim());
  const final=[...document.querySelectorAll('spl-button,button')].filter(e=>e.textContent.trim()==='Submit').map(e=>({disabled:!!e.disabled||e.hasAttribute('disabled'),label:e.textContent.trim()}));
  return {url:location.href,errors,native,alerts,open_editors:document.querySelectorAll('oc-experience-edit-form,oc-education-edit-form').length,
    submit_controls:final,submission_guard:window.__applicationReviewGuard,body:document.body.innerText};
}'''


def inspect(pid):
    fields=evaluate(SCAN_FIELDS,pid)
    check=evaluate(CHECK_PAGE,pid)
    check['fields']=fields
    check['missing_required']=[{'key':f['key'],'label':f['label']} for f in fields
                               if f['required'] and (not f['value'])]
    check['client_valid']=not(check['errors'] or check['native'] or check['missing_required'] or check['open_editors'])
    return check


def run(args):
    read_job(args.job_id)
    profile=json.loads((LOCAL/'profile/applicant.json').read_text())
    dest=LOCAL/'applications'/args.job_id/'safari'
    evaluate(SAFARI_GUARD,args.publication_id)
    report={'submitted':False,'server_submission_validation_performed':False,'steps':[]}
    first=inspect(args.publication_id)
    report['steps'].append(first)
    if args.next and '/screening' not in first['url']:
        attachment=json.loads((dest/'resume-upload.json').read_text())
        if not attachment.get('files') or not attachment['files'][0].get('fileId'):
            raise RuntimeError('Tailored resume upload is not confirmed; not advancing')
        digest=hashlib.sha256((ROOT/'outputs'/args.job_id/'resume.pdf').read_bytes()).hexdigest()
        if digest!=attachment.get('sha256'):
            raise RuntimeError('Tailored PDF has changed since upload; upload the current version before advancing')
        portal_files=evaluate('() => document.querySelector("oc-resume-upload spl-dropzone")?.files||[]',args.publication_id)
        if not any(f.get('fileId')==attachment['files'][0]['fileId'] for f in portal_files):
            raise RuntimeError('Uploaded tailored resume is not present in the current form')
        if first['client_valid']:
            evaluate(r'''() => {
              const choices=[...document.querySelectorAll('oc-nav-first-page spl-button')].filter(e=>e.textContent.trim()==='Next');
              if(choices.length!==1||choices[0].disabled)throw Error('No enabled unique Next control');
              choices[0].click();return true;
            }''',args.publication_id)
            time.sleep(1)
        else:
            report['stopped']='First-page client validation has errors; Next was not clicked'
    current=evaluate('() => location.href',args.publication_id)
    if args.check_privacy and '/screening' in current:
        if not profile.get('application_permissions',{}).get(args.job_id,{}).get('privacy_certification'):
            raise ValueError('Privacy/certification checkbox is not authorized for this job')
        fields=evaluate(SCAN_FIELDS,args.publication_id)
        matches=[f for f in fields if f['type']=='checkbox' and 'privacy notice of the OECD' in f['label']
                 and 'certify' in f['label'].lower()]
        if len(matches)!=1:
            raise RuntimeError('No unique OECD privacy/certification checkbox')
        result=evaluate(r'''() => {
          const node=window.__workerFieldNodes.get(KEY);if(!node)throw Error('Checkbox changed');
          if(!node.input.checked)node.input.click();
          return {checked:node.input.checked};
        }'''.replace('KEY',json.dumps(matches[0]['key'])),args.publication_id)
        report['privacy']=result
        time.sleep(0.2)
    final=inspect(args.publication_id)
    report['steps'].append(final)
    report['final_client_valid']=('/screening' in final['url'] and final['client_valid'] and
                                  any(not control['disabled'] for control in final['submit_controls']))
    (dest/'validation.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    state=json.loads((dest/'review.json').read_text())
    state.update(url=final['url'],fields=final['fields'],validation=report,
                 status='Client-side validation passed; stopped before Submit.' if report['final_client_valid'] else 'Client-side validation is incomplete; see validation results.')
    uploaded=json.loads((dest/'resume-upload.json').read_text())
    for attachment in state['attachments']:
        if attachment['name']=='Resume' and uploaded.get('files'):
            attachment.update(status='Uploaded tailored per-job PDF',portal_file=uploaded['files'][0],sha256=uploaded['sha256'])
    write_review(dest,state)
    stage='screening' if '/screening' in final['url'] else 'profile'
    (dest/f'review-{stage}.json').write_text((dest/'review.json').read_text())
    (dest/f'review-{stage}.html').write_text((dest/'review.html').read_text())
    (dest/'form.html').write_text(evaluate(SNAPSHOT,args.publication_id))
    print(json.dumps({k:v for k,v in report.items() if k!='steps'},indent=2))
    print(json.dumps([{'url':s['url'],'errors':s['errors'],'native':s['native'],'missing_required':s['missing_required'],
                      'open_editors':s['open_editors'],'alerts':s['alerts'],'submit_controls':s['submit_controls']} for s in report['steps']],indent=2))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id',required=True)
    parser.add_argument('--publication-id',required=True)
    parser.add_argument('--next',action='store_true',help='Advance only with the first page’s Next control after checking validation')
    parser.add_argument('--check-privacy',action='store_true',help='Check the exact OECD privacy/certification checkbox with explicit per-job authorization')
    args=parser.parse_args()
    read_job(args.job_id)
    lockdir=LOCAL/'locks';lockdir.mkdir(parents=True,exist_ok=True)
    with (lockdir/(args.job_id+'.lock')).open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        run(args)
