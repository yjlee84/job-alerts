#!/usr/bin/env python3
"""Inspect/fill the user's existing Safari Easy Apply tab; never submit.

Requires Safari Settings > Developer > Allow JavaScript from Apple Events.
Uses the user's normal browser without changing its identity or bypassing checks.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from application_policy import Budget
from fill_application import ROOT, LOCAL, SCAN_FIELDS, SNAPSHOT, read_job, known_answer, draft_answers, write_review, is_hiring_message
from prepare_application import _load_dotenv
from application_records import repair_employment, repair_education


def evaluate(code: str, publication_id: str):
    from portal_session import evaluator
    active=evaluator.get()
    if active is not None:return active(code,publication_id)
    # Find only this job's tab. User switching active tabs cannot cause profile
    # data to be pasted elsewhere. JS verifies origin and job again before work.
    wrapped = """(() => {
      try {
      if(location.hostname!=='jobs.smartrecruiters.com' ||
        !%s.some(id=>location.pathname.includes(id))) throw Error('Wrong Safari tab; expected selected OECD application');
      return JSON.stringify({ok:true,value:(%s)()});
      } catch(error) { return JSON.stringify({ok:false,error:String(error)}); }
    })()""" % (json.dumps([publication_id]), code)
    # AppleScript source goes through stdin, never a shell or a command argument.
    literal = json.dumps(wrapped, ensure_ascii=False)
    token = json.dumps(publication_id)
    script = f'''tell application "Safari"
      repeat with browserWindow in windows
        repeat with tabIndex from (count of tabs of browserWindow) to 1 by -1
          set candidate to tab tabIndex of browserWindow
          if (URL of candidate) contains {token} then
            return (do JavaScript {literal} in candidate)
          end if
        end repeat
      end repeat
      error "No Safari tab is open for the selected job."
    end tell'''
    result = subprocess.run(["osascript", "-"], input=script, capture_output=True, text=True, timeout=35)
    if result.returncode:
        raise RuntimeError(result.stderr.strip())
    if not result.stdout.strip():
        raise RuntimeError('Safari returned no result; check that the page has finished loading.')
    envelope = json.loads(result.stdout)
    if not envelope['ok']:
        raise RuntimeError(envelope['error'])
    return envelope['value']


# Do not expose this as an arbitrary remote scripting interface. It executes
# only our reviewed code, with data passed using JSON literals.
SAFARI_GUARD = r"""() => {
 if(window.__applicationReviewGuard>=4) return true;
 if(window.__applicationReviewGuard)throw Error('Reload this page before installing the current submission guard');
 const denied=()=>{throw Error('Network write disabled during application review')};
 const read=m=>['GET','HEAD','OPTIONS'].includes((m||'GET').toUpperCase());
 const originalFetch=window.fetch;
 window.__workerNativeFetch=originalFetch;
 window.__workerNetworkResponses=[];
 const permit=(method,url)=>{
   if(read(method))return true;
   const target=new URL(url,location.href).href;
   if((method||'GET').toUpperCase()!=='POST')return false;
   if(window.__workerAllowSiteServices&&['api-js.datadome.co','rum.smartrecruiters.com','capture.trackjs.com'].includes(new URL(target).hostname))return true;
   if(window.__reviewUploadEndpoint&&target===window.__reviewUploadEndpoint)return true;
   const state=window.__workerSubmission;
   if(state?.mode==='submit'&&!state.sent&&Date.now()<state.expires&&target===state.endpoint){state.sent=true;return true}
   return false;
 };
 const capture=(url,status,body)=>window.__workerNetworkResponses.push({url:String(url),status,body});
 window.__reviewBlockedRequests=window.__reviewBlockedRequests||[];
 window.fetch=function(input,init){if(!permit(init?.method || input?.method,String(input?.url||input))){
   window.__reviewBlockedRequests.push({method:init?.method||input?.method||'GET',url:String(input?.url||input),kind:'fetch'});
   return Promise.reject(Error('Application writes blocked'))}
   const result=originalFetch.call(this,input,init);
   if(!read(init?.method||input?.method))result.then(r=>r.clone().text().then(body=>capture(r.url,r.status,body))).catch(()=>{});
   return result};
 const originalOpen=XMLHttpRequest.prototype.open;
 XMLHttpRequest.prototype.open=function(method,...args){this.__reviewMethod=method;this.__reviewUrl=String(args[0]);return originalOpen.call(this,method,...args)};
 const originalSend=XMLHttpRequest.prototype.send;
 window.__workerNativeSend=originalSend;
 XMLHttpRequest.prototype.send=function(...args){if(!permit(this.__reviewMethod,this.__reviewUrl)){
   window.__reviewBlockedRequests.push({method:this.__reviewMethod,url:this.__reviewUrl,kind:'xhr',bodyType:args[0]?.constructor?.name});
   return denied()}
   if(!read(this.__reviewMethod))this.addEventListener('loadend',()=>{
      let body='';try{body=this.responseText}catch{}capture(this.__reviewUrl,this.status,body)});
   return originalSend.apply(this,args)};
 navigator.sendBeacon=()=>false;
 window.WebSocket=function(){return denied()};
 const stop=e=>{e.preventDefault();e.stopImmediatePropagation()};
 const armed=()=>window.__workerSubmission&&Date.now()<window.__workerSubmission.expires;
 document.addEventListener('submit',e=>{if(!armed())stop(e)},true);
 document.addEventListener('keydown',e=>{if(e.key==='Enter'&&e.target.tagName!=='TEXTAREA')stop(e)},true);
 document.addEventListener('click',e=>{
   const final=e.composedPath().some(b=>b.matches?.('button,input[type=submit],a,[role=button],spl-button') &&
     /\b(submit|send application|apply now|complete application|finish|confirm application)\b/i.test(b.innerText||b.textContent||b.value||b.getAttribute('aria-label')||''));
   if(final&&!armed())stop(e)},true);
 HTMLFormElement.prototype.submit=denied;
 HTMLFormElement.prototype.requestSubmit=denied;
 window.__applicationReviewGuard=4;
 return true;
}"""


def run(args):
    job = read_job(args.job_id)
    # Derive the numeric id from this job, not a model or a generic browser tab.
    match = re.search(r"/OECD/(\d+)", job['job_url'])
    if not match:
        raise ValueError('Expected an OECD SmartRecruiters listing')
    publication_id = args.publication_id or match[1]
    if not re.fullmatch(r'[a-zA-Z0-9-]+', publication_id):
        raise ValueError('Invalid publication id')
    if args.open:
        url = (f'https://jobs.smartrecruiters.com/oneclick-ui/company/OECD/publication/{publication_id}?dcr_ci=OECD'
               if args.publication_id else job['job_url'])
        script = f'''tell application "Safari"
          activate
          if (count of windows) is 0 then make new document
          tell front window
            set current tab to (make new tab with properties {{URL:{json.dumps(url)}}})
          end tell
        end tell'''
        subprocess.run(['osascript','-'],input=script,text=True,check=True,capture_output=True,timeout=30)
        for _ in range(20):
            time.sleep(1)
            try:
                if evaluate('() => document.readyState',publication_id)=='complete':
                    break
            except RuntimeError:
                pass
    if args.back:
        evaluate(SAFARI_GUARD,publication_id)
        evaluate("""() => {
          const choices=[...document.querySelectorAll('button,a,spl-button')].filter(e=>e.textContent.trim()==='Back');
          if(choices.length!==1)throw Error('No unique Back control');
          choices[0].click();return true;
        }""",publication_id)
        time.sleep(2)
    profile = json.loads(Path(args.profile).read_text())
    dest = LOCAL / 'applications' / args.job_id / 'safari'
    dest.mkdir(parents=True, exist_ok=True)
    budget = Budget(dest.parent / 'usage.json', args.budget_usd)
    _load_dotenv(ROOT / '.env')
    record_repairs = json.loads((dest/'record-repairs.json').read_text()) if (dest/'record-repairs.json').exists() else []
    education_repairs = json.loads((dest/'education-repairs.json').read_text()) if (dest/'education-repairs.json').exists() else []
    if args.repair_records:
        evaluate(SAFARI_GUARD, publication_id)
        # Capture current records before any reversible editor changes.
        backup=evaluate('() => document.body.innerText', publication_id)
        (dest/'records-before.txt').write_text(backup)
        record_repairs=repair_employment(profile,evaluate,publication_id)
        (dest/'record-repairs.json').write_text(json.dumps(record_repairs,ensure_ascii=False,indent=2))
    if args.repair_education:
        evaluate(SAFARI_GUARD,publication_id)
        (dest/'education-before.txt').write_text(evaluate('() => document.body.innerText',publication_id))
        education_repairs=repair_education(profile,evaluate,publication_id)
        (dest/'education-repairs.json').write_text(json.dumps(education_repairs,ensure_ascii=False,indent=2))
    fields = evaluate(SCAN_FIELDS, publication_id)
    for field in fields:
        field['frame'] = 0
    state = {'job': {k: job[k] for k in ('job_id','job_title','job_url')},
             'url': evaluate('() => location.href', publication_id),
             'created_at': datetime.now(timezone.utc).isoformat(), 'submitted': False,
             'status': ('Draft record repair finished; see verification results. Not submitted.' if args.repair_records or args.repair_education
                        else 'Inspected existing Safari form; no fields changed.'),
             'network_policy':'Safari page-level submission protection is active after filling and lasts until navigation or reload.',
             'gpt_cost_usd': budget.spent, 'budget_usd': args.budget_usd,
             'notes': list(profile.get('review_notes', [])), 'fields': fields, 'attachments': [],
             'record_repairs': record_repairs,
             'education_repairs': education_repairs,
             'proposed_profile': {**profile['personal'], **profile.get('qualifications', {}),
                                  'availability': profile['preferences']['availability']}}
    for name, filename in [('Resume','resume.pdf'),('Cover letter','cover_letter.pdf')]:
        path=ROOT/'outputs'/args.job_id/filename
        state['attachments'].append({'name':name,'path':str(path),'status':'Ready for review; not uploaded' if path.exists() else 'Missing'})
    try:
        if args.fill:
            evaluate(SAFARI_GUARD, publication_id)
            if args.regenerate_message:
                for field in fields:
                    if is_hiring_message(field):
                        field['value']=''
            drafts = {} if args.no_gpt else draft_answers(fields, profile, job, budget, dest)
            for field in fields:
                field['worker_status']='Already present; review' if field['value'] else 'Needs your answer'
                if (field['value'] and not (args.refresh_confirmed and known_answer(field,profile))
                    and not (is_hiring_message(field) and field['key'] in drafts)) or field['type'] in {'radio','checkbox'}:
                    continue
                fact = known_answer(field, profile)
                draft = drafts.get(field['key'])
                if fact:
                    answer, source = fact
                elif draft:
                    answer, source = draft['answer'], 'GPT draft — '+draft['evidence']
                else:
                    continue
                if field['value']==answer:
                    field['worker_status']='Already matches confirmed answer'
                    continue
                if field['label'].lower()=='city' and isinstance(field.get('component_value'),dict):
                    location=field['component_value']
                    if location.get('city')==profile['personal']['city'] and location.get('country')==profile['personal']['country']:
                        field['worker_status']='Already matches confirmed location'
                        continue
                if field.get('max_length') and len(answer)>field['max_length']:
                    field['worker_status']='Draft exceeds character limit; not filled'
                    continue
                result=evaluate(r'''() => {
                  if(!window.__applicationReviewGuard) throw Error('Review guard missing');
                  const key=KEY, answer=ANSWER;
                  const node=window.__workerFieldNodes?.get(key);
                  const e=node?.input||document.querySelector('[data-worker-key="'+key+'"]');
                  if(!e) return {ok:false,note:'Field changed; inspect again'};
                  if(node?.host) {
                    e.click();
                    return {ok:false,needs_option:true,note:'Dropdown opened; option selection required'};
                  }
                  if(e.tagName==='SELECT') {
                    const options=[...e.options].filter(o=>[o.text.trim().toLowerCase(),o.value.toLowerCase()].includes(answer.toLowerCase()));
                    if(options.length!==1)return {ok:false,note:'No unique matching option'};
                    e.value=options[0].value;
                  } else if(['text','email','tel','url','textarea','search','date','number'].includes(e.type)) {
                    const prototype=e.tagName==='TEXTAREA'?HTMLTextAreaElement.prototype:HTMLInputElement.prototype;
                    Object.getOwnPropertyDescriptor(prototype,'value').set.call(e,answer);
                  } else return {ok:false,note:'Custom control requires adapter'};
                  e.dispatchEvent(new Event('input',{bubbles:true}));
                  e.dispatchEvent(new Event('change',{bubbles:true}));e.blur();
                  return {ok:true,value:e.value,note:'Read back from live Safari form'};
                }'''.replace('KEY',json.dumps(field['key'])).replace('ANSWER',json.dumps(answer)), publication_id)
                if result.get('needs_option'):
                    result = evaluate(r'''() => {
                      const node=window.__workerFieldNodes.get(KEY), answer=ANSWER;
                      const found=[];
                      const norm=s=>s.replace(/\s+/g,' ').trim();
                      const walk=root=>{for(const e of root.querySelectorAll('*')){
                        if(e.getAttribute('role')==='option'&&e.getClientRects().length){
                          const host=e.getRootNode().host;
                          const label=norm(host?.textContent||e.textContent);
                          if(label===norm(answer))found.push(e);
                        }
                        if(e.shadowRoot)walk(e.shadowRoot);
                      }};
                      walk(node.host.shadowRoot);
                      if(found.length!==1)return {ok:false,note:'No unique rendered option; needs review'};
                      found[0].click();
                      return {ok:true,value:answer,note:'Selected rendered option'};
                    }'''.replace('KEY',json.dumps(field['key'])).replace('ANSWER',json.dumps(answer)),publication_id)
                field.update(source=source,worker_status='Filled; review' if result['ok'] else result['note'])
                if result['ok']:
                    field['value']=result['value']
            current = {field['key']: field for field in evaluate(SCAN_FIELDS, publication_id)}
            for field in fields:
                actual = current.get(field['key'])
                if actual and actual['label'] == field['label']:
                    if field.get('worker_status') == 'Filled; review' and field['value'] != actual['value']:
                        field['worker_status'] = 'Value changed after filling; review'
                    field['value'] = actual['value']
                elif field.get('worker_status') == 'Filled; review':
                    field['worker_status'] = 'Field changed after filling; inspect again'
            state['status']='Filled available Safari fields; stopped before submission.' if fields else 'No visible application fields found.'
        (dest/'form.html').write_text(evaluate(SNAPSHOT, publication_id))
        (dest/'page-text.txt').write_text(evaluate('() => document.body.innerText',publication_id))
    finally:
        state['gpt_cost_usd']=budget.spent
        write_review(dest,state)
        stage='screening' if '/screening' in state['url'] else 'profile'
        (dest/f'review-{stage}.json').write_text((dest/'review.json').read_text())
        (dest/f'review-{stage}.html').write_text((dest/'review.html').read_text())
        if (dest/'form.html').exists():
            (dest/f'form-{stage}.html').write_text((dest/'form.html').read_text())
    print(json.dumps({'status':state['status'],'fields':[{k:f.get(k) for k in ('key','label','value','component_value','worker_status')} for f in fields],
                      'review':str(dest/'review.html'),'gpt_cost_usd':budget.spent,'submitted':False},ensure_ascii=False,indent=2))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job-id',required=True)
    parser.add_argument('--publication-id',help='Publication UUID from this job’s Easy Apply URL')
    parser.add_argument('--profile',default=str(LOCAL/'profile/applicant.json'))
    parser.add_argument('--fill',action='store_true',help='Fill supported answers after installing the review guard')
    parser.add_argument('--open',action='store_true',help='Open this job in a new Safari tab')
    parser.add_argument('--back',action='store_true',help='Use only the exact Back control to return to the previous application step')
    parser.add_argument('--refresh-confirmed',action='store_true',help='Refresh factual fields from the confirmed profile; preserve other existing answers')
    parser.add_argument('--repair-records',action='store_true',help='Repair employment records using confirmed dates in the local profile')
    parser.add_argument('--repair-education',action='store_true',help='Repair and separate degree records using confirmed dates in the local profile')
    parser.add_argument('--regenerate-message',action='store_true',help='Replace the hiring-team message using current writing preferences; requires --fill')
    parser.add_argument('--no-gpt',action='store_true')
    parser.add_argument('--budget-usd',type=float,default=0.03)
    args=parser.parse_args()
    if not re.fullmatch(r'[a-zA-Z0-9_-]+',args.job_id):
        parser.error('Invalid job id')
    lock_dir=LOCAL/'locks'
    lock_dir.mkdir(parents=True,exist_ok=True)
    with (lock_dir/(args.job_id+'.lock')).open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        run(args)


if __name__=='__main__':
    main()
