import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from application_policy import Budget, request_allowed, validate_job_url
from fill_application import SNAPSHOT, SCAN_FIELDS, known_answer, fill_field, is_hiring_message, draft_answers
from safari_application import SAFARI_GUARD
from application_records import validate_month, repair_education, repair_employment
from safari_resume import validate_upload_endpoint
from safari_validation import CHECK_PAGE


class ApplicationPolicyTests(unittest.TestCase):
    def test_upload_allowlist_requires_exact_observed_resume_endpoint(self):
        url='https://jobs.smartrecruiters.com/oneclick-ui/api/company/633d7de35395245472b38a43/attachment/resume?filename=test.pdf'
        observed=[{'method':'POST','bodyType':'FormData','url':url.removeprefix('https://jobs.smartrecruiters.com')}]
        validate_upload_endpoint(url,observed)
        for unsafe in (url.replace('/attachment/resume','/application/submit'),url.replace('jobs.smartrecruiters.com','attacker.test')):
            with self.assertRaises(ValueError):validate_upload_endpoint(unsafe,observed)
        with self.assertRaises(ValueError):validate_upload_endpoint(url,[])
    def test_missing_record_dates_are_not_invented(self):
        with patch('application_records.time.sleep'), patch('builtins.print'):
            evaluator=unittest.mock.Mock(side_effect=AssertionError('No browser mutation should occur'))
            self.assertIn('Waiting',repair_education({'education':[{'institution':'Example','start':None,'end':'2026-09'}]},evaluator,'test')[0]['status'])
            self.assertIn('Waiting',repair_employment({'employment':[{'organization':'Example','start':None,'end':None}]},evaluator,'test')[0]['status'])
            evaluator.assert_not_called()

    def test_invalid_or_reversed_record_dates_stop_before_browser_changes(self):
        for value in ('2026','2026-13','09/2026',None):
            with self.assertRaises(ValueError):validate_month(value)
        evaluator=unittest.mock.Mock(side_effect=AssertionError('No browser mutation should occur'))
        with self.assertRaises(ValueError):
            repair_education({'education':[{'institution':'Example','start':'2026-09','end':'2024-09'}]},evaluator,'test')
        evaluator.assert_not_called()

    def test_no_application_write_can_leave_browser(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            for url in ("https://jobs.smartrecruiters.com/applications", "https://example.org/upload", "https://example.org/graphql"):
                self.assertFalse(request_allowed(method, url))
        self.assertFalse(request_allowed("GET", "https://jobs.smartrecruiters.com/submit"))
        self.assertTrue(request_allowed("GET", "https://jobs.smartrecruiters.com/oneclick-ui/company/OECD/publication/123"))
        with self.assertRaises(ValueError):
            validate_job_url("https://jobs.smartrecruiters.com.attacker.test/OECD/123")

    def test_budget_survives_failure_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "usage.json"
            body = {"model": Budget.MODEL, "max_output_tokens": 1600, "input": "short"}
            Budget(path, 0.03).reserve(body)
            restarted = Budget(path, 0.03)
            self.assertGreater(restarted.spent, 0)
            with self.assertRaises(RuntimeError):
                restarted.reserve({**body, "input": "x" * 100000})
            restarted.settle(0, {"input_tokens": 1000, "output_tokens": 200})
            self.assertAlmostEqual(Budget(path).spent, 0.00165)
            with self.assertRaises(ValueError):
                restarted.reserve({**body, "model": "unknown-model"})

    def test_does_not_infer_eligibility_or_sensitive_answers(self):
        profile = {"personal": {"first_name": "Yongjai", "nationality": "Canada"},
                   "preferences": {"work_authorization": True, "relocation": True}}
        def field(label, type="text"):
            return {"label": label, "type": type}
        self.assertEqual(known_answer(field("First name *"), profile)[0], "Yongjai")
        self.assertIsNone(known_answer(field("Do you have a French work visa?"), profile))
        self.assertIsNone(known_answer(field("Are you an internal candidate?"), profile))
        self.assertIsNone(known_answer(field("Do you have four years of export credit experience?"), profile))
        self.assertIsNone(known_answer(field("Consent to privacy policy", "checkbox"), profile))

    def test_confirmed_sensitive_answers_are_used_without_guessing(self):
        profile = {"personal": {"birth_date": "1985-02-03", "gender": "Male"},
                   "qualifications": {"highest_degree": "Master's degree", "has_second_nationality": False,
                                      "oecd_official_staff_member": False}, "preferences": {}}
        for label, answer in [("Date of birth", "1985-02-03"), ("Gender", "Male"),
                              ("Highest degree", "Master's degree"), ("Second nationality", "None")]:
            self.assertEqual(known_answer({"label": label, "type": "text"}, profile)[0], answer)
        self.assertIsNone(known_answer({"label": "Do you have a work permit?", "type": "text"}, profile))
        self.assertTrue(is_hiring_message({'label':'Let the company know about your interest working there'}))

    def test_existing_signed_message_and_references_do_not_call_gpt(self):
        profile={'personal':{},'preferences':{},'reference_text':'Reference details copied as provided',
                 'writing':{'signature':'Signature text'}}
        fields=[{'key':'message','type':'textarea','label':'Let the company know about your interest working there',
                 'value':'Existing concise message\n\nSignature text'},
                {'key':'refs','type':'textarea','label':'Please provide the details of at least three most recent professional references.',
                 'value':''}]
        with tempfile.TemporaryDirectory() as folder, patch('fill_application.urlopen') as api:
            dest=Path(folder)
            budget=Budget(dest/'usage.json')
            self.assertEqual(draft_answers(fields,profile,{},budget,dest),{})
            api.assert_not_called()
            self.assertEqual(budget.spent,0)


class BrowserTests(unittest.TestCase):
    def test_client_validation_detects_invalid_fields_without_submitting(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            page=browser.new_page()
            page.set_content('<input id="email" type="email" required><button id="submit" type="button">Submit</button>')
            page.evaluate('() => {document.querySelector("#submit").onclick=()=>window.submitted=true;}')
            result=page.evaluate(CHECK_PAGE)
            self.assertEqual(len(result['native']),1)
            page.locator('#email').fill('applicant@example.org')
            result=page.evaluate(CHECK_PAGE)
            self.assertEqual(result['native'],[])
            self.assertEqual(result['submit_controls'],[{'disabled':False,'label':'Submit'}])
            self.assertFalse(page.evaluate('Boolean(window.submitted)'))
            browser.close()
    def test_safari_guard_blocks_final_actions_and_network_writes(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            requests = []
            page.on('request', lambda request: requests.append(request.method))
            page.set_content('<form><input name="name"><button type="submit" id="final">Submit application</button></form>')
            page.evaluate(SAFARI_GUARD)
            page.evaluate("document.querySelector('#final').addEventListener('click',()=>window.sent=true)")
            page.locator('#final').click()
            self.assertFalse(page.evaluate('Boolean(window.sent)'))
            self.assertFalse(page.evaluate("document.querySelector('form').dispatchEvent(new Event('submit',{cancelable:true,bubbles:true}))"))
            self.assertTrue(page.evaluate("async()=>{try{await fetch('https://example.org/submit',{method:'POST',body:'test'});return false}catch{return true}}"))
            self.assertTrue(page.evaluate("()=>{try{document.querySelector('form').submit();return false}catch{return true}}"))
            page.evaluate("""() => {
              const host=document.createElement('spl-button');host.textContent='Submit';
              const root=host.attachShadow({mode:'open'});root.innerHTML='<button><slot></slot></button>';
              document.body.appendChild(host);host.addEventListener('click',()=>window.shadowSent=true);
              root.querySelector('button').click();
            }""")
            self.assertFalse(page.evaluate('Boolean(window.shadowSent)'))
            self.assertNotIn('POST', requests)
            browser.close()

    def test_submission_guard_permits_one_exact_authorized_request(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser=pw.chromium.launch();page=browser.new_page();posts=[]
            def handle(route):
                if route.request.method=='POST':posts.append(route.request.url)
                route.fulfill(status=200,content_type='text/html' if route.request.method=='GET' else 'application/json',body='<p>Test</p>' if route.request.method=='GET' else '{}')
            page.route('**/*',handle)
            page.goto('https://jobs.smartrecruiters.com/oneclick-ui/company/OECD/publication/test')
            page.evaluate(SAFARI_GUARD)
            result=page.evaluate("""async() => {
              const endpoint=location.origin+'/oneclick-ui/api/company/OECD/publication/test/application';
              window.__workerSubmission={mode:'submit',endpoint,expires:Date.now()+30000};
              let wrong=false,repeat=false;
              try{await fetch(endpoint+'/other',{method:'POST'})}catch{wrong=true}
              const first=await fetch(endpoint,{method:'POST'});
              try{await fetch(endpoint,{method:'POST'})}catch{repeat=true}
              return {wrong,repeat,status:first.status};
            }""")
            self.assertEqual(result,{'wrong':True,'repeat':True,'status':200})
            self.assertEqual(len(posts),1)
            browser.close()

    def test_scanner_reads_nested_component_labels_and_selected_tags(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser=pw.chromium.launch()
            page=browser.new_page()
            page.set_content('<spl-multiselect-autocomplete aria-label="Specialised education"></spl-multiselect-autocomplete>')
            page.evaluate("""() => {
              const host=document.querySelector('spl-multiselect-autocomplete');
              host.attachShadow({mode:'open'}).innerHTML='<spl-tags-list></spl-tags-list><input type="text">';
              host.shadowRoot.querySelector('spl-tags-list').attachShadow({mode:'open'}).innerHTML='<spl-tag label="Applied Economics"></spl-tag>';
            }""")
            fields=page.evaluate(SCAN_FIELDS)
            self.assertEqual(len(fields),1)
            self.assertEqual(fields[0]['label'],'Specialised education')
            self.assertEqual(fields[0]['value'],'Applied Economics')
            self.assertTrue(fields[0]['multiple'])
            browser.close()

    def test_fill_readback_and_inert_snapshot(self):
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()
            page.set_content('''<html><head></head><body><form action="https://example.org/submit">
                <label>First name<input data-worker-key="name"></label>
                <textarea data-worker-key="message"></textarea>
                <select data-worker-key="country"><option value="">Choose</option><option value="FR">France</option></select>
                <script>window.original=true</script><button type="submit">Submit</button></form></body></html>''')
            base = {"frame": 0, "options": [], "max_length": None}
            self.assertTrue(fill_field(page, {**base, "key": "name", "type": "text"}, "Yongjai")[0])
            self.assertTrue(fill_field(page, {**base, "key": "message", "type": "textarea"}, "A factual answer")[0])
            self.assertTrue(fill_field(page, {**base, "key": "country", "type": "select-one",
                "options": [{"value": "FR", "label": "France"}]}, "France")[0])
            snapshot = page.evaluate(SNAPSHOT)
            self.assertNotIn('<script', snapshot)
            self.assertNotIn('action="https:', snapshot)
            self.assertIn('value="Yongjai"', snapshot)
            self.assertIn('A factual answer', snapshot)
            self.assertIn("form-action 'none'", snapshot)
            browser.close()


if __name__ == "__main__":
    unittest.main()
