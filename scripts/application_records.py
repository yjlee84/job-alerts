"""Repair SmartRecruiters record editors using applicant-confirmed dates only."""
from __future__ import annotations

import json
import re
import time


def validate_month(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Record dates must be confirmed YYYY-MM values")
    return value


def select_autocomplete(evaluate, pid, scope, selector, value, location=None):
    """Type then select the real component option; never assign a fake value."""
    data=json.dumps({'scope':scope,'selector':selector,'value':value,'query':location['city'] if location else value})
    evaluate(r'''() => {
      const d=DATA, root=document.querySelector(d.scope), host=root?.querySelector(d.selector);
      if(!host)throw Error('Missing autocomplete '+d.selector);
      window.__workerAutocomplete=host;
      if(host.value===d.value)return true;
      const deep=r=>{const i=r.querySelector('input');if(i)return i;for(const e of r.querySelectorAll('*'))if(e.shadowRoot){const i=deep(e.shadowRoot);if(i)return i}};
      const input=deep(host.shadowRoot||host);if(!input)throw Error('Missing autocomplete input');
      input.focus();input.click();Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,'');
      input.dispatchEvent(new Event('input',{bubbles:true,composed:true}));
      Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,d.query);
      input.dispatchEvent(new Event('input',{bubbles:true,composed:true}));return true;
    }'''.replace('DATA',data),pid)
    code=r'''() => {
      const value=VALUE,host=window.__workerAutocomplete,place=PLACE;
      if(host.value===value)return true;
      const found=[];const norm=s=>s.replace(/\s+/g,' ').trim();
      const walk=r=>{for(const e of r.querySelectorAll('*')){
        if(e.getAttribute('role')==='option'&&e.getClientRects().length){
          const label=norm(e.getRootNode().host?.textContent||e.textContent);
          if(label===norm(value)||(place&&label.startsWith(place.city+', ')&&label.endsWith(', '+place.country)))found.push(e);
        }
        if(e.shadowRoot)walk(e.shadowRoot);
      }};walk(host.shadowRoot||host);
      if(found.length!==1)return false;found[0].click();return true;
    }'''.replace('VALUE',json.dumps(value)).replace('PLACE',json.dumps(location))
    for _ in range(20):
        if evaluate(code,pid):
            return
        time.sleep(.25)
    raise RuntimeError('No unique rendered autocomplete option for '+selector)


def ensure_employment(profile, evaluate, pid):
    """Build missing entries on a fresh form before the existing date verifier."""
    for record in profile.get('employment',[]):
        if validate_month(record['start'])>validate_month(record['end']):
            raise ValueError('Employment start date is after end date')
        needed=evaluate(r'''() => {
          const r=RECORD,entries=[...document.querySelectorAll('oc-experience-entry')];
          const norm=s=>s.replace(/[’‘]/g,"'").replace(/\s+/g,' ').trim();
          if(entries.some(e=>norm(e.querySelector('[data-test=experience-entry-company]')?.textContent||'')===norm(r.organization)&&
            norm(e.querySelector('[data-test=experience-entry-title]')?.childNodes[0]?.textContent||'')===norm(r.title)))return false;
          if(!document.querySelector('oc-experience-edit-form'))document.querySelector('[data-test=add-experience] spl-button').click();
          return true;
        }'''.replace('RECORD',json.dumps(record)),pid)
        if not needed:
            continue
        time.sleep(.2)
        for selector,value in [('oc-job-title-autocomplete spl-autocomplete',record['title']),('oc-company-autocomplete spl-autocomplete',record['organization'])]:
            select_autocomplete(evaluate,pid,'oc-experience-edit-form',selector,value)
        evaluate(r'''() => {
          const r=RECORD,form=document.querySelector('oc-experience-edit-form');
          const deep=root=>{const i=root.querySelector('input');if(i)return i;for(const e of root.querySelectorAll('*'))if(e.shadowRoot){const i=deep(e.shadowRoot);if(i)return i}};
          for(const [field,month]of [['startDate',r.start],['endDate',r.end]]){
            const input=deep(form.querySelector('[formcontrolname="'+field+'"]'));
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,month.slice(5)+'/'+month.slice(0,4));
            for(const name of ['input','change','blur'])input.dispatchEvent(new Event(name,{bubbles:true,composed:true}));
          }
          const save=form.querySelector('[data-test=experience-save] spl-button');if(save.disabled)throw Error('Experience Save disabled');
          save.click();return true;
        }'''.replace('RECORD',json.dumps(record)),pid)
        time.sleep(.3)
        if evaluate('() => !!document.querySelector("oc-experience-edit-form")',pid):
            raise RuntimeError('Experience record did not save')
    result=repair_employment(profile,evaluate,pid)
    if len(result)!=len(profile['employment']) or any(r['status']!='Saved and verified confirmed dates' for r in result):
        raise RuntimeError('Employment records failed verification')
    return result


def repair_employment(profile: dict, evaluate, publication_id: str) -> list[dict]:
    """Use scoped Edit/Save controls, never the application's Submit control.

    Read dates back after reopening the saved record. Partial ranges and
    ambiguous entry matches are reported, not filled with default months.
    """
    results = []
    for record in profile.get('employment', []):
        if not record.get('start') or not record.get('end'):
            results.append({'organization': record['organization'], 'status': 'Waiting for confirmed start/end dates'})
            continue
        start, end = validate_month(record['start']), validate_month(record['end'])
        if start > end:
            raise ValueError('Employment start date is after end date')
        opened = evaluate(r'''() => {
          if(!window.__applicationReviewGuard)throw Error('Submission guard missing');
          const record=RECORD;
          const norm=s=>s.replace(/[’‘]/g,"'").replace(/\s+/g,' ').trim();
          const matches=[...document.querySelectorAll('oc-experience-entry')].filter(entry=>
            norm(entry.querySelector('[data-test="experience-entry-company"]')?.textContent||entry.querySelector('oc-company-autocomplete spl-autocomplete')?.value||'')===norm(record.organization) &&
            norm(entry.querySelector('[data-test="experience-entry-title"]')?.childNodes[0]?.textContent||entry.querySelector('oc-job-title-autocomplete spl-autocomplete')?.value||'')===norm(record.title));
          if(matches.length!==1)return {ok:false,note:'No unique matching employment entry'};
          window.__workerRecordEntry=matches[0];
          const button=matches[0].querySelector('[data-test="experience-entry-edit"]');
          if(button)button.click();
          else if(!matches[0].querySelector('oc-experience-edit-form'))return {ok:false,note:'Employment editor unavailable'};
          return {ok:true};
        }'''.replace('RECORD', json.dumps(record, ensure_ascii=False)), publication_id)
        if not opened['ok']:
            results.append({'organization': record['organization'], 'status': opened['note']})
            continue
        time.sleep(0.15)
        changed = evaluate(r'''() => {
          const record=RECORD, form=window.__workerRecordEntry?.querySelector('oc-experience-edit-form');
          if(!form)throw Error('Employment editor did not open');
          const deep=(root,selector)=>{
            const direct=root.querySelector(selector);if(direct)return direct;
            for(const e of root.querySelectorAll('*'))if(e.shadowRoot){const found=deep(e.shadowRoot,selector);if(found)return found}
          };
          const change=(field,month)=>{
            const picker=form.querySelector('oc-datepicker[formcontrolname="'+field+'"]');
            const input=picker&&deep(picker,'input');
            if(!input)throw Error('No date input for '+field);
            const value=month.slice(5)+'/'+month.slice(0,4);
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);
            input.dispatchEvent(new Event('input',{bubbles:true,composed:true}));
            input.dispatchEvent(new Event('change',{bubbles:true,composed:true}));
            input.dispatchEvent(new FocusEvent('blur',{bubbles:false,composed:true}));
            return input.value;
          };
          return {start:change('startDate',record.start),end:change('endDate',record.end)};
        }'''.replace('RECORD', json.dumps(record, ensure_ascii=False)), publication_id)
        expected = {'start': start[5:]+'/'+start[:4], 'end': end[5:]+'/'+end[:4]}
        if changed != expected:
            results.append({'organization': record['organization'], 'status': 'Date read-back mismatch; not saved', 'actual': changed})
            continue
        saved = evaluate(r'''() => {
          const form=window.__workerRecordEntry?.querySelector('oc-experience-edit-form');
          const button=form?.querySelector('[data-test="experience-save"] spl-button');
          if(!button||button.disabled||button.hasAttribute('disabled'))return false;
          button.click();return true;
        }''', publication_id)
        time.sleep(0.15)
        remaining = evaluate("() => Boolean(window.__workerRecordEntry?.querySelector('oc-experience-edit-form'))", publication_id)
        verified = False
        if saved and not remaining:
            evaluate("() => {window.__workerRecordEntry.querySelector('[data-test=\"experience-entry-edit\"]').click();return true}",publication_id)
            actual=evaluate(r'''() => {
              const form=window.__workerRecordEntry.querySelector('oc-experience-edit-form');
              const deep=(root,selector)=>{
                const direct=root.querySelector(selector);if(direct)return direct;
                for(const e of root.querySelectorAll('*'))if(e.shadowRoot){const found=deep(e.shadowRoot,selector);if(found)return found}
              };
              const read=field=>deep(form.querySelector('oc-datepicker[formcontrolname="'+field+'"]'),'input').value;
              return {start:read('startDate'),end:read('endDate')};
            }''',publication_id)
            verified = actual == expected
            evaluate("() => {window.__workerRecordEntry.querySelector('[data-test=\"experience-cancel\"] spl-button').click();return true}",publication_id)
        results.append({'organization': record['organization'], 'start': start, 'end': end,
                        'status': 'Saved and verified confirmed dates' if verified else 'Date verification incomplete; inspect editor'})
        if remaining:
            break
    return results


def repair_education(profile: dict, evaluate, publication_id: str) -> list[dict]:
    """Split merged degree entries and verify institution, degree, and dates."""
    results=[]
    for record in profile.get('education',[]):
        if not record.get('start') or not record.get('end'):
            results.append({'institution':record['institution'],'status':'Waiting for confirmed date range'})
            continue
        start,end=validate_month(record['start']),validate_month(record['end'])
        if start>end:
            raise ValueError('Education start date is after end date')
        opened=evaluate(r'''() => {
          if(!window.__applicationReviewGuard)throw Error('Submission guard missing');
          const record=RECORD, norm=s=>s.replace(/[’‘]/g,"'").replace(/\s+/g,' ').trim();
          const entries=[...document.querySelectorAll('oc-education-entry')];
          const name=e=>norm(e.querySelector('[data-test="education-entry-institution"]')?.childNodes[0]?.textContent||
            e.querySelector('oc-institution-autocomplete spl-autocomplete')?.value||'');
          let matches=entries.filter(e=>name(e)===norm(record.institution));
          if(!matches.length)matches=entries.filter(e=>name(e).startsWith(norm(record.institution)+' '));
          if(matches.length>1)throw Error('Ambiguous degree entry');
          if(matches.length){
            window.__workerEducationEntry=matches[0];
            const edit=matches[0].querySelector('[data-test="education-entry-edit"]');
            if(edit)edit.click();
            window.__workerEducationForm=matches[0].querySelector('oc-education-edit-form');
            return {created:false};
          }
          const pending=[...document.querySelectorAll('oc-education-edit-form')].filter(f=>
            f.querySelector('[data-test="education-major"] spl-input')?.value===record.degree);
          if(pending.length===1){window.__workerEducationForm=pending[0];window.__workerEducationEntry=pending[0].closest('oc-education-entry');return {created:true}}
          window.__workerOldEducationForms=[...document.querySelectorAll('oc-education-edit-form')];
          document.querySelector('[data-test="add-education"] spl-button').click();
          window.__workerEducationForm=null;
          return {created:true};
        }'''.replace('RECORD',json.dumps(record,ensure_ascii=False)),publication_id)
        time.sleep(0.15)
        evaluate(r'''() => {
          const record=RECORD;
          const form=window.__workerEducationForm||[...document.querySelectorAll('oc-education-edit-form')].find(f=>!window.__workerOldEducationForms.includes(f));
          if(!form)throw Error('Degree editor did not open');
          window.__workerEducationForm=form;
          const host=form.querySelector('oc-institution-autocomplete spl-autocomplete');
          window.__workerInstitutionMatches=host.value===record.institution;
          if(window.__workerInstitutionMatches)return true;
          const deep=root=>{const i=root.querySelector('input');if(i)return i;for(const e of root.querySelectorAll('*'))if(e.shadowRoot){const i=deep(e.shadowRoot);if(i)return i}};
          const input=deep(form.querySelector('oc-institution-autocomplete'));
          input.focus();input.click();Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,record.institution);
          input.dispatchEvent(new Event('input',{bubbles:true,composed:true}));
          return true;
        }'''.replace('RECORD',json.dumps(record,ensure_ascii=False)),publication_id)
        select_code=r'''() => {
          if(window.__workerInstitutionMatches)return true;
          const institution=INSTITUTION, found=[];
          const walk=root=>{for(const e of root.querySelectorAll('*')){
            if(e.getAttribute('role')==='option'&&e.getClientRects().length&&
               (e.getRootNode().host?.textContent||e.textContent).trim()===institution)found.push(e);
            if(e.shadowRoot)walk(e.shadowRoot);
          }};
          walk(window.__workerEducationForm);
          if(found.length!==1)return false;
          found[0].click();return true;
        }'''.replace('INSTITUTION',json.dumps(record['institution']))
        selected=False
        for _ in range(12):
            selected=evaluate(select_code,publication_id)
            if selected:
                break
            time.sleep(0.25)
        if not selected:
            results.append({'institution':record['institution'],'status':'No unique institution menu option; editor left open'})
            break
        time.sleep(0.15)
        changed=evaluate(r'''() => {
          const record=RECORD, form=window.__workerEducationForm;
          if(!form)throw Error('Degree editor did not open');
          window.__workerEducationEntry=form.closest('oc-education-entry');
          const deep=(root,selector)=>{
            const direct=root.querySelector(selector);if(direct)return direct;
            for(const e of root.querySelectorAll('*'))if(e.shadowRoot){const found=deep(e.shadowRoot,selector);if(found)return found}
          };
          const change=(root,value)=>{
            const input=deep(root,'input');if(!input)throw Error('Missing degree input');
            Object.getOwnPropertyDescriptor(HTMLInputElement.prototype,'value').set.call(input,value);
            input.dispatchEvent(new Event('input',{bubbles:true,composed:true}));
            input.dispatchEvent(new Event('change',{bubbles:true,composed:true}));
            input.dispatchEvent(new FocusEvent('blur',{bubbles:false,composed:true}));
            return input.value;
          };
          const institution=form.querySelector('oc-institution-autocomplete spl-autocomplete').value;
          const degree=change(form.querySelector('[data-test="education-degree"]'),record.degree);
          const major=change(form.querySelector('[data-test="education-major"]'),record.major||record.degree);
          const start=change(form.querySelector('[data-test="education-date-from"]'),record.start.slice(5)+'/'+record.start.slice(0,4));
          const end=change(form.querySelector('[data-test="education-date-to"]'),record.end.slice(5)+'/'+record.end.slice(0,4));
          return {institution,degree,major,start,end};
        }'''.replace('RECORD',json.dumps(record,ensure_ascii=False)),publication_id)
        saved=evaluate(r'''() => {
          const form=window.__workerEducationForm;
          const button=form?.querySelector('[data-test="education-save"] spl-button');
          if(!button||button.disabled||button.hasAttribute('disabled'))return false;
          button.click();return true;
        }''',publication_id)
        time.sleep(0.4)
        actual=evaluate(r'''() => {
          const institution=INSTITUTION;
          const matches=[...document.querySelectorAll('oc-education-entry')].filter(e=>
            e.querySelector('[data-test="education-entry-institution"]')?.childNodes[0]?.textContent.trim()===institution);
          const entry=matches.length===1?matches[0]:null;
          if(!entry)return {open:true};
          window.__workerEducationEntry=entry;
          return {open:false,institution:entry.querySelector('[data-test="education-entry-institution"]')?.childNodes[0]?.textContent.trim(),
             degree:entry.querySelector('[data-test="education-entry-degree"]')?.textContent.trim(),
             dates:entry.querySelector('[data-test="education-entry-date"]')?.textContent.trim()};
        }'''.replace('INSTITUTION',json.dumps(record['institution'])),publication_id)
        verified=False
        if saved and not actual.get('open'):
            evaluate("() => {window.__workerEducationEntry.querySelector('[data-test=\"education-entry-edit\"]').click();return true}",publication_id)
            months=evaluate(r'''() => {
              const form=window.__workerEducationEntry.querySelector('oc-education-edit-form');
              const deep=(root,selector)=>{const d=root.querySelector(selector);if(d)return d;for(const e of root.querySelectorAll('*'))if(e.shadowRoot){const r=deep(e.shadowRoot,selector);if(r)return r}};
              const stored=selector=>{const d=new Date(form.querySelector(selector+' spl-date-field').value);return d.getFullYear()+'-'+String(d.getMonth()+1).padStart(2,'0')};
              return {start:deep(form.querySelector('[data-test="education-date-from"]'),'input').value,
                end:deep(form.querySelector('[data-test="education-date-to"]'),'input').value,
                stored_start:stored('[data-test="education-date-from"]'),stored_end:stored('[data-test="education-date-to"]')};
            }''',publication_id)
            verified=(actual.get('institution')==record['institution'] and actual.get('degree')==record['degree'] and
                      months=={'start':start[5:]+'/'+start[:4],'end':end[5:]+'/'+end[:4],
                               'stored_start':start,'stored_end':end})
            actual['verified_dates']=months
            evaluate("() => {window.__workerEducationEntry.querySelector('[data-test=\"education-cancel\"] spl-button').click();return true}",publication_id)
        results.append({'institution':record['institution'],'start':start,'end':end,
                        'status':'Saved and verified degree record' if verified else 'Degree editor needs review',
                        'created':opened['created'],'actual':actual,'typed_values':changed})
        if actual.get('open'):
            break
    return results
