import csv
import json
import sys
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from application_store import ApplicationStore,posting_key

class DurableSubmissionTests(unittest.TestCase):
    def test_tracking_parameters_and_slug_do_not_change_posting_identity(self):
        self.assertEqual(posting_key('https://jobs.smartrecruiters.com/OECD/744123-policy-analyst?trid=one'),
                         posting_key('https://jobs.smartrecruiters.com/OECD/744123-new-title?trid=two'))
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        output=self.root/'outputs/job1';output.mkdir(parents=True)
        (output/'resume.pdf').write_bytes(b'%PDF-tailored')
        (output/'cover_letter.pdf').write_bytes(b'%PDF-letter')
        (self.root/'reports').mkdir()
        with (self.root/'reports/job_tracker.csv').open('w',newline='') as f:
            w=csv.DictWriter(f,fieldnames=['Job ID','Status','Application Date','Next Action','Description']);w.writeheader()
            w.writerow({'Job ID':'job1','Status':'Review','Description':'Original JD'})
        self.job={'job_title':'Policy Analyst','job_url':'https://example.test/job','description':'Original JD'}
    def store(self):return ApplicationStore('job1',self.root)
    def test_intent_survives_crash_and_refuses_retry(self):
        self.store().begin(self.job,{'final_client_valid':True})
        restarted=self.store()
        self.assertEqual(restarted.state['status'],'submitting')
        with self.assertRaises(RuntimeError):restarted.begin(self.job,{'final_client_valid':True})
        self.assertIn('Review',(self.root/'reports/job_tracker.csv').read_text())
    def test_uncertain_attempt_cannot_mark_applied_or_retry(self):
        store=self.store();store.begin(self.job,{'final_client_valid':True});store.uncertain('timeout')
        with self.assertRaises(ValueError):store.update_tracker()
        with self.assertRaises(RuntimeError):self.store().begin(self.job,{'final_client_valid':True})
    def test_confirmed_receipt_updates_tracker_and_preserves_archive(self):
        store=self.store();store.begin(self.job,{'final_client_valid':True})
        with self.assertRaises(ValueError):store.confirm({'confirmed':False})
        store.confirm({'confirmed':True,'message':'Your application has been submitted'})
        store.update_tracker() # Receipt replay recovers a tracker-write interruption.
        with (self.root/'reports/job_tracker.csv').open() as f:rows=list(csv.DictReader(f))
        self.assertEqual(rows[0]['Status'],'Applied');self.assertTrue(rows[0]['Application Date'])
        self.assertEqual((store.directory/'interview/job_description.txt').read_text(),'Original JD')
        self.assertEqual((store.directory/'interview/materials/resume.pdf').read_bytes(),b'%PDF-tailored')
        self.assertFalse(store.state['notification_sent'])
    def test_invalid_validation_never_creates_submission_intent(self):
        with self.assertRaises(ValueError):self.store().begin(self.job,{'final_client_valid':False})
        self.assertFalse(self.store().path.exists())
    def test_project_confirmation_emails_are_disabled(self):
        store=self.store();store.begin(self.job,{'final_client_valid':True});store.confirm({'confirmed':True})
        with patch.dict('os.environ',{'JOB_ALERTS_CONFIRMATION_EMAILS':'0'}),patch('application_store.smtplib.SMTP') as smtp:
            self.assertFalse(store.notify());smtp.assert_not_called()
        self.assertEqual(self.store().state['notification_status'],'disabled')
    def test_receipt_replay_preserves_later_manual_tracker_progress(self):
        store=self.store();store.begin(self.job,{'final_client_valid':True});store.confirm({'confirmed':True})
        path=self.root/'reports/job_tracker.csv'
        with path.open() as f:reader=csv.DictReader(f);fields=reader.fieldnames;rows=list(reader)
        rows[0].update({'Status':'Interview','Next Action':'Prepare presentation'})
        with path.open('w',newline='') as f:w=csv.DictWriter(f,fieldnames=fields);w.writeheader();w.writerows(rows)
        store.update_tracker()
        with path.open() as f:row=next(csv.DictReader(f))
        self.assertEqual(row['Status'],'Interview');self.assertEqual(row['Next Action'],'Prepare presentation')

if __name__=='__main__':unittest.main()
