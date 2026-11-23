"""Exercise raw CSV attribution with frozen digests and explicit scope."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from diagnose.scripts import step_sources as adapter


class SourceTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name)
        self.run = self.path / "run"
        self.run.mkdir()
        (self.run / "manifest.json").write_text('{"run_id":"r1"}')
        (self.run / "metadata.json").write_text('{"profile_capture_id":"capture-1"}')

    def csv(self, name, fields, rows):
        path = self.path / name
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(fields.split())
            writer.writerows(rows)
        return path

    def spec(self, tables=None, native=False):
        mapping = None
        if tables is not None:
            mapping = self.path / "mapping.json"
            mapping.write_text(json.dumps({"scope": "full_run", "step_definition": "one scheduler iteration",
                                           "owner_ranks": {"0": "0"},
                                           "tables": tables}))
        out = self.path / "spec.json"
        adapter.prepare(self.run, self.path if native else None, mapping, "26.0 fixture", out)
        return out

    def test_step_definition_must_come_from_site_inventory(self):
        self.csv("inventory.csv", "run step", [["r1", "1"]])
        mapping = self.path / "map.json"
        mapping.write_text(json.dumps({"tables": [{"kind": "inventory", "path": "inventory.csv"}]}))
        target = self.path / "out.json"
        with self.assertRaisesRegex(ValueError, "step_definition"):
            adapter.prepare(self.run, None, mapping, "test", target)
        self.assertFalse(target.exists())

    def test_dependent_original_is_reverified_after_csv_conversion(self):
        self.csv("kvcache.csv", "name used_blocks", [["alloc", 4]])
        original = self.path / "engine.log"
        original.write_text("observed 4 blocks")
        mapping = self.path / "map.json"
        mapping.write_text(json.dumps({"evidence_files": [{"path": original.name,
                                                           "sha256": adapter.file_hash(original)}]}))
        spec = self.path / "spec.json"
        adapter.prepare(self.run, self.path, mapping, "test", spec)
        self.assertIn(str(original.resolve()), {r["path"] for r in adapter.read_sources(spec, self.run)["sources"]})
        original.write_text("observed 5 blocks")
        with self.assertRaisesRegex(ValueError, "source evidence fingerprint mismatch"):
            adapter.read_sources(spec, self.run)

    def test_native_headers_preserve_units_sources_and_rank_scopes(self):
        self.csv("batch.csv", "name during_time(ms) prof_id prefill_scheduled_tokens decode_scheduled_tokens", [
            ["batchFrameworkProcessing", "0.12", "card0", 100, 2],
            ["modelExec", 8, "card0", "<NA>", ""], ["modelExec", 9, "card0", "", ""],
            ["modelExec", 11, "card1", "", ""]])
        self.csv("request.csv", "http_rid queue_wait_time(ms) reply_token_size", [["chat-x", 7, 3]])
        self.csv("kvcache.csv", "name total_blocks used_blocks free_blocks", [["alloc", 500, 400, 100]])
        observed = adapter.read_sources(self.spec(native=True), self.run)
        self.assertEqual(observed["scope"], "partial")
        self.assertEqual(observed["outputs"], [])
        groups = observed["native_observations"][0]["groups"]
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[1]["fields"]["during_time(ms)"]["max"], 9)
        self.assertEqual(groups[1]["fields"]["during_time(ms)"]["max_source"]["line"], 4)
        self.assertTrue(all(r["binding"] == "operator_paired" for r in observed["native_observations"]))
        self.assertEqual(len(observed["sources"]), 4)

    def fixture(self):
        self.csv("i.csv", "campaign dp rank iter ms", [
            ["warmup", 0, 0, 2, 5], ["r1", 0, 1, 2, 0.1], ["r1", 0, 0, 2, 0.08]])
        self.csv("commits.csv", "capture rank iter worker_id offset n", [
            ["capture-1", 1, 2, "wid", 0, 2], ["capture-1", 0, 2, "wid", 0, 2]])
        self.csv("ids.csv", "run_id response_id request_id", [["r1", "chat-12", "fixed-a"]])
        self.csv("engine.csv", "run_id response_id worker_id", [["r1", "chat-12", "wid"]])
        self.csv("seal.csv", "run_id count", [["r1", 1]])
        return [
            {"kind": "inventory", "path": "i.csv", "columns": {
                "run_id": "campaign", "step_id": "iter", "owner_rank": "rank", "replica": "dp", "duration_ms": "ms"}},
            {"kind": "outputs", "path": "commits.csv", "constants": {"replica": "0"}, "columns": {
                "capture_id": "capture", "owner_rank": "rank", "step_id": "iter", "backend_id": "worker_id",
                "output_start": "offset", "output_count": "n"}},
            {"kind": "request_map", "path": "ids.csv", "columns": {
                "run_id": "run_id", "response_id": "response_id", "request_id": "request_id"}},
            {"kind": "bridge", "path": "engine.csv", "columns": {
                "run_id": "run_id", "response_id": "response_id", "backend_id": "worker_id"}},
            {"kind": "seal", "path": "seal.csv", "constants": {"replica": "0"},
             "columns": {"run_id": "run_id", "expected_steps": "count"}},
        ]

    def test_site_joins_owner_capture_and_backend_to_client(self):
        tables = self.fixture()
        self.csv("work.csv", "run_id step_id req cached", [["r1", "2", "chat-12", 16]])
        tables.append({"kind": "work", "path": "work.csv", "constants": {"replica": "0", "owner_rank": "0"},
                       "columns": {"run_id": "run_id", "step_id": "step_id", "response_id": "req",
                                   "prefix_hit_tokens": "cached"}})
        result = adapter.read_sources(self.spec(tables), self.run)
        self.assertEqual(result["scope"], "full_run")
        self.assertEqual(result["expected_steps_by_replica"], {"0": 1})
        self.assertEqual(len(result["inventory"]), 1)
        self.assertEqual(result["inventory"][0]["duration_us"], 80)
        self.assertEqual(result["inventory"][0]["source"]["line"], 4)
        self.assertEqual(len(result["outputs"]), 1)
        self.assertEqual(result["outputs"][0]["request_id"], "fixed-a")
        self.assertEqual(result["work"][0]["prefix_hit_tokens"], 16)

    def test_missing_output_column_and_no_seal_are_partial(self):
        tables = self.fixture()
        tables[1]["columns"]["output_start"] = "unknown"
        observed = adapter.read_sources(self.spec(tables[:-1]), self.run)
        self.assertEqual(observed["scope"], "partial")
        self.assertEqual(observed["outputs"], [])
        self.assertTrue(any("absent columns" in x for x in observed["unresolved"]))
        self.assertIsNone(observed["expected_steps_by_replica"])

    def test_backend_ids_do_not_fall_back_to_text_matching(self):
        tables = self.fixture()
        self.csv("commits.csv", "capture rank iter worker_id offset n", [["capture-1", 0, 2, "chat-12", 0, 2]])
        result = adapter.read_sources(self.spec([t for t in tables if t["kind"] != "bridge"]), self.run)
        self.assertEqual(result["outputs"], [])
        self.assertEqual(result["scope"], "partial")

    def test_reject_conflicting_request_ids(self):
        tables = self.fixture()
        self.csv("ids.csv", "run_id response_id request_id", [["r1", "chat-12", "a"], ["r1", "chat-12", "b"]])
        with self.assertRaisesRegex(ValueError, "conflicting request"):
            adapter.read_sources(self.spec(tables), self.run)

    def test_reject_changed_source_and_mismatched_manifest(self):
        spec = self.spec(self.fixture())
        manifest = self.run / "manifest.json"
        original = manifest.read_bytes()
        manifest.write_bytes(original + b" ")
        with self.assertRaisesRegex(ValueError, "different run"):
            adapter.read_sources(spec, self.run)
        manifest.write_bytes(original)
        with (self.path / "commits.csv").open("a") as f:
            f.write("\n")
        with self.assertRaisesRegex(ValueError, "fingerprint mismatch"):
            adapter.read_sources(spec, self.run)

    def test_same_bytes_cannot_close_inventory(self):
        tables = self.fixture()
        tables[4].update(path="i.csv", columns={"run_id": "campaign", "expected_steps": "iter"},
                         where={"rank": "0"})
        with self.assertRaisesRegex(ValueError, "its own completeness seal"):
            adapter.read_sources(self.spec(tables), self.run)

    def test_duplicate_csv_headers_rejected(self):
        self.csv("request.csv", "http_rid http_rid", [["a", "b"]])
        with self.assertRaisesRegex(ValueError, "invalid CSV header"):
            adapter.read_sources(self.spec(native=True), self.run)

    def test_missing_step_column_degrades_dependent_output_rows(self):
        tables = self.fixture()
        tables[0]["columns"]["step_id"] = "absent"
        result = adapter.read_sources(self.spec(tables), self.run)
        self.assertEqual(result["scope"], "partial")
        self.assertFalse(result["outputs"])
        self.assertTrue(any("matching observed owner step" in issue for issue in result["unresolved"]))

    def test_large_batch_request_list_and_signed_kv_delta(self):
        self.csv("batch.csv", "name res_list batch_size", [["modelExec", "r,"*100000, 100000]])
        self.csv("kvcache.csv", "name blocks_freed", [["allocate", -2]])
        result = adapter.read_sources(self.spec(native=True), self.run)
        batch, kv = result["native_observations"]
        self.assertEqual(batch["groups"][0]["fields"]["batch_size"]["max"], 100000)
        self.assertEqual(kv["groups"][0]["fields"]["blocks_freed"]["min"], -2)

    def test_identifier_spelling_is_preserved(self):
        tables = self.fixture()
        self.csv("ids.csv", "run_id response_id request_id", [["r1", "chat-12", "NA"]])
        result = adapter.read_sources(self.spec(tables), self.run)
        self.assertEqual(result["outputs"][0]["request_id"], "NA")

    def test_requested_sse_proof_rejects_changed_id_or_digest(self):
        tables = self.fixture()
        tables[2]["columns"]["sse_sha256"] = "sse_sha256"
        self.csv("ids.csv", "run_id response_id request_id sse_sha256",
                 [["r1", "chat-12", "fixed-a", "b"*64]])
        spec = self.spec(tables)
        with patch.object(adapter, "verified_sse_maps", return_value={"fixed-a": ("chat-12", "c"*64)}):
            with self.assertRaisesRegex(ValueError, "retained SSE"):
                adapter.read_sources(spec, self.run)
        with patch.object(adapter, "verified_sse_maps", return_value={"fixed-a": ("chat-12", "b"*64)}):
            result = adapter.read_sources(spec, self.run)
            self.assertEqual(result["outputs"][0]["request_id"], "fixed-a")


if __name__ == "__main__":
    unittest.main()
