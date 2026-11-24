"""Audit retired run evidence after the original site is no longer available."""
import json
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import retry_evidence as retry, round_archive, round_evidence as ev


class RetrySnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name).resolve()
        self.site = self.base / 'site'
        self.site.mkdir()
        self.left, self.right = (self.site / 'a', self.site / 'b')
        self.pairs = {'pairs': [{'baseline': 'a', 'candidate': 'b', 'scenario': 'q'}], 'schema_version': 1}
        schedule = self.site / 'pairs.json'
        schedule.write_text(json.dumps(self.pairs))
        self.attempt = {'paths': sorted(map(str, (self.left, self.right))), 'stage': 'assess', 'binding': 'phase-id',
                        'reason': 'worker stopped', 'error_type': 'ValueError', 'error': 'fixture error',
                        'schedule': {'path': str(schedule), 'sha256': ev.file_hash(schedule)}}

    def capture(self):
        ref = retry.capture(self.site, self.attempt, self.pairs)
        self.attempt['snapshot'] = ref
        row = {'acceptance_protocol': {'require_retry_snapshot': True}, 'evidence': {
            'invalid_attempts': [self.attempt], 'profiles': {}, 'assessments': [],
            'evidence_files': [self.attempt['schedule'], ref]}}
        archived = round_archive.ArchivedFiles(self.base / 'bundle')
        for ref in round_archive.reference_paths(row, archived):
            dest = archived.destination(ref['path'])
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ref['path'], dest)
        self.site.rename(self.base / 'hidden')
        return row, archived

    def test_unavailable_files_are_scoped_and_links_remain_unfollowed(self):
        self.left.mkdir()
        (self.left / 'manifest.started.json').write_text('{"provisional":true}')
        self.right.symlink_to(self.left, target_is_directory=True)
        row, files = self.capture()
        report, = retry.review(row, files)
        self.assertEqual(report['verification'], 'recorded_missing')
        self.assertIn('manifest.json', report['missing'][str(self.right)])
        inventory = ev.read_object(files(self.attempt['snapshot']['path']))
        self.assertEqual(inventory['slots'][1]['unavailable'], {'.': 'symlink'})
        self.assertEqual(inventory['slots'][1]['files'], {})
        frozen = files(next(retry.files(self.attempt['snapshot'], files))['path'])
        frozen.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            retry.review(row, files)

    def test_invalid_existing_bytes_independently_reproduce_across_moves(self):
        self.right.mkdir()
        (self.right / 'metadata.json').write_text('{"broken":')
        row, files = self.capture()
        report, = retry.review(row, files)
        self.assertEqual((report['verification'], report['criterion'], report['slot']),
                         ('reproduced', 'invalid_run_evidence', 1))
        self.assertNotIn(str(self.base), report['detail'])

    def test_registered_snapshot_requires_the_attempt_in_the_export(self):
        row, files = self.capture()
        row["evidence"]["invalid_attempts"] = []
        with self.assertRaisesRegex(ValueError, "snapshot list"):
            retry.review(row, files)

    def test_legacy_retry_is_explicit_and_new_round_needs_snapshot(self):
        row = {'evidence': {'invalid_attempts': [self.attempt]}}
        self.assertEqual(retry.review(row)[0]['verification'], 'unverified')
        row['acceptance_protocol'] = {'require_retry_snapshot': True}
        with self.assertRaisesRegex(ValueError, 'required evidence'):
            retry.review(row)


if __name__ == '__main__':
    unittest.main()
