#!/usr/bin/env python3
"""Compare recorded full-check failures on pinned base and the V2 candidate."""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import subprocess
from pathlib import Path

import run_rocksdb_prefix as shared
import run_rocksdb_prefix_full_check as full

ROOT = Path(__file__).resolve().parent
CASES_FILE = ROOT / "controls/rocksdb-prefix-15439/failure-cases.json"
PATCH = ROOT / "patches/rocksdb-prefix-15439/CANDIDATE.patch"
MANIFEST = ROOT / "patches/rocksdb-prefix-15439/candidate-manifest.json"
SOURCE_PIN = shared.SOURCE_PIN
PATCH_SHA256 = "cac9fade4b4bbb5c11fc0884e7febdd512734fe8c0a82d4678ab0a360b07cbd5"
CASE_TIMEOUT = 60
BUILD_TIMEOUT = 35 * 60


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_cases(mode: str) -> tuple[dict, list[dict]]:
    document = json.loads(CASES_FILE.read_text(encoding="utf-8"))
    if document.get("source_pin") != SOURCE_PIN or document.get("candidate_patch_sha256") != PATCH_SHA256:
        raise RuntimeError("failure manifest does not match the pinned source and V2 candidate")
    cases = list(document["normal_cases"] if mode == "normal" else document["status_checked_cases"])
    if not cases:
        raise RuntimeError(f"no failure-control cases listed for {mode}")
    for case in cases:
        if not isinstance(case.get("binary"), str) or not re.fullmatch(r"[a-z][a-z0-9_]*", case["binary"]):
            raise RuntimeError(f"invalid test binary in case manifest: {case}")
        if not isinstance(case.get("gtest_filter"), str) or not re.fullmatch(r"[A-Za-z0-9_./]+", case["gtest_filter"]):
            raise RuntimeError(f"invalid literal GTest filter in case manifest: {case}")
        if case.get("repetitions", 1) not in (1, 2, 3):
            raise RuntimeError(f"invalid diagnostic repetition count: {case}")
    return document, cases


def build_targets(source: Path, logs: Path, env: dict[str, str],
                  mode: str, stage: str, binaries: list[str], receipt: dict) -> dict:
    command = [
        "make", "AUTO_CLEAN=1", "-j2", "J=2", "V=1",
        "DEBUG_LEVEL=1", "LIB_MODE=shared", "NCORES=2",
    ]
    if mode == "status-checked":
        command.append("ASSERT_STATUS_CHECKED=1")
    command.extend(binaries)
    build = shared.run_command(
        receipt, f"{stage}-build", command, source, logs,
        timeout=BUILD_TIMEOUT, env=env,
    )
    if not build["ok"]:
        raise RuntimeError(f"{stage} test binaries failed to build")
    config = shared.make_config_facts(source)
    flags = "\n".join(config.get("selected_flag_lines", []))
    if "-DSNAPPY" not in flags:
        raise RuntimeError(f"{stage} shared library does not have Snappy enabled")
    return {
        "command": command,
        "make_config": config,
        "binary_sha256": {name: shared.sha256_file(source / name) for name in binaries},
        "compile_stdout_gzip": build["stdout_log_gzip"],
    }


def run_case(source: Path, artifacts: Path, logs: Path, env: dict[str, str],
             mode: str, stage: str, case: dict, index: int, repetition: int,
             receipt: dict) -> dict:
    temp = artifacts / "test-tmp" / mode / stage / f"case-{index:03d}-{repetition}"
    temp.mkdir(parents=True, exist_ok=True)
    test_env = dict(env)
    test_env["TEST_TMPDIR"] = str(temp)
    test_env["GTEST_COLOR"] = "no"
    command = [
        "timeout", "--kill-after=5s", "60s", "bash", "-c", 'ulimit -c 0; exec "$@"', "rocksdb-control",
        str(source / case["binary"]), f"--gtest_filter={case['gtest_filter']}",
    ]
    run = shared.run_command(
        receipt, f"{stage}-case-{index:03d}-{repetition}", command,
        source, logs, timeout=CASE_TIMEOUT + 10, env=test_env, expected_exit=None,
    )
    text = "\n".join(
        gzip.decompress((artifacts / run[f"{stream}_log_gzip"]).read_bytes())
        .decode("utf-8", errors="replace")
        for stream in ("stdout", "stderr")
    )
    ran = f"[ RUN      ] {case['gtest_filter']}" in text
    passed = f"[       OK ] {case['gtest_filter']}" in text
    failed = f"[  FAILED  ] {case['gtest_filter']}" in text
    if run["timed_out"] or run["exit_code"] == 124:
        outcome = "timeout"
    elif not ran:
        outcome = "test_not_started_or_harness_error"
    elif run["exit_code"] == 0 and passed:
        outcome = "passed"
    elif failed:
        outcome = "assertion_failure"
    elif "Assertion" in text or run["exit_code"] in (-6, -11, 134, 139):
        outcome = "abort_or_signal"
    else:
        outcome = "unexpected_process_exit"
    return {
        "mode": mode, "stage": stage, "binary": case["binary"],
        "gtest_filter": case["gtest_filter"], "repetition": repetition,
        "exit_code": run["exit_code"], "timed_out": run["timed_out"],
        "outcome": outcome, "run_marker": ran, "passed_marker": passed,
        "failed_marker": failed, "source_full_run_observations": case,
        "stdout_gzip": run["stdout_log_gzip"],
        "stderr_gzip": run["stderr_log_gzip"],
        "stdout_tail": run["stdout_tail"], "stderr_tail": run["stderr_tail"],
    }


def apply_candidate(source: Path, manifest: dict,
                    baseline_artifacts: dict[str, dict]) -> dict:
    if full.git(source, "rev-parse", "HEAD") != SOURCE_PIN:
        raise RuntimeError("pinned source changed before the candidate control")
    check = subprocess.run(
        ["git", "-C", str(source), "apply", "--check", str(PATCH)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check.returncode:
        raise RuntimeError(f"candidate patch does not apply: {check.stderr[-1200:]}")
    applied = subprocess.run(
        ["git", "-C", str(source), "apply", str(PATCH)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if applied.returncode:
        raise RuntimeError(f"candidate patch application failed: {applied.stderr[-1200:]}")
    state = source_state(source)
    if set(state["tracked_paths"]) != EXPECTED_PATHS:
        raise RuntimeError(f"candidate patch paths changed: {state['tracked_paths']}")
    if state["generated_build_artifacts"] != baseline_artifacts:
        raise RuntimeError("applying the candidate changed an unrelated build artifact")
    blobs = shared.verify_candidate_blobs(source, manifest, EXPECTED_PATHS)
    return {
        "base": SOURCE_PIN,
        "patch_sha256": PATCH_SHA256,
        "blobs": blobs,
        "generated_build_artifacts": baseline_artifacts,
    }


EXPECTED_PATHS = set(shared.EXPECTED_CANDIDATE_PATHS)


def source_state(source: Path) -> dict:
    status = full.source_status(source)
    artifact = {}
    for relative in set(status) - EXPECTED_PATHS:
        path = source / relative
        if relative != "db_split_merge" or not path.is_file():
            raise RuntimeError(f"unexpected source-tree path: {relative}")
        data = path.read_bytes()
        if data[:4] != b"\x7fELF":
            raise RuntimeError("untracked db_split_merge is not the recorded ELF helper")
        artifact[relative] = {
            "bytes": len(data),
            "sha256": shared.sha256_bytes(data),
            "kind": "generated ELF build helper",
        }
    return {
        "tracked_paths": sorted(path for path in status if path in EXPECTED_PATHS),
        "generated_build_artifacts": artifact,
    }


def stage_controls(source: Path, artifacts: Path, logs: Path, env: dict[str, str],
                   mode: str, stage: str, cases: list[dict], binaries: list[str],
                   receipt: dict) -> dict:
    build = build_targets(source, logs, env, mode, stage, binaries, receipt)
    before = source_state(source)
    expected_paths = sorted(EXPECTED_PATHS) if stage == "candidate" else []
    if before["tracked_paths"] != expected_paths:
        raise RuntimeError(f"unexpected tracked files after {stage} build")
    results, index = [], 0
    for case in cases:
        for repeat in range(1, case.get("repetitions", 1) + 1):
            index += 1
            result = run_case(
                source, artifacts, logs, env, mode, stage, case, index, repeat, receipt
            )
            if result["outcome"] in {"timeout", "test_not_started_or_harness_error"}:
                raise RuntimeError(
                    f"{stage} GTest did not complete {case['gtest_filter']}: {result['outcome']}"
                )
            results.append(result)
            receipt.setdefault("observed_runs", []).append(result)
            write_json(artifacts / "failure-controls-receipt.json", receipt)
    after = source_state(source)
    if after != before:
        raise RuntimeError(f"source or generated build artifacts changed during {stage} cases")
    return {"build": build, "source_artifacts": after, "results": results}


def run_comparison(source: Path, artifacts: Path, mode: str, manifest: dict,
                   cases: list[dict], receipt: dict) -> dict:
    env, _ = full.environment(source, artifacts, mode)
    env["V"] = "1"
    logs = artifacts / "logs" / mode
    logs.mkdir(parents=True, exist_ok=True)
    binaries = sorted({case["binary"] for case in cases})
    baseline = stage_controls(
        source, artifacts, logs, env, mode, "baseline", cases, binaries, receipt
    )
    baseline_source = baseline["source_artifacts"]
    if baseline_source["tracked_paths"]:
        raise RuntimeError("base diagnostic changed pinned tracked source")
    receipt["candidate_source"] = apply_candidate(
        source, manifest, baseline_source["generated_build_artifacts"]
    )
    candidate = stage_controls(
        source, artifacts, logs, env, mode, "candidate", cases, binaries, receipt
    )
    by_case = {
        (r["binary"], r["gtest_filter"], r["repetition"]): r
        for r in baseline["results"]
    }
    comparisons = []
    for result in candidate["results"]:
        key = (result["binary"], result["gtest_filter"], result["repetition"])
        original = by_case[key]
        comparisons.append({
            "binary": key[0],
            "gtest_filter": key[1],
            "repetition": key[2],
            "source_full_run_observation": result["source_full_run_observations"],
            "baseline_outcome": original["outcome"],
            "candidate_outcome": result["outcome"],
            "baseline_exit_code": original["exit_code"],
            "candidate_exit_code": result["exit_code"],
            "baseline_failed_marker": original["failed_marker"],
            "candidate_failed_marker": result["failed_marker"],
            "baseline_signatures": original["stdout_tail"] + original["stderr_tail"],
            "candidate_signatures": result["stdout_tail"] + result["stderr_tail"],
        })
    return {
        "mode": mode,
        "source_pin": SOURCE_PIN,
        "candidate_patch_sha256": PATCH_SHA256,
        "baseline_build": baseline["build"],
        "candidate_build": candidate["build"],
        "baseline_case_results": baseline["results"],
        "candidate_case_results": candidate["results"],
        "case_comparisons": comparisons,
        "diagnostic_status": "controls_completed; outcomes are recorded without causal classification",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--mode", choices=("normal", "status-checked"), required=True)
    parser.add_argument("--candidate-patch", required=True, type=Path)
    parser.add_argument("--candidate-manifest", required=True, type=Path)
    args = parser.parse_args()
    source, artifacts = args.source.resolve(), args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": 1,
        "phase": "same-runner-pinned-base-and-candidate-failure-controls",
        "issue": "https://github.com/meta/rocksdb/issues/15439",
        "mode": args.mode,
        "source_pin": SOURCE_PIN,
        "candidate_patch_sha256": PATCH_SHA256,
        "control_case_manifest": CASES_FILE.name,
        "source_full_run": {
            "normal": "https://github.com/xgbtxy/contribution-checks/actions/runs/41015193092",
            "status-checked": "https://github.com/xgbtxy/contribution-checks/actions/runs/41015199917",
        },
        "workflow": {
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "ref": os.environ.get("GITHUB_REF"),
            "helper_sha": os.environ.get("GITHUB_SHA"),
        },
        "started_utc": shared.utc(),
    }
    receipt_path = artifacts / "failure-controls-receipt.json"
    write_json(receipt_path, receipt)
    try:
        if args.candidate_patch.resolve() != PATCH.resolve():
            raise RuntimeError("candidate patch path is not the frozen helper input")
        if args.candidate_manifest.resolve() != MANIFEST.resolve():
            raise RuntimeError("candidate manifest path is not the frozen helper input")
        manifest, cases = load_cases(args.mode)
        pristine = shared.verify_source_pin(source)
        if pristine["head"] != SOURCE_PIN or pristine["status_paths"]:
            raise RuntimeError("diagnostic control requires a clean pinned source checkout")
        manifest = shared.verify_candidate_manifest(
            source,
            args.candidate_manifest.resolve(),
            ROOT / "patches/rocksdb-prefix-15439/TESTS_ONLY.patch",
            args.candidate_patch.resolve(),
            shared.EXPECTED_CANDIDATE_PATHS,
        )
        receipt["source_before"] = pristine
        receipt["frozen_candidate_manifest"] = {
            "source_base": manifest["source_base"],
            "candidate_patch_sha256": manifest["candidate_patch_sha256"],
            "tests_only_patch_sha256": manifest["tests_only_patch_sha256"],
            "candidate_paths": sorted(manifest["files"]),
        }
        receipt["runner"] = full.fingerprint(source, artifacts)
        receipt["case_manifest_sha256"] = shared.sha256_file(CASES_FILE)
        receipt["binary_targets"] = sorted({case["binary"] for case in cases})
        receipt["case_filters"] = [
            {key: case[key] for key in (
                "binary", "gtest_filter", "repetitions",
                "confidence", "expected_assertion", "reported_failure_markers",
            ) if key in case}
            for case in cases
        ]
        receipt["make_config"] = shared.make_config_facts(source)
        write_json(receipt_path, receipt)
        receipt["controls"] = run_comparison(
            source, artifacts, args.mode, manifest, cases, receipt
        )
        receipt["status"] = "diagnostic_controls_complete"
    except Exception as error:
        receipt["status"] = "control_runner_or_build_failed"
        receipt["error"] = f"{type(error).__name__}: {error}"
        try:
            receipt["source_after_failure"] = {
                "head": full.git(source, "rev-parse", "HEAD"),
                "paths": sorted(full.source_status(source)),
            }
        except Exception as snapshot_error:
            receipt["snapshot_error"] = repr(snapshot_error)
    finally:
        receipt["finished_utc"] = shared.utc()
        write_json(receipt_path, receipt)
    print(json.dumps({
        "status": receipt["status"],
        "mode": args.mode,
        "case_count": len(receipt.get("controls", {}).get("case_comparisons", [])),
        "error": receipt.get("error"),
    }, sort_keys=True), flush=True)
    return 0 if receipt["status"] == "diagnostic_controls_complete" else 1


if __name__ == "__main__":
    raise SystemExit(main())
