from concurrent.futures import ThreadPoolExecutor
import contextlib
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


MODULE = Path(__file__).resolve().parents[1] / "scripts" / "experience.py"
spec = importlib.util.spec_from_file_location("suite_experience", MODULE)
experience = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experience)


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.work = Path(temporary.name)
        self.database = self.work / "private.sqlite"
        # Exercise the public CLI boundary without creating a complete hardware campaign.
        exporter = self.work / "campaign.py"
        exporter.write_text(
            "import sys\nfrom pathlib import Path\n"
            "command, cflag, campaign, rflag, rnd = sys.argv[1:]\n"
            "assert (command, cflag, rflag) == ('export', '--campaign', '--round')\n"
            "data = Path(campaign) / (rnd + '.json')\n"
            "if not data.is_file(): sys.exit('round is still open')\n"
            "print(data.read_text())\n", encoding="utf-8",
        )
        patch = mock.patch.object(experience, "CAMPAIGN", exporter)
        patch.start()
        self.addCleanup(patch.stop)

    def make_export(self, number=1, decision="revert"):
        evidence = self.work / "receipt.json"
        evidence.write_bytes(b"{}")
        identity = {"config_sha256": "a" * 64, "implementation_sha256": "b" * 64}
        return {
            "schema_version": 1, "campaign_id": "lab-1", "round": number, "decision": decision,
            "baseline": identity, "candidate": dict(identity, config_sha256="c" * 64),
            "scope": "Model A W5", "hypothesis": "APC raises hit rate", "observation": "0% cache hits",
            "evidence": {"comparison": None, "evidence_files": [
                {"path": str(evidence), "sha256": hashlib.sha256(evidence.read_bytes()).hexdigest()}]},
        }

    def publish(self, record):
        (self.work / (str(record["round"]) + ".json")).write_text(json.dumps(record), encoding="utf-8")

    def cli(self, command, *args):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout):
            experience.main([command, "--db", str(self.database), *args])
        return json.loads(stdout.getvalue())

    def add(self, number=1, *args):
        return self.cli("add", "--campaign", str(self.work), "--round", str(number), *args)

    def test_facts_and_append_only_notes_survive_retries(self):
        record = self.make_export()
        self.publish(record)
        self.assertTrue(self.add()["added_round"])
        changed = self.add(1, "--mechanism", "possible cache layout", "--scope-note", "8 ranks")
        self.assertFalse(changed["added_round"])
        self.assertTrue(changed["added_note"])
        self.assertFalse(self.add(1, "--mechanism", "possible cache layout", "--scope-note", "8 ranks")["added_note"])
        self.add(1, "--counterevidence", "rank 2 saw hits in campaign lab-2")
        matches = self.cli("search", "--campaign-id", "lab-1", "--scope", "model a",
                           "--decision", "revert", "--text", "RANK 2")
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["round_record"], record)
        self.assertEqual(len(matches[0]["agent_notes"]), 2)
        self.assertEqual(changed["export_sha256"], matches[0]["export_sha256"])
        self.assertEqual(len(self.cli("search", "--text", "0%")), 1)
        self.assertEqual(self.cli("search", "--decision", "adopt"), [])

    def test_edited_facts_do_not_overwrite_existing_record(self):
        record = self.make_export()
        self.publish(record)
        self.add()
        self.publish(dict(record, observation="95% cache hits"))
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.add(1, "--mechanism", "conflicting edit")
        result = self.cli("search")[0]
        self.assertEqual(result["round_record"], record)
        self.assertEqual(result["agent_notes"], [])

    def test_concurrent_retries_publish_one_round_and_one_note(self):
        record = self.make_export()
        note = {"mechanism": "cache layout"}
        with ThreadPoolExecutor(max_workers=4) as workers:
            jobs = [workers.submit(experience.save, self.database, self.work, record, note) for _ in range(4)]
            results = [job.result() for job in jobs]
        self.assertEqual(sum(item["added_round"] for item in results), 1)
        self.assertEqual(sum(item["added_note"] for item in results), 1)
        self.assertEqual(self.cli("search")[0]["agent_notes"], [note])

    def test_unfinished_or_malformed_rounds_leave_no_db(self):
        self.assertEqual(self.cli("search"), [])
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            self.add()
        record = self.make_export()
        for changes in ({"decision": "staged"}, {"evidence": {}}, {"candidate": {"config_sha256": "bad"}},
                        {"evidence": {"evidence_files": [{"path": "x", "sha256": "bad"}]}},
                        {"schema_version": 3}, {"scope": ""}):
            with self.subTest(changes=changes):
                self.publish(dict(record, **changes))
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                    self.add()
                self.assertFalse(self.database.exists())

    def test_abandoned_round_is_searchable_without_performance_verdict(self):
        self.publish(self.make_export(1))
        self.add()
        record = self.make_export(2, "abandon")
        record.update(hypothesis="", candidate=None)
        record["baseline"]["implementation_sha256"] = None
        self.publish(record)
        self.add(2)
        match = self.cli("search", "--decision", "abandon", "--round", "2", "--limit", "1")[0]
        self.assertIsNone(match["round_record"]["evidence"]["comparison"])
        self.assertEqual(self.cli("search", "--limit", "1")[0]["round_record"]["round"], 2)

    def test_cards_surface_positive_negative_and_unknown_conditions(self):
        context = self.work / "context.json"
        context.write_text('{"hardware":"910C","workload_family":"batch"}')
        for rnd, recommendation in enumerate(("adopt", "revert", "defer"), 1):
            record = self.make_export(rnd, "adopt" if recommendation == "adopt" else "revert")
            record["evidence"]["assessments"] = [{
                "recommendation": recommendation,
                "comparison": {
                    "input_valid": True, "recommendation": recommendation,
                    "pairs": [{"scenario": "holdout", "baseline_run_id": "a"}],
                    "scenarios": {"holdout": {"primary": {
                        "rule": {"metric": "output_token_throughput"},
                        "status": {"adopt": "passed", "revert": "regressed", "defer": "inconclusive"}[recommendation],
                    }}},
                    "validated_inputs": [{"manifest": {"run_id": "a", "client_measurement_policy": {
                        "model": "tiny", "concurrency": 8,
                    }}}],
                },
                "goodput": {"a": {"thresholds": {"max_ttft_s": 0.35}}},
            }]
            self.publish(record)
            self.add(rnd, "--context", str(context))
        context.write_text('{"hardware":"910C","concurrency":4,"cann":"unknown","max_ttft_s":0.35}')
        cards = self.cli("search", "--context", str(context), "--limit", "1")
        self.assertEqual([card["outcome"] for card in cards], ["positive", "negative", "inconclusive"])
        self.assertEqual(cards[0]["matches"], {"hardware": "910C", "max_ttft_s": 0.35})
        self.assertEqual(cards[0]["conflicts"], {"concurrency": {"query": 4, "record": 8}})
        self.assertEqual(cards[0]["unknown"], ["cann"])
        self.assertEqual(cards[0]["context_sources"]["hardware"], "operator_context")
        self.assertEqual(cards[0]["context"]["primary_metric"], "output_token_throughput")
        self.assertFalse(cards[0]["context"]["goodput_primary"])

    def test_sync_archives_only_closed_rounds_and_retries_one_missing_export(self):
        first, second = self.make_export(1), self.make_export(2, "adopt")
        self.publish(first)
        original = experience.campaign_output

        def response(directory, command, *options):
            if command == "history":
                return [{"round": 1, "action": "close"}, {"round": 1, "action": "close"},
                        {"round": 2, "action": "close"}, {"round": 3, "action": "begin"}]
            if command == "status":
                return {"campaign_id": "lab-1"}
            return original(directory, command, *options)

        with mock.patch.object(experience, "campaign_output", side_effect=response):
            progress = experience.sync(self.database, self.work)
            self.assertEqual([row["round"] for row in progress["rounds"]], [1])
            self.assertEqual([row["round"] for row in progress["errors"]], [2])
            self.publish(second)
            resumed = self.cli("sync", "--campaign", str(self.work))
        self.assertEqual(resumed["already_indexed_rounds"], [1])
        self.assertEqual([row["round"] for row in resumed["rounds"]], [2])
        self.assertEqual(resumed["errors"], [])


if __name__ == "__main__":
    unittest.main()
