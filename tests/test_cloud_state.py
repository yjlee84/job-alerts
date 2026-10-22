import io
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from cryptography.fernet import Fernet,InvalidToken
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import cloud_state
from application_store import ApplicationStore

class CloudStateTests(unittest.TestCase):
    def test_restore_preserves_manual_tracker_edits_and_unpublished_receipts(self):
        with tempfile.TemporaryDirectory() as remote,tempfile.TemporaryDirectory() as local:
            remote=Path(remote);local=Path(local);key=Fernet.generate_key()
            for root,content in [(remote,'Job ID,Status,Next Action\na,Review,Apply\nb,Applied,Await response\n'),
                                 (local,'Job ID,Status,Next Action\na,Applied,Manually applied\nb,Review,Apply\n')]:
                (root/'reports').mkdir();(root/'reports/job_tracker.csv').write_text(content)
            payload=cloud_state.pack(remote,key)
            backend=cloud_state.GitHubState('owner/repo',key,'test-token',local)
            with patch.object(backend,'api',return_value={'sha':'state-sha','content':__import__('base64').b64encode(payload).decode()}):
                backend.restore()
            import csv
            with (local/'reports/job_tracker.csv').open() as f:rows={r['Job ID']:r for r in csv.DictReader(f)}
            self.assertEqual(rows['a']['Status'],'Applied');self.assertEqual(rows['a']['Next Action'],'Manually applied')
            self.assertEqual(rows['b']['Status'],'Applied')

    def test_encrypted_roundtrip_preserves_outputs_tracker_and_private_ledger(self):
        with tempfile.TemporaryDirectory() as source,tempfile.TemporaryDirectory() as dest:
            root=Path(source)
            files={'reports/job_tracker.csv':'Job ID,Status\na,Applied\n','outputs/a/resume.pdf':'%PDF-tailored',
                   'outputs/a/job_description.txt':'Job description','.local/profile/applicant.json':'{"personal":"private"}',
                   '.local/applications/a/submission.json':'{"status":"confirmed"}',
                   '.env':'secret API key','.local/cloud/state-key':'must not be in archive'}
            for name,content in files.items():p=root/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(content)
            key=Fernet.generate_key();payload=cloud_state.pack(root,key)
            self.assertNotIn(b'private',payload);cloud_state.unpack(payload,Path(dest),key)
            for name in list(files)[:5]:self.assertEqual((Path(dest)/name).read_text(),files[name])
            self.assertFalse((Path(dest)/'.env').exists());self.assertFalse((Path(dest)/'.local/cloud/state-key').exists())
            with self.assertRaises(InvalidToken):cloud_state.unpack(payload,Path(dest),Fernet.generate_key())

    def test_authenticated_but_unsafe_archive_is_rejected_before_any_write(self):
        key=Fernet.generate_key();buffer=io.BytesIO()
        with tarfile.open(fileobj=buffer,mode='w:gz') as archive:
            for name in ('outputs/a/job_description.txt','../escape'):
                info=tarfile.TarInfo(name);info.size=1;archive.addfile(info,io.BytesIO(b'x'))
        with tempfile.TemporaryDirectory() as root:
            with self.assertRaises(ValueError):cloud_state.unpack(Fernet(key).encrypt(buffer.getvalue()),Path(root),key)
            self.assertFalse((Path(root)/'outputs').exists())

    def test_missing_cloud_checkpoint_blocks_side_effects(self):
        with patch.dict(os.environ,{'JOB_ALERTS_CLOUD':'1'}),patch.object(cloud_state,'_backend',None):
            with self.assertRaises(RuntimeError):cloud_state.checkpoint()

    def test_submission_intent_requires_acknowledged_cloud_checkpoint(self):
        with tempfile.TemporaryDirectory() as root:
            root=Path(root);(root/'reports').mkdir();(root/'reports/job_tracker.csv').write_text('Job ID,Status\na,Review\n')
            output=root/'outputs/a';output.mkdir(parents=True);(output/'resume.pdf').write_bytes(b'%PDF-a')
            backend=unittest.mock.Mock();backend.checkpoint.side_effect=OSError('network unavailable')
            with patch.object(cloud_state,'_backend',backend):
                store=ApplicationStore('a',root)
                with self.assertRaises(OSError):store.begin({'job_title':'Job','job_url':'https://example.test/a','description':'JD'},{'final_client_valid':True})
                self.assertEqual(json.loads(store.path.read_text())['status'],'submitting')
                backend.checkpoint.assert_called_once()

if __name__=='__main__':unittest.main()
