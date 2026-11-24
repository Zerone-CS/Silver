"""Verify deployment and warmup identities using only local fixture files."""
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts import ascend_910c as host
from scripts import site_910c as worker
from scripts import site_model_snapshot as snapshot
from scripts import round_evidence


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.model = self.base / 'source'
        self.model.mkdir()
        self.shard = self.model / 'weights-00001.safetensors'
        self.shard.write_bytes(b'first version')
        blob = self.base / 'blob'
        blob.write_bytes(b'linked tokenizer data')
        (self.model / 'tokenizer.json').symlink_to(blob)
        self.store = self.base / 'store'
        self.receipt = self.base / 'model-snapshot.json'

    def freeze(self, pins=None):
        return snapshot.freeze(self.model, self.store, self.receipt, pins or {})

    def test_source_mutation_cannot_change_a_later_deployment(self):
        record = self.freeze({'tokenizer.json': host.sha(self.model / 'tokenizer.json')})
        mount = self.store / record['content_sha256']
        self.assertFalse((mount / self.shard.name).samefile(self.shard))
        self.assertFalse((mount / 'tokenizer.json').is_symlink())
        self.shard.write_bytes(b'another value')
        self.assertEqual(self.freeze(), record)
        self.assertEqual(snapshot.verify(self.store, record, mount), mount)
        self.assertEqual((mount / self.shard.name).read_bytes(), b'first version')
        with self.assertRaisesRegex(ValueError, 'mount differs'):
            snapshot.verify(self.store, record, self.model)

    def test_new_site_reuses_verified_content_and_detects_modified_snapshot(self):
        record = self.freeze()
        reused = snapshot.freeze(self.model, self.store, self.base / 'second.json', {})
        self.assertEqual(record, reused)
        target = self.store / record['content_sha256'] / self.shard.name
        target.chmod(0o644)
        target.write_bytes(b'other version')
        with self.assertRaisesRegex(ValueError, 'snapshot changed'):
            self.freeze()

    def test_delete_add_and_legacy_identity_mismatch_fail(self):
        with self.assertRaisesRegex(ValueError, 'pinned site'):
            self.freeze({'tokenizer.json': '0' * 64})
        self.assertFalse(self.receipt.exists())
        record = self.freeze()
        target = self.store / record['content_sha256']
        target.chmod(0o755)
        (target / 'new-config.json').write_bytes(b'{}')
        with self.assertRaisesRegex(ValueError, 'file list'):
            snapshot.verify(self.store, record)

    def test_multiple_shards_and_normalized_store_paths(self):
        nested = self.model / 'weights'
        nested.mkdir()
        (nested / 'part.safetensors').write_bytes(b'shard')
        record = self.freeze()
        self.assertIn('weights/part.safetensors', record['files_sha256'])
        with self.assertRaisesRegex(ValueError, 'outside'):
            snapshot.freeze(self.model, self.model / 'nested', self.base / 'bad.json', {})


class WarmupTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.formal = self.base / 'formal.jsonl'
        self.separate = self.base / 'independent.jsonl'
        for file in (self.formal, self.separate):
            file.write_text(''.join(json.dumps({'request_id': f'{file.stem}-{i}', 'prompt_text': f'{file.stem}-{i}',
                                                'output_tokens': 7}) + '\n' for i in range(4)))
        self.spec = self.base / 'cases.json'

    def settings(self, **extras):
        doc = {'dataset': self.formal.name, 'concurrency': 2, **extras}
        self.spec.write_text(json.dumps({'schema_version': 1, 'scenarios': {'s': doc}}))
        return worker.parse_case(self.spec, 's')

    def run_warmup(self, **options):
        config, rounds, source = self.settings(**options)
        live = SimpleNamespace(directory=self.base, site=SimpleNamespace(model='fixture', base_url='unused'))
        live.warmup = lambda *args, **kwargs: host.Live.warmup(live, *args, **kwargs)
        with patch.object(host, 'http', return_value={'model': 'fixture', 'id': 'test',
                                                      'choices': [{'finish_reason': 'stop'}]}) as request:
            policy, artifacts = worker.prepare_warmup(live, config, rounds, source)
        return policy, artifacts, [c.args[2]['messages'][0]['content'] for c in request.call_args_list]

    def test_legacy_inputs_select_the_first_concurrency_requests(self):
        policy, _, prompts = self.run_warmup()
        self.assertEqual(policy['mode'], 'measured_first_concurrency')
        self.assertCountEqual(prompts, ['formal-0', 'formal-1'])

    def test_configured_dataset_exercises_every_row_and_has_content_identity(self):
        ref = {'path': self.separate.name, 'sha256': host.sha(self.separate)}
        policy, files, prompts = self.run_warmup(warmup_dataset=ref, warmup_rounds=2)
        self.assertEqual(policy['dataset_sha256'], ref['sha256'])
        self.assertEqual(policy['rounds'], 2)
        self.assertCountEqual(prompts, [f'independent-{i}' for i in range(4)] * 2)
        self.assertEqual(host.sha(self.base / files[0]), ref['sha256'])
        self.assertEqual(host.read(self.base / files[-1])['policy'], policy)

    def test_declared_prefix_overlap_is_checked_before_warmup(self):
        jobs = [json.loads(line) for line in self.formal.read_text().splitlines()]
        jobs[1]['prompt_text'] = 'independent-2 plus more'
        self.formal.write_text(''.join(json.dumps(job) + '\n' for job in jobs))
        rule = {'characters': 13, 'matching_requests': 1}
        ref = {'path': self.separate.name, 'sha256': host.sha(self.separate), 'shared_prefix': rule}
        config, rounds, source = self.settings(warmup_dataset={**ref, 'shared_prefix': {**rule, 'matching_requests': 4}})
        live = SimpleNamespace(directory=self.base, warmup=Mock())
        with self.assertRaisesRegex(ValueError, 'prefix count'):
            worker.prepare_warmup(live, config, rounds, source)
        live.warmup.assert_not_called()
        (self.base / 'warmup-input.jsonl').unlink()
        policy, _, _ = self.run_warmup(warmup_dataset=ref)
        self.assertEqual(policy['shared_prefix'], {**rule, 'measured_requests': len(jobs)})

    def test_offline_verification_checks_model_and_warmup_evidence_together(self):
        ref = {'path': self.separate.name, 'sha256': host.sha(self.separate)}
        policy, artifacts, _ = self.run_warmup(warmup_dataset=ref)
        model_files = {'tokenizer.json': '1' * 64}
        digest = snapshot.content_id(model_files)
        model = self.base / 'model-snapshot.json'
        host.write(model, {'files_sha256': model_files, 'content_sha256': digest})
        files = [self.base / name for name in artifacts] + [model]
        index = {str(file): host.sha(file) for file in files}
        obs = {'model_snapshot_sha256': digest}
        inputs = {'environment': {'model_snapshot_sha256': digest, 'model_hashes': model_files},
                  'metric_contract': {'site_warmup_policy': policy}}

        def verify():
            round_evidence.verify_site_inputs(self.base, inputs, self.base / 'observation.json', obs, files, index)
        verify()
        obs['model_snapshot_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'model snapshot'):
            verify()
        obs['model_snapshot_sha256'] = digest
        receipt = self.base / 'warmup-receipt.json'
        data = host.read(receipt)
        data['round_results'][0][0]['finish_reason'] = None
        receipt.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'incomplete response'):
            verify()
        data['round_results'][0][0]['finish_reason'] = 'stop'
        data['round_results'][0].pop()
        receipt.write_text(json.dumps(data))
        with self.assertRaisesRegex(ValueError, 'schedule'):
            verify()

    def test_cold_sends_no_workload_requests_and_has_evidence(self):
        policy, files, prompts = self.run_warmup(warmup_rounds=0)
        self.assertEqual((policy['mode'], prompts, files), ('no_workload_warmup', [], ['warmup-receipt.json']))
        with self.assertRaisesRegex(RuntimeError, 'invalid warmup'):
            self.settings(warmup_rounds=0, warmup_dataset={'path': self.separate.name, 'sha256': host.sha(self.separate)})

    def test_changed_warmup_bytes_rejected_before_sending_any_request(self):
        config, rounds, source = self.settings(warmup_dataset={'path': self.separate.name, 'sha256': host.sha(self.separate)})
        self.separate.write_bytes(b'changed')
        live = SimpleNamespace(warmup=Mock(), directory=self.base)
        with self.assertRaisesRegex(RuntimeError, 'frozen digest'):
            worker.prepare_warmup(live, config, rounds, source)
        live.warmup.assert_not_called()


if __name__ == '__main__':
    unittest.main()
