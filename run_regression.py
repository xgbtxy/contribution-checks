#!/usr/bin/env python3
"""Run the focused DataFusion #26691 test patch red, then production patch green."""
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import run_baseline as base

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "datafusion"
ARTIFACTS = ROOT / "artifacts"
PIN = base.PIN
TEST_PATH = "datafusion/sqllogictest/test_files/aggregates_topk.slt"
PROD_PATH = "datafusion/physical-optimizer/src/topk_aggregation.rs"
PATCHES = {
    "tests": ROOT / "patches/datafusion-26691-tests.patch",
    "production": ROOT / "patches/datafusion-26691-production.patch",
}
ALLOWED = {TEST_PATH, PROD_PATH}


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def status_paths(status):
    return {line[3:] for line in status.splitlines() if line}


def state():
    files = {}
    for name in sorted(ALLOWED):
        path = SOURCE / name
        files[name] = {"sha256": sha(path), "bytes": path.stat().st_size}
    return {"head": base.git(SOURCE, "rev-parse", "HEAD"),
            "status": base.git(SOURCE, "status", "--porcelain=v1"),
            "changed_files": base.git(SOURCE, "diff", "--name-only").splitlines(),
            "submodules": base.git(SOURCE, "submodule", "status", "--recursive").splitlines(),
            "files": files}


def write_diff(name, *paths):
    args = ["git", "diff", "--no-ext-diff", "--binary", "HEAD"]
    if paths:
        args.extend(["--", *paths])
    result = base.run(args, SOURCE, ARTIFACTS / "logs" / f"{name}-diff.log", timeout=60)
    if result["exit_code"] != 0:
        raise RuntimeError(f"git diff failed for {name}")
    path = ARTIFACTS / "diffs" / f"{name}.patch"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result["stdout"], encoding="utf-8")
    return {"path": str(path.relative_to(ROOT)), "sha256": sha(path), "bytes": path.stat().st_size}


def apply_patch(name):
    patch = PATCHES[name]
    if not patch.is_file():
        raise FileNotFoundError(f"required patch is missing: {patch}")
    patch_info = {"path": str(patch.relative_to(ROOT)), "sha256": sha(patch), "bytes": patch.stat().st_size}
    manifest = json.loads((ROOT / "patches/datafusion-26691.json").read_text(encoding="utf-8"))
    if manifest["base"] != PIN or patch_info["sha256"] != manifest["files"][name]["patch_sha256"]:
        raise RuntimeError(f"patch manifest mismatch for {name}")
    for suffix, args in (("check", ["git", "apply", "--check", str(patch)]), ("apply", ["git", "apply", str(patch)])):
        result = base.run(args, SOURCE, ARTIFACTS / "logs" / f"apply-{name}-{suffix}.log", timeout=60)
        if result["exit_code"] != 0:
            raise RuntimeError(f"git apply {suffix} failed for {name}")
    entry = manifest["files"][name]
    if base.git(SOURCE, "hash-object", entry["path"]) != entry["candidate_blob"]:
        raise RuntimeError(f"applied source blob mismatch for {name}")
    return patch_info


def build(stage, receipt):
    command = ["cargo", "+1.99.0", "test", "--locked", "--profile=ci", "-p", "datafusion-sqllogictest", "--test", "sqllogictests", "--no-run", "--message-format=json-render-diagnostics"]
    env = os.environ.copy()
    env["CARGO_BUILD_JOBS"] = "4"
    result = base.run(command, SOURCE, ARTIFACTS / "logs" / stage / "cargo-build.log", timeout=40 * 60, env=env)
    binaries, finished = [], []
    for line in result["stdout"].splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("reason") == "build-finished":
            finished.append(event.get("success"))
        if event.get("reason") == "compiler-artifact":
            target = event.get("target", {})
            if target.get("name") == "sqllogictests" and "test" in target.get("kind", []):
                binaries.append(event.get("executable"))
    build_info = {"command": command, "exit_code": result["exit_code"], "timed_out": result["timed_out"],
                  "started_utc": result["started_utc"], "finished_utc": result["finished_utc"],
                  "build_finished": finished, "test_binary_candidates": binaries,
                  "test_cases_executed_by_no_run": 0,
                  "logs": {"stdout_jsonl": result["stdout_log"], "stderr": result["stderr_log"], "metadata": result["meta_log"]}}
    receipt.setdefault("builds", {})[stage] = build_info
    if result["exit_code"] != 0 or result["timed_out"] or finished != [True] or len(binaries) != 1 or not binaries[0]:
        raise RuntimeError(f"{stage} build failed: exit={result['exit_code']}, build_finished={finished}, binaries={binaries}")
    binary = Path(binaries[0]).resolve()
    if not binary.is_file():
        raise FileNotFoundError(f"Cargo reported missing test binary: {binary}")
    build_info["test_binary"] = {"path": str(binary), "sha256": sha(binary), "bytes": binary.stat().st_size}
    base.dump(ARTIFACTS / "receipt.json", receipt)
    return binary


def run_suite(stage, binary):
    command = [binary, "--test-threads", "1", "aggregates_topk.slt"]
    env = os.environ.copy()
    env["RUST_BACKTRACE"] = "1"
    result = base.run(command, SOURCE / "datafusion/sqllogictest", ARTIFACTS / "logs" / f"{stage}-suite.log", timeout=15 * 60, env=env)
    output = result["stdout"] + result["stderr"]
    mismatch_sql = re.findall(
        r"query result mismatch:\n\[SQL\] (.*?)\n\[Diff\] \(-expected\|\+actual\)",
        output.replace("\r\n", "\n"), re.DOTALL,
    )
    summary = {"command": [str(x) for x in command], "started_utc": result["started_utc"], "finished_utc": result["finished_utc"],
               "exit_code": result["exit_code"], "timed_out": result["timed_out"],
               "completed_test_files": base.finished_files(output),
               "query_result_mismatch_markers": output.count("query result mismatch:"),
               "mismatched_queries": [base.normalize(sql) for sql in mismatch_sql],
               "file_error_counts": [int(n) for n in re.findall(r"\b(\d+) errors in file\b", output)],
               "logs": {"stdout": result["stdout_log"], "stderr": result["stderr_log"], "metadata": result["meta_log"]}}
    return summary, output


def main():
    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "logs").mkdir(exist_ok=True)
    receipt = {"status": "running", "started_utc": base.utc(), "source_pin": PIN,
               "test_case_run": "aggregates_topk.slt", "junit_created": False, "diffs": {}}
    base.dump(ARTIFACTS / "receipt.json", receipt)
    try:
        before = state()
        receipt["source_before"] = before
        receipt["diffs"]["source_before"] = write_diff("source-before")
        if before["head"] != PIN or before["status"] or before["changed_files"] or any(not line or line[0] != " " for line in before["submodules"]):
            raise RuntimeError("source checkout is not clean at the pinned SHA with initialized submodules")

        rustc = base.run(["rustc", "+1.99.0", "--version", "--verbose"], SOURCE, ARTIFACTS / "logs/rustc.log", timeout=30)
        cargo = base.run(["cargo", "+1.99.0", "--version"], SOURCE, ARTIFACTS / "logs/cargo.log", timeout=30)
        protoc = base.run(["protoc", "--version"], SOURCE, ARTIFACTS / "logs/protoc.log", timeout=30)
        receipt["toolchain"] = {"rustc": rustc["stdout"], "cargo": cargo["stdout"], "protoc": protoc["stdout"]}
        protoc_version = re.search(r"libprotoc (\d+)\.(\d+)", protoc["stdout"])
        protoc_ok = protoc_version and tuple(map(int, protoc_version.groups())) >= (3, 15)
        if rustc["exit_code"] != 0 or not rustc["stdout"].startswith("rustc 1.99.0 ") or cargo["exit_code"] != 0 or not cargo["stdout"].startswith("cargo 1.99.0 ") or protoc["exit_code"] != 0 or not protoc_ok:
            raise RuntimeError("required pinned Rust/Cargo/protoc toolchain unavailable")

        receipt["test_patch"] = apply_patch("tests")
        receipt["test_stage"] = state()
        receipt["diffs"]["tests_stage"] = write_diff("tests-stage", TEST_PATH)
        if receipt["test_stage"]["head"] != PIN or set(receipt["test_stage"]["changed_files"]) != {TEST_PATH} or status_paths(receipt["test_stage"]["status"]) != {TEST_PATH}:
            raise RuntimeError(f"test patch changed unexpected files: {receipt['test_stage']}")
        red_binary = build("red", receipt)
        red, _ = run_suite("red", red_binary)
        receipt["red_run"] = red
        expected_failures = {
            base.normalize('SELECT g, -"max(topk_projection.v)" AS "max(topk_projection.v)" FROM (SELECT g, max(v) FROM topk_projection GROUP BY g) ORDER BY 2 DESC LIMIT 2;'),
            base.normalize('SELECT -v AS v FROM (SELECT DISTINCT v FROM topk_projection) ORDER BY v LIMIT 2;'),
        }
        if red["exit_code"] != 1 or red["timed_out"] or red["completed_test_files"] != [1] or red["query_result_mismatch_markers"] != 2 or red["file_error_counts"] != [2] or set(red["mismatched_queries"]) != expected_failures:
            raise RuntimeError(f"original source did not produce exactly two query-result mismatches: {red}")
        base.dump(ARTIFACTS / "receipt.json", receipt)

        receipt["production_patch"] = apply_patch("production")
        receipt["diffs"]["production_patch_stage"] = write_diff("production-patch-stage", PROD_PATH)
        fmt = base.run(["cargo", "+1.99.0", "fmt", "--all"], SOURCE, ARTIFACTS / "logs/fmt.log", timeout=10 * 60)
        receipt["fmt"] = {"exit_code": fmt["exit_code"], "timed_out": fmt["timed_out"], "logs": {"stdout": fmt["stdout_log"], "stderr": fmt["stderr_log"], "metadata": fmt["meta_log"]}}
        if fmt["exit_code"] != 0 or fmt["timed_out"]:
            raise RuntimeError("cargo fmt --all failed")
        final_state = state()
        receipt["source_after_fmt"] = final_state
        receipt["diffs"]["after_fmt_full"] = write_diff("after-fmt-full")
        receipt["diffs"]["final_allowed_files"] = write_diff("final-allowed-files", TEST_PATH, PROD_PATH)
        if final_state["head"] != PIN or set(final_state["changed_files"]) != ALLOWED or status_paths(final_state["status"]) != ALLOWED:
            raise RuntimeError(f"formatting changed unexpected files: {final_state['changed_files']}")
        check = base.run(["git", "diff", "--check"], SOURCE, ARTIFACTS / "logs/diff-check.log", timeout=60)
        if check["exit_code"] != 0:
            raise RuntimeError("git diff --check failed")
        green_binary = build("green", receipt)
        green, _ = run_suite("green", green_binary)
        receipt["green_run"] = green
        if green["exit_code"] != 0 or green["timed_out"] or green["completed_test_files"] != [1] or green["query_result_mismatch_markers"] != 0 or green["file_error_counts"]:
            raise RuntimeError(f"candidate did not pass aggregates_topk.slt cleanly: {green}")
        after = state()
        receipt["source_after_green"] = after
        if after["head"] != PIN or set(after["changed_files"]) != ALLOWED or status_paths(after["status"]) != ALLOWED or after["files"] != final_state["files"] or after["submodules"] != final_state["submodules"]:
            raise RuntimeError("source HEAD or modified file set changed unexpectedly")
        receipt.update({"finished_utc": base.utc(), "status": "focused_red_green_pass"})
        base.dump(ARTIFACTS / "receipt.json", receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        receipt.update({"finished_utc": base.utc(), "status": "failed", "error": f"{type(exc).__name__}: {exc}"})
        base.dump(ARTIFACTS / "receipt.json", receipt)
        print(receipt["error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
