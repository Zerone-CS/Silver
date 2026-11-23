import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
MAIN = 'vllm-ascend-throughput-optimization'
NAMES = [MAIN, 'evaluate', 'diagnose', 'optimize', 'knowledge']
SKILL_PATHS = {name: '.' if name == MAIN else name for name in NAMES}
LEGACY_NAMES = ['measure', 'profile', 'validate']
OLD_NAMES = [MAIN, 'measure', 'profile', 'diagnose', 'knowledge', 'optimize', 'validate']


class StandaloneSuiteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Path(self.temp.name).resolve()
        self.suite = self.workspace / 'a-different-location' / 'suite'
        shutil.copytree(ROOT, self.suite, ignore=shutil.ignore_patterns('.git', '.runtime', '.maintenance', '__pycache__', '*.pyc'))
        self.cwd = self.workspace / 'foreign-cwd'
        self.cwd.mkdir()
        self.env = {k: v for k, v in os.environ.items() if k not in ('PYTHONPATH', 'PYTHONHOME')}
        self.env.update(PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1')

    def run_tool(self, name, *args):
        module = 'knowledge/scripts' if name in ('kb.py', 'check_kb_snapshot.py') else 'scripts'
        return subprocess.run([sys.executable, str(self.suite / module / name)] + list(args),
                              cwd=str(self.cwd), env=self.env, capture_output=True, text=True, timeout=45)

    def old_registration(self, discovery, names=OLD_NAMES):
        """Fixture matching the previous seven-entry installer's ownership format."""
        for name in names:
            folder = discovery / name
            folder.mkdir(parents=True)
            text = ('---\nname: ' + name + '\ndescription: previous suite entry\n---\n\n'
                    '[' + name + '](/previous/suite/' + name + '/SKILL.md)\n')
            (folder / 'SKILL.md').write_text(text)
            (folder / 'registration.json').write_text(json.dumps({
                'schema_version': 1, 'kind': 'throughput-suite-discovery',
                'suite_root': '/previous/suite', 'skill': name,
                'entry_sha256': hashlib.sha256(text.encode()).hexdigest(),
            }))

    def discovery_snapshot(self, discovery):
        return {str(p.relative_to(discovery)): p.read_bytes() for p in discovery.rglob('*') if p.is_file()}

    def test_moved_package_verifies_and_queries_real_snapshot(self):
        self.assertTrue((self.suite / 'SKILL.md').is_file())
        self.assertTrue((self.suite / 'knowledge/resources/requirements.lock').is_file())
        self.assertFalse((self.suite / MAIN).exists())
        result = self.run_tool('verify_suite.py')
        self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
        self.assertEqual(json.loads(result.stdout)['external_project_dependencies'], [])
        self.assertEqual(json.loads(result.stdout)['skill_paths'], SKILL_PATHS)
        self.assertEqual(json.loads(result.stdout)['skills'], 5)
        self.assertEqual(json.loads(result.stdout)['compatibility_entries'], 0)
        self.assertEqual(json.loads(result.stdout)['compatibility_aliases'], {})
        self.assertEqual(json.loads(result.stdout)['compatibility_paths'], {})
        for name in LEGACY_NAMES:
            self.assertFalse((self.suite / name).exists())
        result = self.run_tool('kb.py', 'query', '--count')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), '756')

    def test_old_nested_skill_mapping_is_rejected(self):
        manifest_path = self.suite / 'suite-manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['skill_paths'][MAIN] = MAIN
        manifest_path.write_text(json.dumps(manifest))
        result = self.run_tool('verify_suite.py')
        self.assertEqual(result.returncode, 2)
        self.assertIn('skill paths', result.stdout)

    def test_contract_and_command_ownership_cannot_be_reassigned(self):
        manifest_path = self.suite / 'suite-manifest.json'
        original = json.loads(manifest_path.read_text())
        variants = []
        manifest = json.loads(json.dumps(original))
        manifest['public_contracts']['measurement']['owner'] = 'knowledge'
        variants.append(manifest)
        manifest = json.loads(json.dumps(original))
        manifest['command_entrypoints']['evaluate']['benchmark'] = 'knowledge/scripts/kb.py'
        variants.append(manifest)
        manifest = json.loads(json.dumps(original))
        manifest['implementation_dependencies']['evaluate'] = ['knowledge']
        variants.append(manifest)
        for manifest in variants:
            with self.subTest(manifest=manifest):
                manifest_path.write_text(json.dumps(manifest))
                result = self.run_tool('verify_suite.py')
                self.assertNotEqual(result.returncode, 0)

    def test_evaluate_pipeline_runs_alone_without_site_packages(self):
        # A capability must work without coordinator, KB, or their dependencies.
        module = self.workspace / 'evaluate-only'
        shutil.copytree(self.suite / 'evaluate', module)
        self.assertFalse((module.parent / 'knowledge').exists())
        self.assertFalse((module / '.runtime').exists())
        result = subprocess.run([sys.executable, '-S', '-B', '-m', 'unittest', 'discover',
                                 '-s', str(module / 'tests'), '-p', 'test_workflow_integration.py'],
                                cwd=str(self.cwd), env=self.env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('OK', result.stderr)

    def test_test_runner_executes_all_scopes_and_returns_failure(self):
        # Sentinel suites exercise discovery/exit propagation, not business tests.
        fake = self.workspace / 'runner-package'
        (fake / 'scripts').mkdir(parents=True)
        shutil.copy2(self.suite / 'scripts/run_tests.py', fake / 'scripts/run_tests.py')
        suites = {'evaluate': ['evaluate/tests'], 'knowledge': ['knowledge/tests', 'knowledge/kb/tests'],
                  'suite': ['tests']}
        (fake / 'suite-manifest.json').write_text(json.dumps({'test_suites': suites}))
        for directories in suites.values():
            for relative in directories:
                folder = fake / relative
                folder.mkdir(parents=True)
                marker = str(folder / 'visited')
                assertion = 'self.fail("sentinel failure")' if relative == 'knowledge/tests' else 'self.assertTrue(True)'
                (folder / 'test_sentinel.py').write_text(
                    'import unittest\nfrom pathlib import Path\n'
                    'class Sentinel(unittest.TestCase):\n'
                    '    def test_visit(self):\n'
                    '        Path(' + repr(marker) + ').touch()\n'
                    '        ' + assertion + '\n')
        result = subprocess.run([sys.executable, str(fake / 'scripts/run_tests.py')],
                                cwd=str(self.cwd), env=self.env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        for directories in suites.values():
            for relative in directories:
                self.assertTrue((fake / relative / 'visited').exists(), relative)

    def test_tampered_bundled_code_is_rejected(self):
        query = self.suite / 'knowledge/kb/query.py'
        query.write_text(query.read_text() + '\n# altered\n')
        result = self.run_tool('verify_suite.py')
        self.assertEqual(result.returncode, 2)
        self.assertIn('knowledge/kb/query.py', result.stdout)

    def test_legacy_compatibility_metadata_is_rejected(self):
        manifest_path = self.suite / 'suite-manifest.json'
        original = json.loads(manifest_path.read_text())
        for field, target in [('compatibility_aliases', 'optimize'), ('compatibility_paths', '../measure')]:
            with self.subTest(field=field):
                manifest = json.loads(json.dumps(original))
                manifest[field]['measure'] = target
                manifest_path.write_text(json.dumps(manifest))
                result = self.run_tool('verify_suite.py')
                self.assertEqual(result.returncode, 2)
                self.assertIn('compatibility', result.stdout)

    def test_registration_points_all_entries_to_this_suite(self):
        discovery = self.workspace / 'skills'
        result = self.run_tool('register_skills.py', '--skills-dir', str(discovery))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in discovery.iterdir()), sorted(NAMES))
        self.assertEqual(json.loads(result.stdout)['canonical_entries'], 5)
        self.assertEqual(json.loads(result.stdout)['compatibility_entries'], 0)
        for name in NAMES:
            folder = discovery / name
            self.assertEqual(sorted(p.name for p in folder.iterdir()), ['SKILL.md', 'registration.json'])
            self.assertIn(str(self.suite / SKILL_PATHS[name] / 'SKILL.md'), (folder / 'SKILL.md').read_text())
            self.assertFalse(folder.is_symlink())
        again = self.run_tool('register_skills.py', '--skills-dir', str(discovery))
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_registration_preserves_unrelated_existing_skill(self):
        discovery = self.workspace / 'skills'
        existing = discovery / 'knowledge'
        existing.mkdir(parents=True)
        (existing / 'SKILL.md').write_text('user-owned content')
        result = self.run_tool('register_skills.py', '--skills-dir', str(discovery))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((existing / 'SKILL.md').read_text(), 'user-owned content')
        self.assertEqual(list(discovery.iterdir()), [existing])

    def test_registration_after_move_updates_only_owned_entries(self):
        discovery = self.workspace / 'skills'
        self.old_registration(discovery)
        legacy_before = {name: self.discovery_snapshot(discovery / name) for name in LEGACY_NAMES}
        initial = self.run_tool('register_skills.py', '--skills-dir', str(discovery))
        self.assertEqual(initial.returncode, 0, initial.stderr)
        moved = self.workspace / 'relocated-again'
        self.suite.rename(moved)
        self.suite = moved
        result = self.run_tool('register_skills.py', '--skills-dir', str(discovery))
        self.assertEqual(result.returncode, 0, result.stderr)
        for name in NAMES:
            self.assertEqual(json.loads((discovery / name / 'registration.json').read_text())['suite_root'], str(moved))
            self.assertIn(str(moved / SKILL_PATHS[name] / 'SKILL.md'), (discovery / name / 'SKILL.md').read_text())
        for name in LEGACY_NAMES:
            self.assertEqual(self.discovery_snapshot(discovery / name), legacy_before[name])

    def test_registration_leaves_previous_legacy_entries_untouched(self):
        discovery = self.workspace / 'skills'
        self.old_registration(discovery)
        legacy_before = {name: self.discovery_snapshot(discovery / name) for name in LEGACY_NAMES}
        result = self.run_tool('register_skills.py', '--skills-dir', str(discovery))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in discovery.iterdir()), sorted(NAMES + LEGACY_NAMES))
        self.assertEqual(json.loads(result.stdout)['compatibility_entries'], 0)
        self.assertEqual(len(json.loads(result.stdout)['discovery_entries']), 5)
        for name in LEGACY_NAMES:
            self.assertEqual(self.discovery_snapshot(discovery / name), legacy_before[name])

    def test_unknown_and_legacy_directories_are_untouched(self):
        discovery = self.workspace / 'skills'
        self.old_registration(discovery, ['validate'])
        customized = discovery / 'validate' / 'SKILL.md'
        customized.write_text(customized.read_text() + '\nUser customization\n')
        for name in ('measure', 'unrelated'):
            existing = discovery / name
            existing.mkdir(parents=True)
            (existing / 'SKILL.md').write_text('user-owned content')
        before = self.discovery_snapshot(discovery)
        result = self.run_tool('register_skills.py', '--skills-dir', str(discovery))
        self.assertEqual(result.returncode, 0, result.stderr)
        after = self.discovery_snapshot(discovery)
        self.assertEqual({path: after[path] for path in before}, before)
        self.assertEqual(sorted(p.name for p in discovery.iterdir()), sorted(NAMES + ['measure', 'validate', 'unrelated']))
        self.assertFalse((discovery / 'profile').exists())

    def test_edited_owned_canonical_entry_rejects_entire_plan(self):
        discovery = self.workspace / 'skills'
        self.old_registration(discovery, ['knowledge'])
        entry = discovery / 'knowledge' / 'SKILL.md'
        entry.write_text(entry.read_text() + '\nUser customization\n')
        before = self.discovery_snapshot(discovery)
        result = self.run_tool('register_skills.py', '--skills-dir', str(discovery))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('edited', result.stderr)
        self.assertEqual(self.discovery_snapshot(discovery), before)
        self.assertEqual([p.name for p in discovery.iterdir()], ['knowledge'])

    def test_removed_legacy_flag_cannot_create_compatibility_entries(self):
        discovery = self.workspace / 'skills'
        result = self.run_tool('register_skills.py', '--skills-dir', str(discovery), '--include-legacy')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('unrecognized arguments', result.stderr)
        self.assertFalse(discovery.exists())

    def test_registration_rolls_back_existing_and_new_entries_on_write_failure(self):
        discovery = self.workspace / 'skills'
        self.old_registration(discovery)
        before = self.discovery_snapshot(discovery)
        code = '''
import sys
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(sys.argv[1]) / 'scripts'))
from register_skills import register
write_text = Path.write_text
def fail_write(path, *args, **kwargs):
    if path.parent.name == 'evaluate' and path.name == 'registration.json':
        raise OSError('simulated disk write failure')
    return write_text(path, *args, **kwargs)
with patch.object(Path, 'write_text', fail_write):
    try:
        register(Path(sys.argv[1]), Path(sys.argv[2]))
    except OSError as exc:
        assert str(exc) == 'simulated disk write failure'
    else:
        raise AssertionError('failure was not triggered')
'''
        result = subprocess.run([sys.executable, '-c', code, str(self.suite), str(discovery)],
                                cwd=str(self.cwd), env=self.env, capture_output=True, text=True, timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.discovery_snapshot(discovery), before)
        self.assertEqual(sorted(p.name for p in discovery.iterdir()), sorted(OLD_NAMES))

    def test_registration_does_not_follow_entry_file_symlink(self):
        discovery = self.workspace / 'skills'
        self.assertEqual(self.run_tool('register_skills.py', '--skills-dir', str(discovery)).returncode, 0)
        entry = discovery / 'knowledge' / 'SKILL.md'
        outside = self.workspace / 'outside-entry.md'
        original = entry.read_bytes()
        outside.write_bytes(original)
        entry.unlink()
        entry.symlink_to(outside)
        result = self.run_tool('register_skills.py', '--skills-dir', str(discovery))
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('symlink', result.stderr)
        self.assertEqual(outside.read_bytes(), original)

    def test_runtime_backup_cannot_escape_through_maintenance_symlink(self):
        outside = self.workspace / 'outside-backups'
        outside.mkdir()
        (self.suite / '.maintenance').symlink_to(outside, target_is_directory=True)
        result = self.run_tool('setup_runtime.py')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('maintenance', result.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.suite / '.runtime').exists())

    def test_runtime_cannot_escape_suite_root_through_symlink(self):
        outside = self.workspace / 'outside-runtime'
        outside.mkdir()
        (self.suite / '.runtime').symlink_to(outside, target_is_directory=True)
        result = self.run_tool('setup_runtime.py')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('runtime must be a directory inside this suite', result.stderr)
        self.assertEqual(list(outside.iterdir()), [])
        self.assertFalse((self.suite.parent / '.runtime').exists())

    def test_help_does_not_create_runtime_or_start_builds(self):
        before = sorted(str(p.relative_to(self.suite)) for p in self.suite.rglob('*'))
        for command in [('setup_runtime.py', '--help'), ('kb.py', 'extract', '--help'), ('kb.py', 'parse', '--help')]:
            result = self.run_tool(command[0], *command[1:])
            self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(before, sorted(str(p.relative_to(self.suite)) for p in self.suite.rglob('*')))
        self.assertFalse((self.suite / '.runtime').exists())


if __name__ == '__main__':
    unittest.main()
