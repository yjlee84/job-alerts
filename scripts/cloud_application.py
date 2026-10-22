"""Run the verified OECD form steps in a normal Playwright cloud browser.

No stealth plugins, identity spoofing, proxy rotation or CAPTCHA bypasses.
An access restriction stops this job and produces a needs-attention result.
"""
import json
import os
import re
import time
from contextlib import redirect_stdout
from types import SimpleNamespace
from urllib.parse import urljoin,urlparse

from playwright.sync_api import sync_playwright
from fill_application import ROOT,LOCAL,read_job
from portal_session import use_evaluator
from application_store import ApplicationStore,save,now
from cloud_state import checkpoint

class CloudBrowser:
    def __enter__(self):
        self.playwright=sync_playwright().start()
        self.browser=self.playwright.chromium.launch(headless=os.environ.get('BROWSER_HEADLESS','1')!='0')
        state=LOCAL/'cloud-browser.json'
        self.context=self.browser.new_context(storage_state=str(state) if state.exists() else None)
        self.page=self.context.new_page();self.sent=False;self.job_id=None
        self.context.route('**/oneclick-ui/api/**',self.route)
        return self

    def __exit__(self,*exc):
        try:self.context.storage_state(path=str(LOCAL/'cloud-browser.json'))
        finally:self.browser.close();self.playwright.stop()

    def route(self,route):
        request=route.request;url=urlparse(request.url)
        if request.method in {'GET','HEAD','OPTIONS'}:return route.continue_()
        if url.hostname!='jobs.smartrecruiters.com':return route.abort()
        if re.fullmatch(r'/oneclick-ui/api/company/[a-f0-9]{24}/attachment/resume',url.path) and request.method=='POST':
            allowed=self.page.evaluate('() => window.__reviewUploadEndpoint||null')
            if request.url==allowed:return route.continue_()
        expected='/oneclick-ui/api/company/OECD/publication/'+getattr(self,'pid','')+'/application'
        if request.method=='POST' and url.path==expected and self.job_id and not self.sent:
            state=ApplicationStore(self.job_id)
            armed=self.page.evaluate('() => window.__workerSubmission?.mode==="submit"&&window.__workerSubmission?.sent===true')
            if state.state['status']=='submitting' and armed:
                # A second independent check at the browser network boundary.
                checkpoint();self.sent=True;return route.continue_()
        return route.abort()

    def evaluate(self,code,pid):
        url=urlparse(self.page.url)
        if url.hostname!='jobs.smartrecruiters.com' or pid not in url.path:
            raise RuntimeError('Cloud browser is not on the selected application')
        return self.page.evaluate(code)

    def visit(self,url):
        parsed=urlparse(url)
        if parsed.scheme!='https' or parsed.hostname!='jobs.smartrecruiters.com':raise ValueError('Unsupported application origin')
        response=self.page.goto(url,wait_until='domcontentloaded',timeout=60000)
        self.page.wait_for_timeout(3000)
        text=self.page.locator('body').inner_text()
        restricted=(response and response.status in {401,403,429} or
                    re.search(r'access temporarily restricted|verify (?:that )?you are human|confirm you are human|please enable js and disable any ad blocker',text,re.I) or
                    any('captcha-delivery.com' in f.url for f in self.page.frames))
        if restricted:
            save(LOCAL/'cloud-probe.json',{'status':'blocked','at':now(),'http_status':response.status if response else None,'url':self.page.url,'title':self.page.title()})
            raise RuntimeError('SmartRecruiters restricted the cloud browser. No application was submitted; manual verification or another supported hosting setup is needed.')

    def ready(self):
        self.page.locator('oc-personal-information').wait_for(state='visible',timeout=30000)

def probe(url):
    with CloudBrowser() as session:
        session.visit(url);session.ready()
        result={'status':'ready','at':now(),'url':session.page.url,'form_visible':True,'submitted':False}
        save(LOCAL/'cloud-probe.json',result)
    return result

def fill_and_submit(job_id):
    import safari_application as application
    import safari_resume as resume
    import safari_validation as validation
    import safari_worker as worker
    job=read_job(job_id);dest=LOCAL/'applications'/job_id/'safari';dest.mkdir(parents=True,exist_ok=True)
    publication=dest.parent/'publication.json'
    with (dest.parent/'cloud-steps.log').open('w') as log,redirect_stdout(log),CloudBrowser() as session,use_evaluator(session.evaluate):
        if publication.exists():
            info=json.loads(publication.read_text());pid=info['publication_id'];url=info['url']
        else:
            session.visit(job['job_url'])
            numeric=re.search(r'/OECD/(\d+)',job['job_url'])[1]
            info=worker.discover(numeric,job_id);pid=info['publication_id'];url=info['url']
        session.pid=pid;session.job_id=job_id;session.visit(url);session.ready()
        # Permit the site's normal telemetry and integrity services. Actual
        # application/upload writes remain gated independently at both layers.
        session.page.evaluate('() => {window.__workerAllowSiteServices=true}')
        args=SimpleNamespace(job_id=job_id,publication_id=pid,open=False,back=False,
                             profile=str(LOCAL/'profile/applicant.json'),budget_usd=.03,
                             repair_records=False,repair_education=False,fill=False,
                             refresh_confirmed=True,regenerate_message=False,no_gpt=False)
        application.run(args)
        worker.prepare_records(pid,job_id)
        args.fill=True;application.run(args)
        worker.city(pid);args.fill=False;application.run(args)
        resume_args=SimpleNamespace(job_id=job_id,publication_id=pid,endpoint=None)
        resume.upload(resume_args)
        observed=json.loads((dest/'resume-upload.json').read_text())['blocked']
        endpoints={urljoin('https://jobs.smartrecruiters.com',r.get('url','')) for r in observed
                   if r.get('method')=='POST' and r.get('bodyType')=='FormData' and '/attachment/resume?' in r.get('url','')}
        if len(endpoints)!=1:raise RuntimeError('No unique verified resume upload endpoint')
        resume_args.endpoint=endpoints.pop();resume.upload(resume_args)
        val_args=SimpleNamespace(job_id=job_id,publication_id=pid,next=True,check_privacy=True)
        validation.run(val_args)
        args.fill=True;application.run(args)
        val_args.next=False;validation.run(val_args)
        if not json.loads((dest/'validation.json').read_text()).get('final_client_valid'):
            raise RuntimeError('The cloud form has missing answers or validation errors; no submission was attempted')
        worker.probe_submit(pid,job_id)
        return worker.submit(pid,job_id)
