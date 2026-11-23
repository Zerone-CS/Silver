"""Offline behavioral tests for the standalone KB build interfaces.

Run: PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests -v
No network or actual model/API calls are made.
"""
from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

KB = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(KB))
from kb_runtime import builder_identity, check_output, llm_environment, validate_sources, SNAPSHOT_ROOT


class StandaloneKBTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.vllm = self.root / 'vllm-src'
        self.ascend = self.root / 'ascend-src'
        self.write(self.vllm / 'vllm/config/cache.py', '@config\nclass CacheConfig:\n    block_size: int = 16\n')
        self.write(self.vllm / 'vllm/engine/arg_utils.py', 'def add_cli_args(parser):\n    parser.add_argument("--block-size", type=int, default=16, help="KV cache block size")\n    return parser\n')
        self.write(self.vllm / 'vllm/envs.py', 'import os\nVLLM_TEST_CACHE = os.getenv("VLLM_TEST_CACHE", "1")\n')
        self.write(self.ascend / 'vllm_ascend/ascend_config.py', 'class AscendConfig:\n    enable_graph: bool = True\n')
        self.write(self.ascend / 'vllm_ascend/envs.py', 'import os\nVLLM_ASCEND_TEST = os.getenv("VLLM_ASCEND_TEST", "0")\n')
        self.write(self.ascend / 'docs/source/test.md', 'Use `--block-size` for KV cache blocks.\n')
        self.inputs = self.root / 'input.json'
        self.inputs.write_text(json.dumps([{'name': '--block-size', 'type': 'cli', 'scope': 'vllm', 'category': 'memory', 'description': 'KV cache block size'}]))
        # Fail closed if a subprocess tries to read user settings, use old paths,
        # import optional LLM libraries, or establish a network connection.
        hook = '''import os, sys

def guard(event, args):
    if event == 'open' and args and isinstance(args[0], (str, bytes)):
        path = os.fsdecode(args[0])
        if '/.claude/' in path or '/vllm-workspace/' in path:
            raise RuntimeError('forbidden implicit file dependency')
    if event in ('socket.connect', 'socket.getaddrinfo'):
        raise RuntimeError('network forbidden in offline tests')
    if event == 'import' and args and args[0].split('.')[0] in ('anthropic', 'pydantic'):
        raise RuntimeError('optional LLM dependency used by offline path')
sys.addaudithook(guard)
'''
        self.write(self.root / 'sitecustomize.py', hook)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith(('ANTHROPIC_', 'VLLM_', 'LLM_'))}
        self.env.update(PYTHONPATH=os.pathsep.join([str(self.root), str(KB)]), PYTHONDONTWRITEBYTECODE='1')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, content):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)

    def command(self, module, *args):
        return subprocess.run([sys.executable, '-m', module, *map(str, args)], cwd=self.root, env=self.env, text=True, capture_output=True, timeout=30)

    def source_args(self):
        return ['--vllm-src', self.vllm, '--vllm-ascend-src', self.ascend]

    def parse(self, output, *flags):
        return self.command('parse_params', '--input', self.inputs, '--output', output, *self.source_args(), *flags)

    def test_safe_extractor_help_has_no_output_or_side_effects(self):
        before = set(self.root.rglob('*'))
        result = self.command('extract_params', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--vllm-src', result.stdout)
        self.assertEqual(before, set(self.root.rglob('*')))

    def test_safe_parser_help_without_optional_dependencies(self):
        result = self.command('parse_params', '--help')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('--dry-run-stage1', result.stdout)

    def test_required_explicit_arguments(self):
        for module in ['extract_params', 'parse_params']:
            result = self.command(module)
            self.assertEqual(result.returncode, 2)
            self.assertIn('required', result.stderr)

    def test_missing_sources_fail_before_output(self):
        output = self.root / 'new.json'
        result = self.command('extract_params', '--vllm-src', self.root / 'missing', '--vllm-ascend-src', self.ascend, '--output', output)
        self.assertEqual(result.returncode, 2)
        self.assertIn('Missing required source', result.stderr)
        self.assertFalse(output.exists())

    def test_static_extraction_is_usable_from_another_cwd(self):
        output = self.root / 'fresh/parameters.json'
        result = self.command('extract_params', *self.source_args(), '--output', output, '--pretty')
        self.assertEqual(result.returncode, 0, result.stderr)
        data = json.loads(output.read_text())
        self.assertTrue(any(p['name'] == '--block-size' and p['default'] == 16 for p in data), data)
        meta = json.loads(Path(str(output) + '.provenance.json').read_text())
        self.assertEqual(meta['source_inputs']['vllm']['commit'], 'unknown')
        self.assertIsNone(meta['source_inputs']['vllm']['dirty'])
        self.assertFalse(meta['llm_requested'])

    def test_extract_does_not_replace_existing_file(self):
        self.inputs.write_text('preserve-me')
        result = self.command('extract_params', *self.source_args(), '--output', self.inputs)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(self.inputs.read_text(), 'preserve-me')

    def test_extract_does_not_replace_existing_provenance(self):
        output = self.root / 'fresh.json'
        sidecar = Path(str(output) + '.provenance.json')
        sidecar.write_text('preserve-me')
        result = self.command('extract_params', *self.source_args(), '--output', output)
        self.assertEqual(result.returncode, 2)
        self.assertFalse(output.exists())
        self.assertEqual(sidecar.read_text(), 'preserve-me')

    def test_source_syntax_failure_is_explicit(self):
        self.write(self.ascend / 'vllm_ascend/broken.py', 'def broken(:\n')
        result = self.command('extract_params', *self.source_args(), '--output', self.root / 'broken.json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Source syntax cannot be parsed', result.stderr)
        self.assertFalse((self.root / 'broken.json').exists())

    def test_llm_extraction_requires_key_and_model_before_output(self):
        result = self.command('extract_params', *self.source_args(), '--output', self.root / 'llm.json', '--with-claude')
        self.assertEqual(result.returncode, 2)
        self.assertIn('LLM requires ANTHROPIC_API_KEY', result.stderr)
        self.assertFalse((self.root / 'llm.json').exists())

    def test_rule_only_parse_needs_no_llm_configuration(self):
        output = self.root / 'rules'
        result = self.parse(output, '--dry-run-stage1')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((output / 'progress.json').is_file())
        self.assertTrue((output / 'build-inputs.json').is_file())
        self.assertFalse((output / 'manifest.yaml').exists())

    def test_parse_llm_missing_key_fails_before_output(self):
        output = self.root / 'live'
        result = self.parse(output)
        self.assertEqual(result.returncode, 2)
        self.assertIn('LLM requires ANTHROPIC_API_KEY', result.stderr)
        self.assertFalse(output.exists())

    def test_parse_new_output_is_required(self):
        output = self.root / 'existing'
        output.mkdir()
        result = self.parse(output, '--dry-run-stage1')
        self.assertEqual(result.returncode, 2)
        self.assertFalse((output / 'progress.json').exists())

    def test_bundled_snapshot_cannot_be_a_build_output_even_on_resume(self):
        with self.assertRaisesRegex(ValueError, 'immutable'):
            check_output(SNAPSHOT_ROOT / 'new', resume=False)
        with self.assertRaisesRegex(ValueError, 'immutable'):
            check_output(SNAPSHOT_ROOT, resume=True)

    def test_output_cannot_mutate_source_inputs(self):
        with self.assertRaisesRegex(ValueError, 'separate from source'):
            check_output(self.vllm / 'output', [self.vllm, self.ascend])

    def test_resume_requires_existing_tool_provenance(self):
        output = self.root / 'foreign-output'
        output.mkdir()
        result = self.parse(output, '--dry-run-stage1', '--resume')
        self.assertEqual(result.returncode, 2)
        self.assertIn('build-inputs.json', result.stderr)

    def test_resume_matches_original_input_and_source_bytes(self):
        output = self.root / 'rules'
        self.assertEqual(self.parse(output, '--dry-run-stage1').returncode, 0)
        resumed = self.parse(output, '--dry-run-stage1', '--resume')
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        self.inputs.write_text(self.inputs.read_text() + '\n')
        changed = self.parse(output, '--dry-run-stage1', '--resume')
        self.assertEqual(changed.returncode, 2)
        self.assertIn('input/source or builder identity differs', changed.stderr)

    def test_resume_rejects_changed_source_bytes(self):
        output = self.root / 'rules'
        self.assertEqual(self.parse(output, '--dry-run-stage1').returncode, 0)
        self.write(self.vllm / 'vllm/config/new.py', 'NEW = 1\n')
        changed = self.parse(output, '--dry-run-stage1', '--resume')
        self.assertEqual(changed.returncode, 2)
        self.assertIn('input/source or builder identity differs', changed.stderr)

    def test_corrupt_progress_is_not_silently_reset(self):
        output = self.root / 'rules'
        self.assertEqual(self.parse(output, '--dry-run-stage1').returncode, 0)
        (output / 'progress.json').write_text('{}')
        result = self.parse(output, '--dry-run-stage1', '--resume')
        self.assertEqual(result.returncode, 2)
        self.assertIn('progress.json is invalid', result.stderr)

    def test_llm_config_only_uses_explicit_environment(self):
        with mock.patch.dict(os.environ, {}, clear=True), mock.patch.object(Path, 'home', side_effect=AssertionError('must not read user settings')):
            self.assertEqual(llm_environment(), {})
            with self.assertRaisesRegex(ValueError, 'missing: api_key, model'):
                llm_environment(required=True)
        with mock.patch.dict(os.environ, {'ANTHROPIC_AUTH_TOKEN': 'synthetic-placeholder', 'ANTHROPIC_MODEL': 'test-model', 'ANTHROPIC_BASE_URL': 'https://example.invalid'}, clear=True):
            result = llm_environment(required=True)
            self.assertEqual(result['model'], 'test-model')
            self.assertEqual(result['base_url'], 'https://example.invalid')

    def test_real_git_commit_and_dirty_identity_are_derived(self):
        for arguments in [['init', '-q'], ['add', '.'], ['-c', 'user.name=Offline Test', '-c', 'user.email=offline@example.invalid', 'commit', '-qm', 'fixture']]:
            subprocess.run(['git', '-C', str(self.vllm), *arguments], check=True, capture_output=True)
        _, identities = validate_sources(self.vllm, self.ascend)
        self.assertEqual(len(identities['vllm']['commit']), 40)
        self.assertFalse(identities['vllm']['dirty'])
        self.write(self.vllm / 'vllm/envs.py', 'CHANGED = 1\n')
        _, changed = validate_sources(self.vllm, self.ascend)
        self.assertTrue(changed['vllm']['dirty'])
        self.assertNotEqual(identities['vllm']['python_markdown_sha256'], changed['vllm']['python_markdown_sha256'])

    def test_resume_rejects_schema_log_param_and_progress_symlinks(self):
        output = self.root / 'linked-output'
        self.assertEqual(self.parse(output, '--dry-run-stage1').returncode, 0)
        protected = self.root / 'protected.txt'
        protected.write_text('must remain unchanged')
        for name in ['schema.yaml', 'progress.json', 'logs/response.txt', 'params']:
            with self.subTest(name=name):
                linked = output / name
                previous = linked.read_bytes() if linked.is_file() else None
                if linked.exists():
                    linked.unlink()
                linked.parent.mkdir(parents=True, exist_ok=True)
                linked.symlink_to(protected)
                result = self.parse(output, '--dry-run-stage1', '--resume')
                self.assertEqual(result.returncode, 2)
                self.assertIn('output tree contains a symlink', result.stderr)
                self.assertEqual(protected.read_text(), 'must remain unchanged')
                linked.unlink()
                if previous is not None:
                    linked.write_bytes(previous)

    def test_source_tilde_is_expanded_before_output_boundary_check(self):
        with mock.patch.dict(os.environ, {'HOME': str(self.root)}):
            with self.assertRaisesRegex(ValueError, 'separate from source'):
                check_output(self.vllm / 'nested-output', ['~/vllm-src'])

    def test_parse_rejects_sanitized_filename_collision_before_output(self):
        self.inputs.write_text(json.dumps([{'name': name, 'category': 'memory', 'type': 'cli'} for name in ['--foo-bar', '--foo_bar']]))
        output = self.root / 'collision'
        result = self.parse(output, '--dry-run-stage1')
        self.assertEqual(result.returncode, 2)
        self.assertIn('both map to foo_bar.yaml', result.stderr)
        self.assertFalse(output.exists())

    def test_builder_identity_covers_code_resources_and_prompt_without_api_key(self):
        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'secret-never-record', 'ANTHROPIC_MODEL': 'test-model', 'ANTHROPIC_BASE_URL': 'https://example.invalid'}, clear=True):
            identity = builder_identity('llm', 3, 1)
        files = identity['files_sha256']
        for name in ['kb_runtime.py', 'parse_params/__main__.py', 'parse_params/schema.py', 'parse_params/resources/stage1_rules.yaml', 'parse_params/resources/system_prompt.txt', 'parse_params/resources/user_prompt_template.txt', 'parse_params/resources/schema.yaml', 'parse_params/prompt.txt']:
            self.assertIn(name, files)
        self.assertEqual(identity['llm'], {'model': 'test-model', 'endpoint': 'https://example.invalid'})
        self.assertNotIn('secret-never-record', json.dumps(identity))
        self.assertNotIn('api_key', json.dumps(identity))
        with mock.patch('kb_runtime.file_sha256', return_value='changed'):
            changed = builder_identity('llm', 3, 1)
        self.assertNotEqual(files, changed['files_sha256'])

    def test_builder_rejects_endpoint_with_embedded_credentials_before_recording(self):
        with mock.patch.dict(os.environ, {'ANTHROPIC_BASE_URL': 'https://secret@example.invalid'}, clear=True):
            with self.assertRaisesRegex(ValueError, 'must not contain credentials'):
                builder_identity('stage1-only', 0, 1)

    def test_resume_rejects_changed_recorded_builder_files(self):
        output = self.root / 'rules'
        self.assertEqual(self.parse(output, '--dry-run-stage1').returncode, 0)
        path = output / 'build-inputs.json'
        identity = json.loads(path.read_text())
        identity['builder']['files_sha256']['parse_params/resources/stage1_rules.yaml'] = 'old-rule-hash'
        path.write_text(json.dumps(identity))
        result = self.parse(output, '--dry-run-stage1', '--resume')
        self.assertEqual(result.returncode, 2)
        self.assertIn('builder identity differs', result.stderr)

    def test_resume_rejects_changed_effective_model_and_endpoint(self):
        output = self.root / 'rules'
        self.assertEqual(self.parse(output, '--dry-run-stage1').returncode, 0)
        self.env['ANTHROPIC_MODEL'] = 'another-model'
        self.env['ANTHROPIC_BASE_URL'] = 'https://example.invalid'
        result = self.parse(output, '--dry-run-stage1', '--resume')
        self.assertEqual(result.returncode, 2)
        self.assertIn('builder identity differs', result.stderr)

    def test_dry_run_output_cannot_resume_as_llm_build(self):
        output = self.root / 'rules'
        self.assertEqual(self.parse(output, '--dry-run-stage1').returncode, 0)
        result = self.parse(output, '--resume')
        self.assertEqual(result.returncode, 2)
        self.assertIn('builder identity differs', result.stderr)
        self.assertNotIn('optional LLM dependency', result.stderr)

    def test_query_uses_bundled_data_from_another_cwd(self):
        result = self.command('query', '--count')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '756')

    def test_extractor_modules_import_with_postponed_annotations(self):
        import importlib
        for path in sorted((KB / 'extract_params').glob('*.py')):
            importlib.import_module('extract_params.' + path.stem)
        importlib.import_module('parse_params.utils')

    def test_source_directory_symlink_cannot_hide_an_external_input(self):
        external = self.root / 'external.py'
        external.write_text('X = 1\n')
        (self.ascend / 'vllm_ascend/link.py').symlink_to(external)
        with self.assertRaisesRegex(ValueError, 'symlink escapes'):
            validate_sources(self.vllm, self.ascend)

    @unittest.skipUnless(sys.version_info < (3, 10), 'only Python 3.9 rejects match syntax')
    def test_python39_rejects_newer_input_syntax_without_claiming_coverage(self):
        self.write(self.ascend / 'vllm_ascend/newer.py', 'match value:\n    case 1:\n        pass\n')
        result = self.command('extract_params', *self.source_args(), '--output', self.root / 'newer.json')
        self.assertEqual(result.returncode, 2)
        self.assertIn('Source syntax cannot be parsed by Python 3.9', result.stderr)

    @unittest.skipUnless(importlib.util.find_spec('pydantic'), 'pydantic v2 is required for optional schema validation')
    def test_schema_import_and_validation_on_current_python(self):
        from parse_params.schema import ParameterYAML
        value = ParameterYAML(name='--test', type='cli', category='memory', scope='vllm', value_type='int', default=1, valid_choices=None, performance_impact='low', performance_scope=['throughput'], impact_detail='Synthetic fixture')
        self.assertIsNone(value.cli_example)
        self.assertEqual(value.model_dump()['name'], '--test')


if __name__ == '__main__':
    unittest.main()
