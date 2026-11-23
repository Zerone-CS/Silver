#!/usr/bin/env python3
"""Read-only structural audit of vllmTKB1025 parameter YAML cards.

The field checks follow parse_params/schema.py::ParameterYAML. No pipeline,
model runtime, credential provider, LLM client or Pydantic module is imported.
PyYAML is loaded after CLI parsing, so --help also works without that package.
"""

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import sys


REQUIRED = (
    "name", "type", "category", "scope", "value_type", "default",
    "valid_choices", "performance_impact", "impact_detail",
)
ENUMS = {
    "type": {"cli", "env", "nested"},
    "category": {
        "scheduling", "memory", "parallelism", "communication", "quantization",
        "compilation", "speculation", "kv_cache", "model", "observability",
        "hardware", "other",
    },
    "scope": {"vllm", "vllm-ascend", "both"},
    "performance_impact": {"high", "medium", "low", "none"},
}
LIMITS = [
    "A pass checks local serialization, card structure, names and declared counts only.",
    "Card explanations, defaults, constraints and tuning advice are not fact-checked.",
    "Source commits are declarations; deployed-source compatibility is not verified.",
    "Full parameter coverage, successful LLM analysis and throughput benefit are not verified.",
]


def _import_yaml():
    import yaml
    return yaml


def sanitize_filename(name):
    """Match the current builder's filename rule, retaining dots and case."""
    safe = name[2:] if name.startswith("--") else name
    for before, after in (
        ("/", "_"), ("\\", "_"), ("-", "_"), ("*", "_star_"),
        ("?", "_"), ("<", "_lt_"), (">", "_gt_"), ("|", "_"),
        (":", "_"), ('"', "_"), ("'", "_"),
    ):
        safe = safe.replace(before, after)
    return re.sub(r"_+", "_", safe).strip("_") or "unnamed_parameter"


def _strict_loader(yaml):
    class UniqueKeyLoader(yaml.SafeLoader):
        pass

    def mapping(loader, node, deep=False):
        # Flatten legal YAML merges first; ambiguous repeated effective keys are
        # rejected rather than silently choosing one parameter definition.
        loader.flatten_mapping(node)
        result = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                repeated = key in result
            except TypeError:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "unhashable mapping key", key_node.start_mark,
                )
            if repeated:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping", node.start_mark,
                    "duplicate mapping key {!r}".format(key), key_node.start_mark,
                )
            result[key] = loader.construct_object(value_node, deep=deep)
        return result

    UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping
    )
    return UniqueKeyLoader


def _issue(report, severity, code, file, message):
    report[severity].append({"code": code, "file": str(file), "message": message})


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def _check_card(data, filename, report):
    """Validate persisted ParameterYAML structure, without coercing card data."""
    start = len(report["errors"])

    def error(message):
        _issue(report, "errors", "card_structure", filename, message)

    def string(value, field, nonempty=False):
        if not isinstance(value, str) or (nonempty and not value.strip()):
            error("{} must be {}string".format(field, "a nonempty " if nonempty else "a "))

    def strings(value, field):
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            error("{} must be a list of strings".format(field))
            return False
        return True

    def objects(value, field, required_strings, optional_strings=()):
        if not isinstance(value, list):
            error("{} must be a list of mappings".format(field))
            return
        for index, item in enumerate(value):
            label = "{}[{}]".format(field, index)
            if not isinstance(item, dict):
                error("{} must be a mapping".format(label))
                continue
            for key in required_strings:
                if key not in item:
                    error("{}.{} is required".format(label, key))
                else:
                    string(item[key], "{}.{}".format(label, key))
            for key in optional_strings:
                if key in item:
                    string(item[key], "{}.{}".format(label, key))

    for field in REQUIRED:
        if field not in data:
            error("{} is required by ParameterYAML".format(field))
    for field in ("name", "type", "category", "scope", "value_type", "performance_impact"):
        if field in data:
            string(data[field], field, nonempty=True)
    if "impact_detail" in data:
        string(data["impact_detail"], "impact_detail")
        if isinstance(data["impact_detail"], str) and not data["impact_detail"].strip():
            _issue(report, "warnings", "empty_explanation", filename,
                   "impact_detail is empty; the card provides no mechanism explanation")
    for field, allowed in ENUMS.items():
        if field in data and isinstance(data[field], str) and data[field] not in allowed:
            error("{} must be one of {}".format(field, ", ".join(sorted(allowed))))
    for field in ("source_file", "constraints"):
        if field in data:
            strings(data[field], field)
    if not data.get("source_file"):
        _issue(report, "warnings", "missing_source_locations", filename,
               "source_file is empty or absent; source evidence requires separate review")
    if "performance_scope" in data and strings(data["performance_scope"], "performance_scope"):
        invalid = set(data["performance_scope"]) - {"latency", "throughput", "memory"}
        if invalid:
            error("performance_scope has unknown values: {}".format(", ".join(sorted(invalid))))
    if "cli_example" in data and data["cli_example"] is not None:
        string(data["cli_example"], "cli_example")
    if "deprecated" in data and not isinstance(data["deprecated"], bool):
        error("deprecated must be a boolean")
    if "usage_locations" in data:
        objects(data["usage_locations"], "usage_locations", ("file",), ("context",))
    if "related_parameters" in data:
        objects(data["related_parameters"], "related_parameters", ("name",), ("relation",))
    if "tuning_advice" in data:
        advice = data["tuning_advice"]
        if not isinstance(advice, dict):
            error("tuning_advice must be a mapping")
        else:
            for field in ("summary", "quick_guide"):
                if field in advice:
                    string(advice[field], "tuning_advice." + field)
            if "caveats" in advice:
                strings(advice["caveats"], "tuning_advice.caveats")
            if "suggested_values" in advice:
                values = advice["suggested_values"]
                objects(values, "tuning_advice.suggested_values", ("scenario", "reason"))
                if isinstance(values, list):
                    for index, item in enumerate(values):
                        if isinstance(item, dict) and "value" not in item:
                            error("tuning_advice.suggested_values[{}].value is required".format(index))
    # The actual model coerces analysis_date to str; do not misreport legal YAML
    # date scalars as a schema error, but show the serialization difference.
    if "analysis_date" in data and not isinstance(data["analysis_date"], str):
        _issue(report, "warnings", "analysis_date_coercion", filename,
               "analysis_date is not serialized as a string; the builder normally normalizes it")
    return len(report["errors"]) == start


def _check_manifest(data, path, counts, report):
    if not isinstance(data, dict):
        _issue(report, "errors", "manifest_structure", path, "manifest must be a mapping")
        return

    def count(mapping, key, location):
        if key not in mapping:
            return None
        value = mapping[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            _issue(report, "errors", "manifest_structure", path,
                   "{} must be a nonnegative integer".format(location))
            return None
        return value

    declared = count(data, "output_yaml_count", "output_yaml_count")
    if declared is None and "output_yaml_count" not in data:
        _issue(report, "warnings", "manifest_count_missing", path,
               "manifest has no output_yaml_count; persisted-card count cannot be reconciled")
    elif declared is not None and declared != counts["yaml_files"]:
        _issue(report, "errors", "output_count_mismatch", path,
               "output_yaml_count={} but {} YAML files were found".format(declared, counts["yaml_files"]))
    stage2 = data.get("stage2_deep_analysis")
    if stage2 is None:
        _issue(report, "warnings", "manifest_analysis_missing", path,
               "manifest has no stage2_deep_analysis; analysis completeness is unknown")
        return
    if not isinstance(stage2, dict):
        _issue(report, "errors", "manifest_structure", path,
               "stage2_deep_analysis must be a mapping")
        return
    analyzed = count(stage2, "analyzed", "stage2_deep_analysis.analyzed")
    if analyzed is not None and analyzed != counts["yaml_files"]:
        _issue(report, "warnings", "analyzed_vs_written", path,
               "{} parameters were declared analyzed but {} YAML cards exist; skipped/none/error items and partial/resumed builds require separate reconciliation".format(analyzed, counts["yaml_files"]))
    perf = count(stage2, "confirmed_perf_related", "stage2_deep_analysis.confirmed_perf_related")
    if perf is not None and perf != counts["yaml_files"]:
        _issue(report, "warnings", "perf_vs_written", path,
               "confirmed_perf_related={} differs from {} persisted cards; inspect skipped items, filename collisions and build history".format(perf, counts["yaml_files"]))
    for impact in ("high", "medium", "low", "none"):
        field = "confirmed_not_perf" if impact == "none" else "performance_" + impact
        expected = count(stage2, field, "stage2_deep_analysis." + field)
        actual = counts["by_performance_impact"].get(impact, 0)
        # Non-performance items are intentionally not written by the builder.
        if impact != "none" and expected is not None and expected != actual:
            _issue(report, "warnings", "impact_count_mismatch", path,
                   "{}={} but {} persisted cards declare impact={} (counts alone do not identify the cause)".format(field, expected, actual, impact))
    errors = count(stage2, "errors", "stage2_deep_analysis.errors")
    if errors:
        _issue(report, "warnings", "analysis_errors_declared", path,
               "manifest declares {} analysis errors; resume does not automatically retry them".format(errors))
    stage1 = data.get("stage1_coarse_filter")
    if isinstance(stage1, dict):
        passed = count(stage1, "passed", "stage1_coarse_filter.passed")
        if passed is not None and analyzed is not None and passed != analyzed:
            _issue(report, "warnings", "partial_analysis", path,
                   "Stage 1 passed {} parameters but Stage 2 analyzed {}; full analysis is not established".format(passed, analyzed))


def inspect_snapshot(params_dir, manifest=None):
    params_dir = Path(params_dir)
    manifest = Path(manifest) if manifest is not None else None
    report = {
        "schema_version": 1,
        "status": "fail",
        "params_dir": str(params_dir.resolve()),
        "manifest": str(manifest.resolve()) if manifest is not None else None,
        "schema_basis": "vllmTKB1025/parse_params/schema.py::ParameterYAML",
        "counts": {
            "yaml_files": 0, "parsed_mappings": 0,
            "structurally_valid_cards": 0, "unique_names": 0,
            "by_type": {}, "by_scope": {}, "by_performance_impact": {},
        },
        "hashes": {"cards": {}, "manifest_sha256": None, "snapshot_sha256": None},
        "errors": [], "warnings": [], "limits": list(LIMITS),
    }
    try:
        yaml = _import_yaml()
    except ImportError:
        _issue(report, "errors", "dependency_missing", params_dir,
               "PyYAML is required for inspection; use a prepared environment with pyyaml installed")
        return report
    loader = _strict_loader(yaml)
    if not params_dir.is_dir():
        _issue(report, "errors", "params_directory", params_dir,
               "params-dir must be an existing directory")
        return report
    try:
        files = sorted(params_dir.glob("*.yaml"))
    except OSError as exc:
        _issue(report, "errors", "params_directory", params_dir, str(exc))
        return report
    counts = report["counts"]
    counts["yaml_files"] = len(files)
    if not files:
        _issue(report, "errors", "empty_snapshot", params_dir,
               "no *.yaml parameter cards found")
    names = {}
    filenames = {}
    aliases = {}
    by_type, by_scope, by_impact = Counter(), Counter(), Counter()
    for path in files:
        label = path.name
        before = len(report["errors"])
        try:
            raw = path.read_bytes()
            report["hashes"]["cards"][label] = hashlib.sha256(raw).hexdigest()
            data = yaml.load(raw.decode("utf-8"), Loader=loader)
        except (OSError, UnicodeError, yaml.YAMLError, ValueError, RecursionError) as exc:
            _issue(report, "errors", "card_parse", label, str(exc))
            continue
        if not isinstance(data, dict):
            _issue(report, "errors", "card_structure", label,
                   "each parameter card must be a YAML mapping")
            continue
        counts["parsed_mappings"] += 1
        _check_card(data, label, report)
        name = data.get("name")
        if _nonempty_string(name):
            if name in names:
                _issue(report, "errors", "duplicate_name", label,
                       "name {!r} is also declared in {}".format(name, names[name]))
            else:
                names[name] = label
            expected = sanitize_filename(name) + ".yaml"
            if path.name != expected:
                _issue(report, "errors", "filename_mismatch", label,
                       "builder filename for {!r} is {!r}; case and dots are preserved".format(name, expected))
            if expected in filenames and filenames[expected][0] != name:
                other_name, other_file = filenames[expected]
                _issue(report, "errors", "filename_collision", label,
                       "{!r} and {!r} ({}) sanitize to the same filename {!r}".format(other_name, name, other_file, expected))
            else:
                filenames[expected] = (name, label)
            identity = (name[2:] if name.startswith("--") else name).replace("-", "_")
            if identity in aliases and aliases[identity][0] != name:
                other_name, other_file = aliases[identity]
                _issue(report, "warnings", "possible_parameter_alias", label,
                       "{!r} and {!r} ({}) have the same identity after flag-prefix/hyphen normalization; inspect source definitions before merging or assuming independence".format(other_name, name, other_file))
            else:
                aliases[identity] = (name, label)
        for field, counter in (("type", by_type), ("scope", by_scope), ("performance_impact", by_impact)):
            value = data.get(field)
            if isinstance(value, str):
                counter[value] += 1
        if len(report["errors"]) == before:
            counts["structurally_valid_cards"] += 1
    counts["unique_names"] = len(names)
    counts["by_type"] = dict(sorted(by_type.items()))
    counts["by_scope"] = dict(sorted(by_scope.items()))
    counts["by_performance_impact"] = dict(sorted(by_impact.items()))
    if manifest is not None:
        try:
            raw = manifest.read_bytes()
            report["hashes"]["manifest_sha256"] = hashlib.sha256(raw).hexdigest()
            data = yaml.load(raw.decode("utf-8"), Loader=loader)
            _check_manifest(data, manifest, counts, report)
        except (OSError, UnicodeError, yaml.YAMLError, ValueError, RecursionError) as exc:
            _issue(report, "errors", "manifest_parse", manifest, str(exc))
    else:
        _issue(report, "warnings", "manifest_not_supplied", params_dir,
               "no manifest was supplied; build counts and declared source identity were not inspected")
    identity = {
        "cards": report["hashes"]["cards"],
        "manifest_sha256": report["hashes"]["manifest_sha256"],
    }
    report["hashes"]["snapshot_sha256"] = hashlib.sha256(
        json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    report["status"] = "fail" if report["errors"] else "pass"
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--params-dir", required=True, type=Path, help="Directory containing parameter *.yaml cards (read only)")
    parser.add_argument("--manifest", type=Path, help="Optional existing build manifest.yaml (read only)")
    parser.add_argument("--output", required=True, type=Path, help="New JSON report; existing files are never overwritten")
    args = parser.parse_args(argv)
    if args.output.exists() or args.output.is_symlink():
        print("error: output already exists: {}".format(args.output), file=sys.stderr)
        return 2
    report = inspect_snapshot(args.params_dir, args.manifest)
    try:
        with args.output.open("x", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
            handle.write("\n")
    except (OSError, ValueError) as exc:
        print("error: cannot create JSON report: {}".format(exc), file=sys.stderr)
        return 2
    print("{}: {} cards, {} errors, {} warnings; report: {}".format(
        report["status"], report["counts"]["yaml_files"], len(report["errors"]),
        len(report["warnings"]), args.output,
    ))
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    sys.exit(main())
