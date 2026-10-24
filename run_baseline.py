#!/usr/bin/env python3
"""Run four pinned DataFusion #26691 SQLLogicTest cases and observe physical plans."""
import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

PIN = "039c0b99d5b05e73ff5def029c0131b76b481c6f"
ROOT = Path(__file__).resolve().parent
ORACLES = ROOT / "oracles"
QUERIES = {
    "issue-on": 'SELECT g, -"max(t.v)" AS "max(t.v)" FROM (SELECT g, max(v) FROM t GROUP BY g) ORDER BY 2 DESC LIMIT 2;',
    "issue-off": 'SELECT g, -"max(t.v)" AS "max(t.v)" FROM (SELECT g, max(v) FROM t GROUP BY g) ORDER BY 2 DESC LIMIT 2;',
    "positive": "SELECT score AS rank, g AS label, -score AS decoration FROM (SELECT g, MAX(v) AS score FROM t GROUP BY g) ORDER BY 1 DESC LIMIT 2;",
    "distinct-probe": "SELECT -v AS v FROM (SELECT DISTINCT v FROM t) ORDER BY v LIMIT 2;",
}
ORACLE_ROWS = {
    "issue-on": [["a", "-2"], ["d", "-3"]],
    "issue-off": [["a", "-2"], ["d", "-3"]],
    "positive": [["10", "b", "-10"], ["9", "c", "-9"]],
    "distinct-probe": [["-10"], ["-9"]],
}
BUGGY_ROWS = [["c", "-9"], ["b", "-10"]]


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def dump(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run(command, cwd, log, timeout=300, env=None):
    command = [str(part) for part in command]
    start = utc()
    try:
        p = subprocess.run(command, cwd=cwd, env=env, capture_output=True, timeout=timeout, check=False)
        code, out_bytes, err_bytes, timed_out = p.returncode, p.stdout, p.stderr, False
    except subprocess.TimeoutExpired as e:
        code, timed_out = None, True
        out_bytes = e.stdout if isinstance(e.stdout, bytes) else (e.stdout or "").encode("utf-8")
        err_bytes = e.stderr if isinstance(e.stderr, bytes) else (e.stderr or "").encode("utf-8")
    log.parent.mkdir(parents=True, exist_ok=True)
    stem = log.with_suffix("")
    stdout_path = stem.with_suffix(".stdout.jsonl" if log.stem == "cargo-build" else ".stdout.log")
    stderr_path = stem.with_suffix(".stderr.log")
    meta_path = stem.with_suffix(".meta.json")
    stdout_path.write_bytes(out_bytes)
    stderr_path.write_bytes(err_bytes)
    finished = utc()
    meta = {"started_utc": start, "finished_utc": finished, "command": command, "exit_code": code, "timed_out": timed_out,
            "stdout_bytes": len(out_bytes), "stderr_bytes": len(err_bytes),
            "stdout_log": str(stdout_path.relative_to(ROOT)) if stdout_path.is_relative_to(ROOT) else str(stdout_path),
            "stderr_log": str(stderr_path.relative_to(ROOT)) if stderr_path.is_relative_to(ROOT) else str(stderr_path),
            "meta_log": str(meta_path.relative_to(ROOT)) if meta_path.is_relative_to(ROOT) else str(meta_path)}
    dump(meta_path, meta)
    return {**meta, "stdout": out_bytes.decode("utf-8", errors="replace"), "stderr": err_bytes.decode("utf-8", errors="replace")}


def git(source, *args):
    output = subprocess.run(["git", "-C", str(source), *args], text=True, encoding="utf-8", errors="replace", capture_output=True, check=True).stdout
    return output.rstrip("\r\n")


def normalize(sql):
    return re.sub(r"\s+", " ", sql).strip().rstrip(";").casefold()


def records(path):
    lines, found, i = path.read_text(encoding="utf-8-sig").splitlines(), [], 0
    while i < len(lines):
        if not lines[i].startswith("query "):
            i += 1
            continue
        header, sql = lines[i].strip(), []
        i += 1
        while i < len(lines) and lines[i].strip() != "----":
            sql.append(lines[i].strip())
            i += 1
        if i == len(lines):
            raise ValueError(f"{path.name}: query has no ---- separator")
        i += 1
        answers = []
        while i < len(lines) and lines[i].strip():
            answers.append(lines[i].strip())
            i += 1
        found.append({"header": header, "sql": " ".join(sql), "answers": answers})
    return found


def oracle_rows(case):
    path = ORACLES / f"{case}.slt"
    matches = [r for r in records(path) if normalize(r["sql"]) == normalize(QUERIES[case])]
    rows = [line.split() for line in matches[0]["answers"]] if len(matches) == 1 else []
    if rows != ORACLE_ROWS[case]:
        raise ValueError(f"{case}: invalid handwritten oracle {rows!r}")
    return rows


def source_facts(source):
    tracked = ["datafusion/physical-optimizer/src/topk_aggregation.rs", "datafusion/sqllogictest/test_files/aggregates_topk.slt", "Cargo.lock", "rust-toolchain.toml"]
    hashes = {}
    for name in tracked:
        file = source / name
        hashes[name] = {"head_blob": git(source, "rev-parse", f"HEAD:{name}"), "worktree_blob": git(source, "hash-object", str(file)), "sha256": hashlib.sha256(file.read_bytes()).hexdigest()}
    return {"head": git(source, "rev-parse", "HEAD"), "status_porcelain": git(source, "status", "--porcelain=v1"), "diff_names": git(source, "diff", "--name-only").splitlines(), "submodules": git(source, "submodule", "status", "--recursive").splitlines(), "tracked_hashes": hashes}


def finished_files(text):
    counts = [int(n) for n in re.findall(r"Completed\s+(\d+)\s+test files", text)]
    # indicatif's final message is hidden without a TTY; the runner separately
    # reports terminal progress in CI. Both forms must agree when both appear.
    counts.extend(
        int(total)
        for done, total in re.findall(
            r"Progress:\s+(\d+)/(\d+)\s+files completed \(100%\)", text
        )
        if done == total
    )
    return sorted(set(counts))


def result_mismatch(text, sql):
    # sqllogictest 0.29.1 distinguishes QueryResultMismatch from SQL failures.
    text = re.sub(r"\x1b\[[0-9;]*m", "", text).replace("\r\n", "\n")
    if any(marker in text for marker in ("query failed:", "parse error:", "query columns mismatch:")):
        return False
    matches = re.findall(
        r"query result mismatch:\n\[SQL\] (.*?)\n\[Diff\] \(-expected\|\+actual\)",
        text,
        re.DOTALL,
    )
    return len(matches) == 1 and normalize(matches[0]) == normalize(sql)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "datafusion")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--self-check", action="store_true")
    args = parser.parse_args()
    if args.self_check:
        for case in QUERIES:
            text = (ORACLES / f"{case}.slt").read_text(encoding="utf-8-sig")
            oracle_rows(case)
            if any(row not in text for row in ["('a', 2)", "('b', 10)", "('c', 9)", "('d', 3)", "('e', 7)"]):
                raise ValueError(f"{case}: five-row setup is incomplete")
        issue_text = (ORACLES / "issue-on.slt").read_text(encoding="utf-8-sig")
        if "https://github.com/apache/datafusion/issues/26691" not in issue_text or "@jayzhan211" not in issue_text:
            raise ValueError("issue-on must credit the official issue reporter")
        print(json.dumps({"oracle_self_check": "passed", "cases": list(QUERIES)}, indent=2))
        return 0

    source, artifacts = args.source.resolve(), args.artifacts.resolve()
    (artifacts / "logs").mkdir(parents=True, exist_ok=True)
    (artifacts / "plans").mkdir(parents=True, exist_ok=True)
    (artifacts / "completed").mkdir(parents=True, exist_ok=True)
    receipt = {"status": "running", "started_utc": utc(), "pinned_sha": PIN, "source": str(source), "compile_test_cases_executed": 0}
    dump(artifacts / "receipt.json", receipt)
    errors = []
    try:
        facts = source_facts(source)
        receipt["source_facts_before"] = facts
        receipt["oracle_hashes_before"] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(ORACLES.glob("*.slt"))}
        if facts["head"] != PIN or facts["status_porcelain"] or facts["diff_names"] or any(not line or line[0] != " " for line in facts["submodules"]):
            raise ValueError("source HEAD/status/submodule pin validation failed")
        if any(v["head_blob"] != v["worktree_blob"] for v in facts["tracked_hashes"].values()):
            raise ValueError("baseline source hashes differ from pinned HEAD")

        tools = {}
        for name, cmd in (("rustc", ["rustc", "+1.99.0", "--version", "--verbose"]), ("cargo", ["cargo", "+1.99.0", "--version"]), ("protoc", ["protoc", "--version"])):
            r = run(cmd, source, artifacts / "logs" / f"{name}.log", timeout=30)
            tools[name] = {"exit_code": r["exit_code"], "version": (r["stdout"] + r["stderr"]).strip(), "logs": {"stdout": r["stdout_log"], "stderr": r["stderr_log"], "metadata": r["meta_log"]}}
            if r["exit_code"] != 0:
                errors.append(f"{name} version check failed")
        if not tools["rustc"]["version"].startswith("rustc 1.99.0 ") or not tools["cargo"]["version"].startswith("cargo 1.99.0 "):
            errors.append("expected exact Rust/Cargo 1.99.0")
        protoc = re.search(r"libprotoc (\d+)\.(\d+)", tools["protoc"]["version"])
        if not protoc or tuple(map(int, protoc.groups())) < (3, 15):
            errors.append("protoc must be version 3.15 or newer")
        receipt["toolchain"] = tools
        if errors:
            raise ValueError("; ".join(errors))

        build_cmd = ["cargo", "+1.99.0", "test", "--locked", "--profile=ci", "-p", "datafusion-sqllogictest", "--test", "sqllogictests", "--no-run", "--message-format=json-render-diagnostics"]
        build_env = os.environ.copy()
        build_env["CARGO_BUILD_JOBS"] = "4"
        build = run(build_cmd, source, artifacts / "logs" / "cargo-build.log", timeout=80 * 60, env=build_env)
        binaries, finished = [], []
        for line in build["stdout"].splitlines():
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
        receipt["cargo_build"] = {"command": build_cmd, "started_utc": build["started_utc"], "finished_utc": build["finished_utc"], "exit_code": build["exit_code"], "timed_out": build["timed_out"], "build_finished": finished, "test_binary_candidates": binaries, "test_cases_executed": 0, "logs": {"stdout_jsonl": build["stdout_log"], "stderr": build["stderr_log"], "metadata": build["meta_log"]}}
        binary = Path(binaries[0]).resolve() if len(binaries) == 1 and binaries[0] else None
        if binary and binary.is_file():
            with binary.open("rb") as stream:
                binary_sha256 = hashlib.file_digest(stream, "sha256").hexdigest()
            receipt["sql_logic_test_binary"] = {"path": str(binary), "sha256": binary_sha256, "size_bytes": binary.stat().st_size}
        if build["exit_code"] != 0 or build["timed_out"] or finished != [True] or not binary or not binary.is_file():
            raise ValueError(f"cargo --no-run failed validation: exit={build['exit_code']}, build_finished={finished}, binaries={binaries}")

        temp_root = Path(os.environ.get("RUNNER_TEMP", "/tmp")) / "datafusion-26691-baseline"
        case_results = {}
        for case, sql in QUERIES.items():
            expected = oracle_rows(case)
            original = (ORACLES / f"{case}.slt").read_text(encoding="utf-8-sig")
            complete_dir = temp_root / case / "complete"
            oracle_dir = temp_root / case / "oracle"
            (complete_dir / "test_files").mkdir(parents=True, exist_ok=True)
            (oracle_dir / "test_files").mkdir(parents=True, exist_ok=True)
            complete_file = complete_dir / "test_files" / f"{case}.slt"
            body, marker, cleanup = original.partition("# Restore the original configuration.")
            complete_file.write_text(body.rstrip() + f"\n\nquery TT\nEXPLAIN {sql}\n----\nPENDING_PLAN\n\n" + marker + cleanup, encoding="utf-8")
            (oracle_dir / "test_files" / f"{case}.slt").write_text(original, encoding="utf-8")

            complete = run([binary, "--complete", "--test-threads", "1", f"{case}.slt"], complete_dir, artifacts / "logs" / f"{case}-complete.log")
            complete_text = complete["stdout"] + complete["stderr"]
            completed_copy = artifacts / "completed" / f"{case}.slt"
            if complete_file.is_file():
                completed_copy.write_bytes(complete_file.read_bytes())
            oracle = run([binary, "--test-threads", "1", f"{case}.slt"], oracle_dir, artifacts / "logs" / f"{case}-oracle.log")
            oracle_text = oracle["stdout"] + oracle["stderr"]
            confirmed_mismatch = result_mismatch(oracle_text, sql)
            if complete["exit_code"] != 0 or complete["timed_out"] or finished_files(complete_text) != [1]:
                errors.append(f"{case}: completion mode did not successfully execute exactly one file")
            if finished_files(oracle_text) != [1]:
                errors.append(f"{case}: oracle mode did not execute exactly one file")

            generated = records(complete_file)
            data = [r for r in generated if normalize(r["sql"]) == normalize(sql)]
            explain = [r for r in generated if normalize(r["sql"]) == normalize("EXPLAIN " + sql)]
            actual = [line.split() for line in data[0]["answers"]] if len(data) == 1 else []
            physical = []
            if len(explain) == 1:
                plan_rows = explain[0]["answers"]
                marker = next((i for i, line in enumerate(plan_rows) if line.strip() == "physical_plan"), None)
                if marker is not None:
                    physical = plan_rows[marker + 1 :]
            (artifacts / "plans" / f"{case}.txt").write_text("\n".join(physical) + "\n", encoding="utf-8")
            limits = [line for line in physical if "AggregateExec:" in line and "lim=[2]" in line]
            observed = {"expected_rows": expected, "actual_rows": actual, "complete_started_utc": complete["started_utc"], "complete_finished_utc": complete["finished_utc"], "complete_exit_code": complete["exit_code"], "complete_timed_out": complete["timed_out"], "complete_files": finished_files(complete_text), "complete_logs": {"stdout": complete["stdout_log"], "stderr": complete["stderr_log"], "metadata": complete["meta_log"]}, "completed_slt": str(completed_copy.relative_to(ROOT)) if completed_copy.is_file() else None, "oracle_started_utc": oracle["started_utc"], "oracle_finished_utc": oracle["finished_utc"], "oracle_exit_code": oracle["exit_code"], "oracle_timed_out": oracle["timed_out"], "oracle_files": finished_files(oracle_text), "oracle_logs": {"stdout": oracle["stdout_log"], "stderr": oracle["stderr_log"], "metadata": oracle["meta_log"]}, "parsed_query_record_count": len(data), "parsed_explain_record_count": len(explain), "sort_topk_lines": [line for line in physical if "SortExec: TopK(fetch=2)" in line], "aggregate_limit_lines": limits, "physical_plan_rows": len(physical)}
            if not physical or len(data) != 1 or len(explain) != 1:
                errors.append(f"{case}: completed copy lacks query results or physical EXPLAIN plan")
            observed["confirmed_query_result_mismatch"] = confirmed_mismatch
            if case == "issue-on":
                if actual != BUGGY_ROWS or not limits or oracle["exit_code"] != 1 or oracle["timed_out"] or not re.search(r"\b1 failures\b", oracle_text) or not confirmed_mismatch:
                    errors.append("issue-on: expected reported wrong rows, Aggregate lim=[2], and exactly one hand-oracle mismatch")
                observed["classification"] = "expected_red" if actual == BUGGY_ROWS and bool(limits) and oracle["exit_code"] == 1 and not oracle["timed_out"] and re.search(r"\b1 failures\b", oracle_text) and confirmed_mismatch else "not_reproduced"
            elif case == "issue-off":
                if actual != expected or oracle["exit_code"] != 0 or limits:
                    errors.append("issue-off: expected correct oracle, passing hand test, and no Aggregate lim=[2]")
                observed["classification"] = "disabled_control"
            elif case == "positive":
                if actual != expected or oracle["exit_code"] != 0 or not limits:
                    errors.append("positive: expected oracle rows, passing hand test, and at least one Aggregate lim=[2]")
                observed["classification"] = "positive_control"
            else:
                mismatch = actual != expected
                oracle_reported_mismatch = oracle["exit_code"] == 1 and not oracle["timed_out"] and re.search(r"\b1 failures\b", oracle_text) is not None and confirmed_mismatch
                observed["additional_result_mismatch"] = mismatch
                observed["same_kind_wrong_result"] = mismatch and oracle_reported_mismatch and complete["exit_code"] == 0
                if mismatch:
                    observed["classification"] = "additional_result_mismatch"
                    if not observed["same_kind_wrong_result"]:
                        errors.append("distinct-probe: output differs, but the oracle run did not confirm exactly one result mismatch")
                else:
                    observed["classification"] = "exploratory_probe_pass"
                    if oracle["exit_code"] != 0:
                        errors.append("distinct-probe: observed output matches oracle, but oracle run failed")
            case_results[case] = observed
            dump(artifacts / "case-results.json", case_results)
            receipt["cases"] = case_results
            dump(artifacts / "receipt.json", receipt)

        after = source_facts(source)
        receipt["source_facts_after"] = after
        oracle_hashes_after = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(ORACLES.glob("*.slt"))}
        receipt["oracle_hashes_after"] = oracle_hashes_after
        if oracle_hashes_after != receipt["oracle_hashes_before"]:
            errors.append("hand-written oracle files changed during the run")
        if after["head"] != PIN or after["status_porcelain"] or after["diff_names"] or after["tracked_hashes"] != facts["tracked_hashes"]:
            errors.append("pinned DataFusion checkout changed during the run")
        receipt.update({"cases": case_results, "errors": errors, "finished_utc": utc(), "status": "diagnostic_pass" if not errors else "diagnostic_fail"})
        dump(artifacts / "case-results.json", case_results)
        dump(artifacts / "receipt.json", receipt)
        print(json.dumps({"status": receipt["status"], "errors": errors, "cases": case_results}, indent=2))
        return 0 if not errors else 1
    except Exception as exc:
        receipt.update({"errors": errors + [f"{type(exc).__name__}: {exc}"], "finished_utc": utc(), "status": "diagnostic_fail"})
        dump(artifacts / "receipt.json", receipt)
        print(receipt["errors"][-1], file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
