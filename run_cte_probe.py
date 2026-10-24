#!/usr/bin/env python3
"""Observe one materialized-CTE TopK plan/result on pinned main and the candidate."""
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import run_baseline as base

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / "datafusion"
ART = ROOT / "artifacts"
PIN = base.PIN
PROD = "datafusion/physical-optimizer/src/topk_aggregation.rs"
PATCH = ROOT / "patches/datafusion-26691-production.patch"
MANIFEST = ROOT / "patches/datafusion-26691.json"
SQL = "SELECT q.g, q.score FROM (WITH c AS MATERIALIZED (SELECT MAX(v) AS score, g FROM t GROUP BY g) SELECT g, score FROM c) q ORDER BY q.g DESC NULLS LAST LIMIT 2;"
# DataFusion's configured default is Dialect::Generic; restore it after this probe.
RESET = "statement ok\nSET datafusion.sql_parser.dialect = 'Generic';"
EXPECTED = [["5", "50"], ["4", "40"]]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def status_paths(status):
    return {line[3:] for line in status.splitlines() if line}


def source_state():
    return {"head": base.git(SOURCE, "rev-parse", "HEAD"),
            "status": base.git(SOURCE, "status", "--porcelain=v1"),
            "changed_files": base.git(SOURCE, "diff", "--name-only").splitlines(),
            "submodules": base.git(SOURCE, "submodule", "status", "--recursive").splitlines(),
            "production_blob": base.git(SOURCE, "hash-object", str(SOURCE / PROD)),
            "production_sha256": sha(SOURCE / PROD)}


def run(command, cwd, name, timeout=300, env=None):
    return base.run(command, cwd, ART / "logs" / name, timeout=timeout, env=env)


def build(stage, receipt):
    command = ["cargo", "+1.99.0", "test", "--locked", "--profile=ci", "-p", "datafusion-sqllogictest", "--test", "sqllogictests", "--no-run", "--message-format=json-render-diagnostics"]
    env = os.environ.copy()
    env["CARGO_BUILD_JOBS"] = "4"
    res = run(command, SOURCE, f"{stage}-build/cargo-build.log", timeout=40 * 60, env=env)
    binaries, finished = [], []
    for line in res["stdout"].splitlines():
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("reason") == "build-finished":
            finished.append(msg.get("success"))
        if msg.get("reason") == "compiler-artifact" and msg.get("target", {}).get("name") == "sqllogictests" and "test" in msg.get("target", {}).get("kind", []):
            binaries.append(msg.get("executable"))
    info = {"exit_code": res["exit_code"], "timed_out": res["timed_out"], "build_finished": finished, "binary_candidates": binaries, "no_run_test_cases": 0, "logs": {"stdout_jsonl": res["stdout_log"], "stderr": res["stderr_log"], "metadata": res["meta_log"]}}
    receipt.setdefault("builds", {})[stage] = info
    if res["exit_code"] != 0 or res["timed_out"] or finished != [True] or len(binaries) != 1 or not binaries[0]:
        raise RuntimeError(f"{stage} build failed: {info}")
    binary = Path(binaries[0]).resolve()
    if not binary.is_file():
        raise FileNotFoundError(f"SQLLogicTest runner missing: {binary}")
    info["binary"] = {"path": str(binary), "sha256": sha(binary), "bytes": binary.stat().st_size}
    base.dump(ART / "receipt.json", receipt)
    return binary


def rows_for(path, sql):
    matches = [r for r in base.records(path) if base.normalize(r["sql"]) == base.normalize(sql)]
    return [line.split() for line in matches[0]["answers"]] if len(matches) == 1 else [], len(matches)


def phase(stage, binary, receipt):
    oracle = (
        "# Five non-null CTAS rows; sort by the materialized CTE continuation output.\n"
        "statement ok\nset datafusion.sql_parser.dialect = 'PostgreSQL';\n\n"
        "statement ok\nset datafusion.explain.logical_plan_only = false;\n\n"
        "statement ok\nset datafusion.optimizer.enable_topk_aggregation = true;\n\n"
        "statement ok\nCREATE TABLE t AS SELECT column1 AS g, column2 AS v FROM (VALUES (1,100),(2,20),(3,30),(4,40),(5,50));\n\n"
        f"query II\n{SQL}\n----\n5 50\n4 40\n\n{RESET}\n"
    )
    phase_dir = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "datafusion-cte-probe" / stage
    for name in ("oracle", "complete"):
        (phase_dir / name / "test_files").mkdir(parents=True, exist_ok=True)
    oracle_path = phase_dir / "oracle/test_files/cte_probe.slt"
    complete_path = phase_dir / "complete/test_files/cte_probe.slt"
    oracle_path.write_text(oracle, encoding="utf-8")
    prefix, suffix = oracle.rsplit(RESET, 1)
    complete_path.write_text(prefix + f"query TT\nEXPLAIN {SQL}\n----\nPENDING_PLAN\n\n{RESET}" + suffix, encoding="utf-8")

    command = lambda complete: [binary, *( ["--complete"] if complete else []), "--test-threads", "1", "cte_probe.slt"]
    env = os.environ.copy()
    env["RUST_BACKTRACE"] = "1"
    c = run(command(True), phase_dir / "complete", f"{stage}-complete.log", timeout=300, env=env)
    o = run(command(False), phase_dir / "oracle", f"{stage}-oracle.log", timeout=300, env=env)
    ctext, otext = c["stdout"] + c["stderr"], o["stdout"] + o["stderr"]
    (ART / "completed").mkdir(parents=True, exist_ok=True)
    (ART / "completed" / f"{stage}.slt").write_bytes(complete_path.read_bytes())
    actual, query_count = rows_for(complete_path, SQL)
    explain_records = [r for r in base.records(complete_path) if base.normalize(r["sql"]) == base.normalize("EXPLAIN " + SQL)]
    explain_count = len(explain_records)
    explains = explain_records[0]["answers"] if explain_count == 1 else []
    marker = next((i for i, line in enumerate(explains) if line.strip() == "physical_plan"), None)
    full_plan = "\n".join(explains)
    physical = explains[marker + 1:] if marker is not None else []
    (ART / "plans").mkdir(parents=True, exist_ok=True)
    (ART / "plans" / f"{stage}.txt").write_text(full_plan + "\n", encoding="utf-8")
    mismatch_count = otext.count("query result mismatch:")
    file_errors = [int(x) for x in re.findall(r"\b(\d+) errors in file\b", otext)]
    actual_matches = actual == EXPECTED
    oracle_consistent = (actual_matches and o["exit_code"] == 0 and mismatch_count == 0 and not file_errors) or (not actual_matches and o["exit_code"] == 1 and mismatch_count == 1 and file_errors == [1])
    info = {"complete_exit_code": c["exit_code"], "complete_timed_out": c["timed_out"], "complete_file_count": base.finished_files(ctext), "oracle_exit_code": o["exit_code"], "oracle_timed_out": o["timed_out"], "oracle_file_count": base.finished_files(otext), "oracle_query_result_mismatch_count": mismatch_count, "oracle_file_error_counts": file_errors, "expected_rows": EXPECTED, "actual_rows": actual, "parsed_query_record_count": query_count, "parsed_explain_record_count": explain_count, "materialized_cte_nodes": [x for x in physical if "MaterializedCteExec" in x], "sort_nodes": [x for x in physical if "Sort" in x and "Exec" in x], "projection_nodes": [x for x in physical if "ProjectionExec" in x], "plan_file": f"artifacts/plans/{stage}.txt", "complete_slt_file": f"artifacts/completed/{stage}.slt", "complete_logs": {"stdout": c["stdout_log"], "stderr": c["stderr_log"], "metadata": c["meta_log"]}, "oracle_logs": {"stdout": o["stdout_log"], "stderr": o["stderr_log"], "metadata": o["meta_log"]}, "result_classification": "correct" if actual_matches else "result_mismatch"}
    if c["exit_code"] != 0 or c["timed_out"] or base.finished_files(ctext) != [1] or o["timed_out"] or base.finished_files(otext) != [1] or query_count != 1 or explain_count != 1 or marker is None or not info["materialized_cte_nodes"] or not info["sort_nodes"] or not info["projection_nodes"] or not oracle_consistent:
        raise RuntimeError(f"{stage} probe did not complete/classify consistently: {info}")
    receipt.setdefault("phases", {})[stage] = info
    base.dump(ART / "receipt.json", receipt)
    return info


def main():
    (ART / "logs").mkdir(parents=True, exist_ok=True)
    receipt = {"status": "running", "started_utc": base.utc(), "source_pin": PIN, "probe_sql": SQL, "expected_rows": EXPECTED, "junit_created": False}
    base.dump(ART / "receipt.json", receipt)
    try:
        before = source_state()
        receipt["source_before"] = before
        if before["head"] != PIN or before["status"] or before["changed_files"] or any(not line or line[0] != " " for line in before["submodules"]):
            raise RuntimeError(f"pinned source not clean/initialized: {before}")
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8-sig"))
        if manifest["base"] != PIN or manifest["files"]["production"]["path"] != PROD or sha(PATCH) != manifest["files"]["production"]["patch_sha256"]:
            raise RuntimeError("production patch does not match local manifest")
        if base.git(SOURCE, "rev-parse", f"HEAD:{PROD}") != manifest["files"]["production"]["head_blob"]:
            raise RuntimeError("production base blob differs from manifest")
        receipt["manifest"] = {"patch_sha256": sha(PATCH), "candidate_blob": manifest["files"]["production"]["candidate_blob"]}
        for name, command in (("rustc", ["rustc", "+1.99.0", "--version", "--verbose"]), ("cargo", ["cargo", "+1.99.0", "--version"]), ("protoc", ["protoc", "--version"])):
            res = run(command, SOURCE, f"tool-{name}.log", timeout=30)
            receipt.setdefault("toolchain", {})[name] = {"version": res["stdout"], "exit_code": res["exit_code"], "logs": {"stdout": res["stdout_log"], "stderr": res["stderr_log"], "metadata": res["meta_log"]}}
        if not receipt["toolchain"]["rustc"]["version"].startswith("rustc 1.99.0 ") or not receipt["toolchain"]["cargo"]["version"].startswith("cargo 1.99.0 ") or receipt["toolchain"]["protoc"]["exit_code"] != 0:
            raise RuntimeError("pinned Rust/Cargo/protoc unavailable")

        base.dump(ART / "receipt.json", receipt)
        baseline_binary = build("baseline", receipt)
        receipt["phases"] = {"baseline": phase("baseline", baseline_binary, receipt)}
        base.dump(ART / "receipt.json", receipt)

        check = run(["git", "apply", "--check", str(PATCH)], SOURCE, "apply-production-check.log", timeout=60)
        apply = run(["git", "apply", str(PATCH)], SOURCE, "apply-production.log", timeout=60)
        if check["exit_code"] != 0 or apply["exit_code"] != 0:
            raise RuntimeError("production patch failed to apply")
        if base.git(SOURCE, "hash-object", str(SOURCE / PROD)) != manifest["files"]["production"]["candidate_blob"]:
            raise RuntimeError("applied production blob differs from manifest candidate")
        fmt = run(["cargo", "+1.99.0", "fmt", "--all"], SOURCE, "cargo-fmt.log", timeout=10 * 60)
        receipt["cargo_fmt"] = {"exit_code": fmt["exit_code"], "logs": {"stdout": fmt["stdout_log"], "stderr": fmt["stderr_log"], "metadata": fmt["meta_log"]}}
        if fmt["exit_code"] != 0 or base.git(SOURCE, "diff", "--name-only").splitlines() != [PROD] or base.git(SOURCE, "hash-object", str(SOURCE / PROD)) != manifest["files"]["production"]["candidate_blob"]:
            raise RuntimeError("cargo fmt changed unexpected source or production blob no longer matches manifest")
        receipt["source_after_patch_fmt"] = source_state()
        if receipt["source_after_patch_fmt"]["status"] and status_paths(receipt["source_after_patch_fmt"]["status"]) != {PROD}:
            raise RuntimeError(f"unexpected status after production patch/format: {receipt['source_after_patch_fmt']['status']}")
        if receipt["source_after_patch_fmt"]["changed_files"] != [PROD]:
            raise RuntimeError(f"production patch changed unexpected files: {receipt['source_after_patch_fmt']['changed_files']}")
        diff_check = run(["git", "diff", "--check"], SOURCE, "git-diff-check.log", timeout=60)
        receipt["git_diff_check"] = {"exit_code": diff_check["exit_code"], "logs": {"stdout": diff_check["stdout_log"], "stderr": diff_check["stderr_log"], "metadata": diff_check["meta_log"]}}
        if diff_check["exit_code"] != 0:
            raise RuntimeError("git diff --check failed")
        green_binary = build("candidate", receipt)
        receipt["phases"]["candidate"] = phase("candidate", green_binary, receipt)
        receipt["phase_comparison"] = {"baseline_rows": receipt["phases"]["baseline"]["actual_rows"], "candidate_rows": receipt["phases"]["candidate"]["actual_rows"], "changed_behavior": receipt["phases"]["baseline"]["actual_rows"] != receipt["phases"]["candidate"]["actual_rows"], "baseline_correct": receipt["phases"]["baseline"]["actual_rows"] == EXPECTED, "candidate_correct": receipt["phases"]["candidate"]["actual_rows"] == EXPECTED}
        after = source_state()
        receipt["source_after_candidate"] = after
        if after["head"] != PIN or after["changed_files"] != [PROD] or status_paths(after["status"]) != {PROD} or after["submodules"] != receipt["source_before"]["submodules"]:
            raise RuntimeError(f"unexpected source mutation during probe: {after}")
        receipt.update({"finished_utc": base.utc(), "status": "probe_observed"})
        base.dump(ART / "receipt.json", receipt)
        print(json.dumps(receipt, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        receipt.update({"finished_utc": base.utc(), "status": "probe_failed", "error": f"{type(exc).__name__}: {exc}"})
        base.dump(ART / "receipt.json", receipt)
        print(receipt["error"], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
