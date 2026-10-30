#!/usr/bin/env python3
"""Run a same-runner release read-path comparison for RocksDB #15439."""

from __future__ import annotations

import argparse
import csv
import io
import json
import math
import sys
from pathlib import Path

import run_rocksdb_prefix as shared

ROOT = Path(__file__).resolve().parent
BENCH = ROOT / "rocksdb_prefix_seek_bench.cc"
TESTS_PATCH = ROOT / "patches/rocksdb-prefix-15439/TESTS_ONLY.patch"
CANDIDATE_PATCH = ROOT / "patches/rocksdb-prefix-15439/CANDIDATE.patch"
MANIFEST = ROOT / "patches/rocksdb-prefix-15439/candidate-manifest.json"
EXPECTED = {
    (cmp, mode, workload)
    for cmp in ("bytewise", "reverse_bytewise")
    for mode in ("off", "on")
    for workload in (
        "matching_existing",
        "matching_gap",
        "mismatch_before_range",
        "mismatch_after_range",
        "tail_mismatch_before_range",
        "tail_mismatch_after_range",
    )
}


def parse_output(text: str, diff_sha: str, library_sha: str) -> tuple[dict, dict]:
    lines = text.splitlines()
    header = next((i for i, line in enumerate(lines) if line.startswith("comparator,")), None)
    if header is None:
        raise RuntimeError("benchmark output has no CSV header")
    meta = dict(line.split("=", 1) for line in lines[:header] if "=" in line)
    expected = {
        "input_source_git_sha": shared.SOURCE_PIN,
        "input_product_diff_sha256": diff_sha,
        "input_librocksdb_sha256": library_sha,
    }
    for key, value in expected.items():
        if meta.get(key) != value:
            raise RuntimeError(f"benchmark input metadata mismatch: {key}")
    rows = {}
    for row in csv.DictReader(io.StringIO("\n".join(lines[header:]))):
        key = (row["comparator"], row["prefix_mode"], row["workload"])
        if key not in EXPECTED or key in rows:
            raise RuntimeError(f"unexpected or duplicate benchmark row: {key}")
        parsed = {
            "prefix_bytes": int(row["prefix_bytes"]),
            "block_bytes": int(row["block_bytes"]),
            "iterations": int(row["iterations"]),
            "repeats": int(row["repeats"]),
            "min": float(row["ns_per_seek_min"]),
            "median": float(row["ns_per_seek_median"]),
            "max": float(row["ns_per_seek_max"]),
            "checksum": int(row["checksum"]),
        }
        if parsed["iterations"] < 1000 or parsed["repeats"] != 3:
            raise RuntimeError(f"invalid iteration metadata: {key}")
        timings = (parsed["min"], parsed["median"], parsed["max"])
        if parsed["block_bytes"] <= 0 or not all(
            math.isfinite(value) and value > 0 for value in timings
        ) or not timings[0] <= timings[1] <= timings[2]:
            raise RuntimeError(f"invalid block size or timing: {key}")
        rows[key] = parsed
    if set(rows) != EXPECTED:
        raise RuntimeError(f"expected 24 rows, received {len(rows)}")
    logical_prefix = int(meta.get("logical_common_user_prefix_bytes", "0"))
    tail_shared = int(meta.get("tail_mismatch_shared_prefix_bytes", "-1"))
    mismatch_offset = int(meta.get("tail_mismatch_offset_from_prefix_end", "-1"))
    if (meta.get("format_version") != "8" or meta.get("entries") != "128"
            or meta.get("user_key_prefix_bytes") != "64"
            or mismatch_offset != 4
            or tail_shared != logical_prefix - mismatch_offset
            or not logical_prefix - 8 <= tail_shared < logical_prefix):
        raise RuntimeError("benchmark fixture/workload metadata differs from plan")
    for comparator, mode, workload in EXPECTED:
        expected_prefix = logical_prefix if mode == "on" else 0
        if rows[(comparator, mode, workload)]["prefix_bytes"] != expected_prefix:
            raise RuntimeError(f"block prefix length differs from key oracle: {comparator}/{mode}")
    return meta, rows


def serializable_rows(rows: dict) -> list[dict]:
    return [
        {"comparator": key[0], "prefix_mode": key[1], "workload": key[2], **rows[key]}
        for key in sorted(rows)
    ]


def pair_rows(before_rows: dict, candidate_rows: dict, after_rows: dict) -> list[dict]:
    pairs = []
    for key in sorted(EXPECTED):
        before, candidate, after = (
            before_rows[key], candidate_rows[key], after_rows[key]
        )
        for field in ("checksum", "prefix_bytes", "block_bytes", "iterations", "repeats"):
            if before[field] != candidate[field] or before[field] != after[field]:
                raise RuntimeError(f"A/B/A metadata or checksum mismatch: {key}/{field}")
        pairs.append({
            "comparator": key[0], "prefix_mode": key[1], "workload": key[2],
            "checksum": before["checksum"], "prefix_bytes": before["prefix_bytes"],
            "block_bytes": before["block_bytes"], "iterations": before["iterations"],
            "repeats": before["repeats"],
            "baseline_before_ns_per_seek_median": before["median"],
            "candidate_ns_per_seek_median": candidate["median"],
            "baseline_after_ns_per_seek_median": after["median"],
            "candidate_over_baseline_before_median_ratio": candidate["median"] / before["median"],
            "candidate_over_baseline_after_median_ratio": candidate["median"] / after["median"],
            "baseline_after_over_before_median_ratio": after["median"] / before["median"],
            "baseline_range_ns": [before["min"], before["max"]],
            "candidate_range_ns": [candidate["min"], candidate["max"]],
            "baseline_after_range_ns": [after["min"], after["max"]],
        })
    return pairs


def run_binary(receipt: dict, source: Path, logs: Path, env: dict[str, str],
               stage: str, binary: Path, library_sha: str, diff_sha: str,
               iterations: int, artifacts: Path, binary_origin: str) -> tuple[dict, dict]:
    result = shared.run_command(
        receipt, f"{stage}-seek-benchmark",
        [str(binary), shared.SOURCE_PIN, diff_sha, library_sha, str(iterations)],
        source, logs, timeout=60, env=env,
    )
    if not result["ok"]:
        raise RuntimeError(f"{stage} benchmark failed or timed out at 60 seconds")
    text = result["stdout"].decode("utf-8", errors="strict")
    (artifacts / f"{stage}.stdout.txt").write_text(text, encoding="utf-8")
    (artifacts / f"{stage}.stderr.txt").write_bytes(result["stderr"])
    meta, rows = parse_output(text, diff_sha, library_sha)
    receipt.setdefault("benchmarks", {})[stage] = {
        "binary_sha256": shared.sha256_file(binary),
        "binary_origin": binary_origin,
        "library_sha256": library_sha,
        "source_diff_sha256": diff_sha,
        "metadata": meta,
        "rows": serializable_rows(rows),
        "timeout_seconds": 60,
    }
    shared.write_json(artifacts / "benchmark-comparison.json", receipt)
    return meta, rows


def compile_and_run(receipt: dict, source: Path, bins: Path, logs: Path,
                    env: dict[str, str], stage: str, library_sha: str,
                    diff_sha: str, iterations: int, artifacts: Path) -> tuple[Path, dict, dict]:
    binary = shared.compile_program(
        receipt, source, bins, logs, env, f"prefix-{stage}-seek-bench", BENCH
    )
    meta, rows = run_binary(
        receipt, source, logs, env, stage, binary, library_sha, diff_sha,
        iterations, artifacts, "compiled and linked at this stage",
    )
    return binary, meta, rows


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=200000)
    args = parser.parse_args()
    if not 1000 <= args.iterations <= 1000000:
        parser.error("--iterations must be between 1000 and 1000000")

    source, artifacts = args.source.resolve(), args.artifacts.resolve()
    logs, bins = artifacts / "logs", artifacts / "bin"
    logs.mkdir(parents=True, exist_ok=True)
    bins.mkdir(parents=True, exist_ok=True)
    receipt = {
        "status": "running", "issue": shared.ISSUE_URL,
        "phase": "same-runner-release-read-path-benchmark",
        "started_utc": shared.utc(), "source_pin": shared.SOURCE_PIN,
        "iterations_per_repeat": args.iterations, "repeats": 3,
        "bench_source_sha256": shared.sha256_file(BENCH),
        "system": shared.capture_runtime_metadata(source),
        "commands": {}, "libraries": {}, "helper_compiles": {},
    }
    shared.write_json(artifacts / "benchmark-comparison.json", receipt)
    try:
        receipt["source_before_baseline"] = shared.verify_source_pin(source)
        env = shared.make_env()
        shared.build_static_library(receipt, source, logs, env, "bench-baseline-release")
        clean = shared.source_snapshot(source)
        if clean["head"] != shared.SOURCE_PIN or clean["status_paths"]:
            raise RuntimeError("baseline build changed the pinned clean source")
        receipt["baseline_source_after_build"] = clean
        base_lib = shared.sha256_file(source / "librocksdb.a")
        base_config = shared.make_config_facts(source)
        receipt.setdefault("release_builds", {})["baseline"] = {
            "library_sha256": base_lib, "make_config": base_config,
            "debug_level": 0, "make_jobs": 2,
        }
        baseline_binary, base_meta, base_rows = compile_and_run(
            receipt, source, bins, logs, env, "baseline", base_lib,
            "none", args.iterations, artifacts,
        )

        manifest = shared.verify_candidate_manifest(
            source, MANIFEST, TESTS_PATCH, CANDIDATE_PATCH,
            shared.EXPECTED_CANDIDATE_PATHS,
        )
        paths, patch_info = shared.apply_review_patch(
            receipt, source, logs, env, CANDIDATE_PATCH,
            "bench-candidate", shared.EXPECTED_CANDIDATE_PATHS,
        )
        patch_blobs = shared.verify_candidate_blobs(source, manifest, paths)
        receipt["candidate_patch"] = {
            **patch_info, "verified_git_blobs": patch_blobs,
            "manifest_patch_sha256": manifest["candidate_patch_sha256"],
        }
        shared.build_static_library(receipt, source, logs, env, "bench-candidate-release")
        after = shared.source_snapshot(source, paths)
        if after["head"] != shared.SOURCE_PIN or set(after["status_paths"]) != paths:
            raise RuntimeError("candidate build changed paths outside the frozen patch")
        if shared.verify_candidate_blobs(source, manifest, paths) != patch_blobs:
            raise RuntimeError("candidate build changed a frozen source blob")
        receipt["candidate_source_after_build"] = after
        candidate_lib = shared.sha256_file(source / "librocksdb.a")
        candidate_config = shared.make_config_facts(source)
        if candidate_config.get("sha256") != base_config.get("sha256"):
            raise RuntimeError("baseline/candidate release build configurations differ")
        receipt["release_builds"]["candidate"] = {
            "library_sha256": candidate_lib, "make_config": candidate_config,
            "debug_level": 0, "make_jobs": 2,
        }
        candidate_meta, candidate_rows = compile_and_run(
            receipt, source, bins, logs, env, "candidate", candidate_lib,
            manifest["candidate_patch_sha256"], args.iterations, artifacts,
        )

        # Reuse the already-linked baseline executable after the candidate run.
        # The checkout now contains the candidate patch, but this A2 binary and
        # the retained executable still contains the original baseline code.
        base_again_meta, base_again_rows = run_binary(
            receipt, source, logs, env, "baseline_repeat", baseline_binary,
            base_lib, "none", args.iterations, artifacts,
            "retained baseline-linked executable; candidate patch is in checkout",
        )
        if (receipt["benchmarks"]["baseline_repeat"]["binary_sha256"]
                != receipt["benchmarks"]["baseline"]["binary_sha256"]):
            raise RuntimeError("A2 did not reuse the retained baseline executable")

        for meta in (candidate_meta, base_again_meta):
            if (base_meta.get("compiler") != meta.get("compiler")
                    or base_meta.get("target_platform") != meta.get("target_platform")):
                raise RuntimeError("baseline/candidate compiler or target metadata differ")
            for field in (
                "format_version", "entries", "user_key_prefix_bytes",
                "logical_common_user_prefix_bytes",
                "tail_mismatch_offset_from_prefix_end",
                "tail_mismatch_shared_prefix_bytes", "restart_interval",
                "iterations_per_repeat", "measured_repeats",
            ):
                if base_meta.get(field) != meta.get(field):
                    raise RuntimeError(f"A/B/A benchmark metadata differs: {field}")
        pairs = pair_rows(base_rows, candidate_rows, base_again_rows)
        receipt["comparison"] = {
            "rows": len(pairs), "checksums_match_across_aba": True,
            "baseline_before_metadata": base_meta,
            "candidate_metadata": candidate_meta,
            "baseline_after_metadata": base_again_meta,
            "pairs": pairs,
        }
        receipt["status"] = "benchmark_complete"
        receipt["finished_utc"] = shared.utc()
        shared.write_json(artifacts / "benchmark-comparison.json", receipt)
        print(json.dumps({"status": receipt["status"], "rows": len(pairs),
                          "checksums_match_across_aba": True}, sort_keys=True), flush=True)
        return 0
    except Exception as error:
        receipt["status"], receipt["error"] = "failed", repr(error)
        receipt["finished_utc"] = shared.utc()
        shared.write_json(artifacts / "benchmark-comparison.json", receipt)
        print(f"benchmark failed: {error}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
