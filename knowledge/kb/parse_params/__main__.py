"""Entry point for parse_params: orchestrate Stage 1 + Stage 2 + manifest generation.

Usage:
    python -m parse_params --input parameters.json --output output/
    python -m parse_params --input parameters.json --output output/ --resume
    python -m parse_params --input parameters.json --output output/ --dry-run-stage1
    python -m parse_params --input parameters.json --output output/ --max-params 20
    python -m parse_params --input parameters.json --output output/ --concurrency 15
    python -m parse_params --input parameters.json --output output/ --verbose
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import shutil
import signal
import sys
from pathlib import Path

from . import config
from .manifest import generate_manifest
from .progress import ProgressManager
from .stage1_filter import run_stage1
from .utils import ensure_dir, load_params, setup_logging, sanitize_filename


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="parse_params — vLLM/vllm-ascend parameter performance analysis",
    )
    parser.add_argument(
        "--input", "-i",
        type=Path,
        required=True,
        help="Explicit parameters.json input",
    )
    parser.add_argument(
        "--output", "-o",
        type=Path,
        required=True,
        help="New output directory; reuse only with an explicit matching --resume",
    )
    parser.add_argument(
        "--resume", "-r",
        action="store_true",
        help="Resume from progress.json, skipping already-processed parameters",
    )
    parser.add_argument(
        "--dry-run-stage1",
        action="store_true",
        help="Only run Stage 1, print pass/skip summary, and exit",
    )
    parser.add_argument(
        "--max-params", "-n",
        type=int,
        default=0,
        help="Limit Stage 2 to N parameters (0 = unlimited, for testing)",
    )
    parser.add_argument(
        "--concurrency", "-c",
        type=int,
        default=config.LLM_CONCURRENCY,
        help=f"Number of concurrent LLM API calls (default: {config.LLM_CONCURRENCY})",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    parser.add_argument("--vllm-src", type=Path, required=True, help="vLLM source repository root")
    parser.add_argument("--vllm-ascend-src", type=Path, required=True, help="vllm-ascend source repository root")
    args = parser.parse_args()
    if args.max_params < 0 or args.concurrency < 1:
        parser.error("--max-params must be >= 0 and --concurrency must be >= 1")
    return args


async def main_async(args: argparse.Namespace) -> None:
    logger = setup_logging(args.verbose)

    # Override concurrency from CLI
    config.LLM_CONCURRENCY = args.concurrency

    # Preflight before writing anything: no implicit source, output, or API settings.
    from kb_runtime import builder_identity, check_output, file_sha256, llm_environment, validate_sources, write_json_new
    output_dir = check_output(args.output, [args.vllm_src, args.vllm_ascend_src], resume=args.resume)
    if not args.input.is_file():
        raise ValueError("Input parameters file does not exist: " + str(args.input))
    roots, source_inputs = validate_sources(args.vllm_src, args.vllm_ascend_src)
    config.VLLM_ROOT = roots["vllm"]
    config.VLLM_ASCEND_ROOT = roots["vllm-ascend"]
    config.SOURCE_COMMIT_VLLM = source_inputs["vllm"]["commit"]
    config.SOURCE_COMMIT_VLLM_ASCEND = source_inputs["vllm-ascend"]["commit"]
    config.DOC_SEARCH_DIRS = [(roots["vllm-ascend"] / "docs" / "source", "vllm-ascend")]
    build_inputs = {
        "input_parameters_file": str(args.input.resolve()),
        "input_sha256": file_sha256(args.input),
        "source_inputs": source_inputs,
        "builder": builder_identity("stage1-only" if args.dry_run_stage1 else "llm", args.max_params, args.concurrency),
    }
    provenance_path = output_dir / "build-inputs.json"
    if args.resume:
        if not provenance_path.is_file() or not (output_dir / "progress.json").is_file():
            raise ValueError("--resume requires build-inputs.json and progress.json from this bundled tool")
        if json.loads(provenance_path.read_text()) != build_inputs:
            raise ValueError("--resume input/source or builder identity differs; start a new output directory")
        # Do not silently reset corrupted progress.
        progress_data = json.loads((output_dir / "progress.json").read_text())
        if not isinstance(progress_data, dict) or not all(isinstance(progress_data.get(k), list) for k in ("stage1_passed", "stage1_skipped", "processed_params", "error_params")):
            raise ValueError("--resume progress.json is invalid")
        if not isinstance(progress_data.get("stage2_results", {}), dict):
            raise ValueError("--resume Stage 2 terminal results are invalid")
    params = load_params(args.input, logger)
    if not params:
        raise ValueError("No parameters found in input file")
    if not isinstance(params, list) or any(not isinstance(p, dict) or not isinstance(p.get("name"), str) for p in params):
        raise ValueError("Input must be a list of parameter objects with string names")
    if len({p["name"] for p in params}) != len(params):
        raise ValueError("Duplicate parameter names in input")
    # Reject filename collisions before any build writes or LLM initialization.
    # Test every stage-1 candidate, including candidates beyond --max-params.
    preflight_passed, _ = run_stage1(params, config.STAGE1_RULES_PATH, logger)
    filenames = {}
    for param in preflight_passed:
        filename = sanitize_filename(param["name"]) + ".yaml"
        if filename in filenames:
            raise ValueError("Sanitized parameter filename collision: " + repr(filenames[filename]) + " and " + repr(param["name"]) + " both map to " + filename)
        filenames[filename] = param["name"]
    if not args.dry_run_stage1:
        llm_environment(required=True)
        # Heavy optional dependencies are not required for help/rule-only dry runs.
        from .stage2_analyzer import Stage2Analyzer
    logs_dir = output_dir / "logs"
    progress_path = output_dir / "progress.json"
    if not args.resume:
        output_dir.mkdir(parents=True, exist_ok=False)
        write_json_new(provenance_path, build_inputs)
    ensure_dir(logs_dir)

    # Copy schema to output for traceability
    schema_dest = output_dir / "schema.yaml"
    ensure_dir(output_dir)
    if config.SCHEMA_YAML_PATH.exists():
        shutil.copy2(config.SCHEMA_YAML_PATH, schema_dest)
        logger.info("Schema copied to %s", schema_dest)

    logger.info("Loaded %d parameters from %s", len(params), args.input)

    # Initialize progress manager
    progress = ProgressManager(progress_path, logger)

    # --- Stage 1: Coarse filter ---
    if args.resume and progress.data.get("stage1_passed"):
        logger.info("Resuming: using saved Stage 1 results (%d passed, %d skipped)",
                    len(progress.data["stage1_passed"]),
                    len(progress.data["stage1_skipped"]))
        reason_map: dict[str, str | None] = {}
        for s in progress.data["stage1_skipped"]:
            if isinstance(s, dict):
                reason_map[s.get("name")] = s.get("skip_reason")
            else:
                reason_map[str(s)] = None
        stage1_passed = [p for p in params if p["name"] in progress.data["stage1_passed"]]
        stage1_skipped = [
            {**p, "skip_reason": reason_map.get(p["name"])}
            for p in params
            if p["name"] in reason_map
        ]
    else:
        stage1_passed, stage1_skipped = run_stage1(
            params, config.STAGE1_RULES_PATH, logger
        )
        progress.set_stage1_results(stage1_passed, stage1_skipped)

    # Write Stage 1 skipped log
    if stage1_skipped:
        skip_log_path = logs_dir / "stage1_skipped.json"
        with open(skip_log_path, "w", encoding="utf-8") as f:
            json.dump(stage1_skipped, f, indent=2, ensure_ascii=False)
        logger.info("Stage 1 skipped %d params written to %s",
                    len(stage1_skipped), skip_log_path)

    if args.dry_run_stage1:
        logger.info("=== Stage 1 Dry Run Summary ===")
        logger.info("Total: %d", len(params))
        logger.info("Passed to Stage 2: %d", len(stage1_passed))
        logger.info("Skipped: %d", len(stage1_skipped))
        logger.info("Skipped categories: %s",
                    {p.get("category") for p in stage1_skipped})
        return

    # --- Stage 2: LLM Deep Analysis ---
    stage2_params = stage1_passed
    if args.max_params > 0:
        stage2_params = stage2_params[:args.max_params]
        logger.info("Limited to %d parameters (--max-params)", len(stage2_params))

    analyzer = Stage2Analyzer(output_dir, logs_dir, logger)
    stage2_results = await analyzer.run(stage2_params, progress)

    # --- Post-processing: Generate manifest ---
    manifest_path = generate_manifest(
        total_params=len(params),
        stage1_passed=len(stage1_passed),
        stage1_skipped=len(stage1_skipped),
        stage2_results=stage2_results,
        output_dir=output_dir,
        logger=logger,
        source_commit_vllm=config.SOURCE_COMMIT_VLLM,
        source_commit_vllm_ascend=config.SOURCE_COMMIT_VLLM_ASCEND,
        source_inputs=source_inputs,
        input_parameters_file=str(args.input.resolve()),
    )

    # --- Summary ---
    impact_counts = {"high": 0, "medium": 0, "low": 0, "none": 0}
    errors = 0
    for r in stage2_results:
        if r.get("status") != "ok":
            errors += 1
        else:
            impact = r.get("performance_impact", "none")
            impact_counts[impact] = impact_counts.get(impact, 0) + 1

    logger.info("=" * 60)
    if errors:
        logger.error("Parse failed: %d Stage 2 error(s); evidence retained for review.", errors)
    else:
        logger.info("Parse complete!")
    logger.info("  Input:            %d params", len(params))
    logger.info("  Stage 1 skipped:  %d", len(stage1_skipped))
    logger.info("  Stage 2 analyzed: %d", len(stage2_results))
    logger.info("  ── high:          %d", impact_counts["high"])
    logger.info("  ── medium:        %d", impact_counts["medium"])
    logger.info("  ── low:           %d", impact_counts["low"])
    logger.info("  ── none:          %d", impact_counts["none"])
    logger.info("  Errors:           %d", errors)
    logger.info("  Output:           %s", output_dir)
    logger.info("  Manifest:         %s", manifest_path)
    logger.info("=" * 60)
    if errors:
        raise ValueError("Stage 2 contains errors; inspect manifest.yaml, progress.json and logs/analysis_errors.json")


def main() -> None:
    args = parse_args()

    # Handle SIGINT gracefully
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    def sigint_handler():
        print("\nInterrupted. Progress saved to progress.json. Use --resume to continue.", file=sys.stderr)
        loop.stop()

    loop.add_signal_handler(signal.SIGINT, sigint_handler)

    try:
        loop.run_until_complete(main_async(args))
    except (ValueError, OSError, ImportError) as exc:
        print("parse_params: error: " + str(exc), file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("\nInterrupted. Progress saved to progress.json. Use --resume to continue.", file=sys.stderr)
    finally:
        loop.close()


if __name__ == "__main__":
    main()
