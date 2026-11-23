"""Public query entrypoint works without the other capability modules or builders."""

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


MODULE = Path(__file__).resolve().parents[1]


class KnowledgeEntrypointTests(unittest.TestCase):
    def test_relocated_query_uses_selected_snapshot_without_builders_or_writes(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            module = root / "relocated knowledge"
            (module / "scripts").mkdir(parents=True)
            default = module / "kb/parse_params/output/params"
            default.mkdir(parents=True)
            shutil.copy2(MODULE / "scripts/kb.py", module / "scripts/kb.py")
            shutil.copy2(MODULE / "kb/query.py", module / "kb/query.py")
            (default / "default.yaml").write_text(json.dumps({"name": "--default"}), encoding="utf-8")
            selected = root / "selected snapshot"
            selected.mkdir()
            for name in ("first", "second"):
                (selected / (name + ".yaml")).write_text(json.dumps({"name": "--" + name}), encoding="utf-8")

            def inventory():
                return {
                    str(p.relative_to(root)): hashlib.sha256(p.read_bytes()).hexdigest()
                    for p in root.rglob("*") if p.is_file()
                }

            before = inventory()
            for args, expected in [(["--count"], "1"), (["-d", str(selected), "--count"], "2")]:
                result = subprocess.run(
                    [sys.executable, "-B", str(module / "scripts/kb.py"), "query", *args],
                    cwd=root, capture_output=True, text=True, timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), expected)
                self.assertEqual(inventory(), before)


if __name__ == "__main__":
    unittest.main()
