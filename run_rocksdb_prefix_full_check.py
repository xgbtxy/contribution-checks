#!/usr/bin/env python3
"""Run one pinned RocksDB full-check configuration and retain bounded evidence."""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import stat
import signal
import subprocess
import tarfile
import tempfile
import time
from pathlib import Path

import run_rocksdb_prefix as shared


SOURCE_PIN = "87d442749a6a9f0fe5b9f07de7346466f8dd5acf"
CANDIDATE_SHA256 = "cac9fade4b4bbb5c11fc0884e7febdd512734fe8c0a82d4678ab0a360b07cbd5"
EXPECTED_PATHS = set(shared.EXPECTED_CANDIDATE_PATHS)
EXPECTED_TRACKED_PATHS = EXPECTED_PATHS - {
    "unreleased_history/bug_fixes/reverse_bytewise_common_prefix.md"
}
STATUS_CHECK_EXCLUSIONS = {
    "test_binaries": ["c_test", "env_test", "range_locking_test", "testutil_test"],
    "auxiliary_targets": ["ldb_tests", "db_crashtest_tests", "rocksdb_dump_test"],
}
EXPECTED_REGRESSION_TESTS = (
    "BlockCommonPrefixTest.PrefixDoesNotIncludeInternalKeyFooter",
    "BlockCommonPrefixTest.LegacyDataBlocksPreserveSeekAndIteration",
    "BlockCommonPrefixTest.LegacyInternalKeyIndexBlockPreservesSeek",
    "DBBasicTest.ReverseBytewiseCommonPrefixKeepsLatestValueVisible",
)
REPO_URL = "https://github.com/meta/rocksdb"
JOB_TIMEOUT_SECONDS = 350 * 60
CASE_TIMEOUT_SECONDS = None  # Stock `make check` has no per-GTest timeout hook.


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.rstrip("\r\n")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def source_status(source: Path) -> dict[str, str]:
    raw = git(source, "status", "--porcelain=v1", "--untracked-files=normal")
    result: dict[str, str] = {}
    for line in raw.splitlines():
        if len(line) < 4:
            raise RuntimeError(f"unparseable git status line: {line!r}")
        result[line[3:]] = line[:2]
    return result


def verify_status_exclusions(source: Path) -> dict:
    makefile = (source / "Makefile").read_text(encoding="utf-8")
    failing = re.search(
        r"ifdef ASSERT_STATUS_CHECKED\s+.*?TESTS_FAILING_ASC\s*=\s*\\\n"
        r"(?P<items>(?:\s+\w+\s*\\?\s*\n)+)",
        makefile,
        re.DOTALL,
    )
    if failing is None:
        raise RuntimeError("pinned Makefile status-check exclusion list could not be parsed")
    actual_tests = re.findall(r"\b[a-z][a-z0-9_]*_test\b|\bc_test\b", failing.group("items"))
    if actual_tests != STATUS_CHECK_EXCLUSIONS["test_binaries"]:
        raise RuntimeError(f"status-check unit-test exclusions changed: {actual_tests}")
    extra_region = re.search(
        r"CHECK_EXTRA_TARGETS\s*=.*?ifndef ASSERT_STATUS_CHECKED[^\n]*\n"
        r"\s*CHECK_EXTRA_TARGETS\s*\+=\s*(?P<items>[^\n]+)",
        makefile,
        re.DOTALL,
    )
    if extra_region is None:
        raise RuntimeError("pinned Makefile status-check auxiliary exclusion list could not be parsed")
    actual_extras = re.findall(r"\b[a-z][a-z0-9_]*\b", extra_region.group("items"))
    if actual_extras != STATUS_CHECK_EXCLUSIONS["auxiliary_targets"]:
        raise RuntimeError(f"status-check auxiliary exclusions changed: {actual_extras}")
    return {"verified": True, "test_binaries": actual_tests, "auxiliary_targets": actual_extras}


def verify_candidate(source: Path, patch: Path, manifest_path: Path) -> dict:
    if git(source, "rev-parse", "HEAD") != SOURCE_PIN:
        raise RuntimeError("source checkout differs from the pinned main commit")
    remote = git(source, "remote", "get-url", "origin").rstrip("/").removesuffix(".git")
    if not remote.endswith("/meta/rocksdb") and not remote.endswith(":meta/rocksdb"):
        raise RuntimeError(f"unexpected RocksDB source remote: {remote}")
    if source_status(source):
        raise RuntimeError("RocksDB source checkout is not clean before the full-check run")
    if sha256(patch) != CANDIDATE_SHA256:
        raise RuntimeError("candidate patch SHA-256 differs from the reviewed frozen patch")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("source_base") != SOURCE_PIN:
        raise RuntimeError("candidate manifest base differs from the source pin")
    if manifest.get("candidate_patch_sha256") != CANDIDATE_SHA256:
        raise RuntimeError("candidate manifest patch hash differs from the frozen patch")
    if set(manifest.get("files", {})) != EXPECTED_PATHS:
        raise RuntimeError("candidate manifest does not contain the exact six reviewed files")
    check = subprocess.run(
        ["git", "-C", str(source), "apply", "--check", str(patch)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if check.returncode:
        raise RuntimeError(f"candidate patch does not apply: {check.stderr[-2000:]}")
    apply = subprocess.run(
        ["git", "-C", str(source), "apply", str(patch)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if apply.returncode:
        raise RuntimeError(f"candidate patch application failed: {apply.stderr[-2000:]}")
    paths = set(source_status(source))
    if paths != EXPECTED_PATHS:
        raise RuntimeError(f"applied paths differ from the six-file manifest: {sorted(paths)}")
    hashes = shared.verify_candidate_blobs(source, manifest, paths)
    return {
        "base": SOURCE_PIN,
        "remote": remote,
        "patch_sha256": CANDIDATE_SHA256,
        "patch_paths": sorted(paths),
        "patch_blobs": hashes,
    }


def verify_frozen_blobs(
    source: Path, manifest: dict, allow_build_outputs: bool = False
) -> dict:
    if git(source, "rev-parse", "HEAD") != SOURCE_PIN:
        raise RuntimeError("source HEAD changed during validation")
    statuses = source_status(source)
    tracked_paths = {path for path, status in statuses.items() if status != "??"}
    if tracked_paths != EXPECTED_TRACKED_PATHS:
        raise RuntimeError(f"tracked source paths changed: {sorted(tracked_paths)}")
    extras = sorted(set(statuses) - EXPECTED_PATHS)
    if extras and not allow_build_outputs:
        raise RuntimeError(f"unexpected source paths after check/precheck: {extras}")
    hashes = shared.verify_candidate_blobs(source, manifest, EXPECTED_PATHS)
    return {"head": SOURCE_PIN, "status_paths": statuses, "candidate_blobs": hashes}


def run_precheck(source: Path, artifacts: Path, env: dict[str, str], label: str,
                 command: list[str], manifest: dict) -> dict:
    logs = artifacts / "prechecks"
    logs.mkdir(parents=True, exist_ok=True)
    raw = logs / f"{label}.stdout.raw"
    started = utc()
    start = time.monotonic()
    with raw.open("wb") as stream:
        try:
            result = subprocess.run(
                command,
                cwd=source,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                timeout=300,
                check=False,
            )
            exit_code = result.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            exit_code = None
            timed_out = True
    log = compress_log(raw, logs / f"{label}.stdout.gz")
    raw.unlink(missing_ok=True)
    result_record = {
        "command": command,
        "started_utc": started,
        "elapsed_seconds": round(time.monotonic() - start, 2),
        "exit_code": exit_code,
        "timed_out": timed_out,
        "success": exit_code == 0 and not timed_out,
        "stdout": log,
    }
    record_path = logs / f"{label}.json"
    try:
        result_record["source_after"] = verify_frozen_blobs(source, manifest)
    except Exception as error:
        result_record["frozen_blob_verification_error"] = str(error)
    write_json(record_path, result_record)
    if not result_record["success"] or "frozen_blob_verification_error" in result_record:
        raise RuntimeError(
            f"{label} failed or changed the frozen candidate (exit={exit_code}, "
            f"timeout={timed_out}, blob_error="
            f"{result_record.get('frozen_blob_verification_error')}); "
            "see its JSON record and compressed output"
        )
    return result_record


def assert_full_check_execution(source: Path, artifacts: Path, progress: dict, mode: str) -> dict:
    if not isinstance(progress, dict) or progress.get("status") != "completed":
        raise RuntimeError(f"final check-progress does not report completion: {progress!r}")
    if not isinstance(progress.get("total"), int) or progress["total"] <= 0:
        raise RuntimeError(f"check-progress reports no generated test/check jobs: {progress!r}")
    if not isinstance(progress.get("completed"), int) or progress["completed"] < progress["total"]:
        raise RuntimeError(f"check-progress has incomplete jobs: {progress!r}")
    if progress.get("failed") != 0:
        raise RuntimeError(f"check-progress reports failed jobs: {progress!r}")

    log_path = source / "LOG"
    if not log_path.is_file() or log_path.stat().st_size == 0:
        raise RuntimeError("make check returned without a nonempty GNU Parallel LOG")
    with log_path.open("r", encoding="utf-8", errors="replace", newline="") as stream:
        rows = list(csv.DictReader(stream, delimiter="\t"))
    required_columns = {"Exitval", "Signal", "Command"}
    if not rows or not required_columns.issubset(rows[0]):
        raise RuntimeError("GNU Parallel LOG has no test/check rows or expected columns")
    expected_rows = progress["total"] + (1 if mode == "normal" else 0)
    if len(rows) != expected_rows:
        raise RuntimeError(
            f"GNU Parallel LOG rows ({len(rows)}) do not match generated check jobs "
            f"plus non-sharded c_test ({expected_rows})"
        )
    failing = [row for row in rows if row["Exitval"] != "0" or row["Signal"] != "0"]
    if failing:
        raise RuntimeError(f"GNU Parallel LOG records {len(failing)} failed job(s)")

    shard_rows = [
        row
        for row in rows
        if "run-block_test-shard-" in row["Command"]
        or "run-db_basic_test-shard-" in row["Command"]
    ]
    if not shard_rows:
        raise RuntimeError("successful job log contains no block_test or db_basic_test shard")
    logs_by_path: dict[Path, set[str]] = {}
    shard_status: dict[Path, bool] = {}
    all_markers = tuple(
        marker
        for test_name in EXPECTED_REGRESSION_TESTS
        for marker in (f"[ RUN      ] {test_name}", f"[       OK ] {test_name}")
    )
    for row in shard_rows:
        match = re.search(r"(?:^|[ \t])(?:\./)?t/(run-[^ \t;&]+)", row["Command"])
        if match is None:
            continue
        output_path = source / "t" / f"log-{match.group(1)}"
        if output_path.is_file():
            found: set[str] = set()
            with output_path.open("r", encoding="utf-8", errors="replace") as stream:
                for line in stream:
                    found.update(marker for marker in all_markers if marker in line)
            logs_by_path[output_path] = found
            shard_status[output_path] = row["Exitval"] == "0" and row["Signal"] == "0"
    evidence_dir = artifacts / "target-test-shard-logs"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    tests_by_log: dict[str, dict[str, dict[str, str]]] = {}
    for output_path, found in logs_by_path.items():
        case_markers = {
            test_name: {
                "run": f"[ RUN      ] {test_name}",
                "passed": f"[       OK ] {test_name}",
            }
            for test_name in EXPECTED_REGRESSION_TESTS
            if f"[ RUN      ] {test_name}" in found
            and f"[       OK ] {test_name}" in found
        }
        if case_markers or not shard_status.get(output_path, False):
            compressed = compress_log(
                output_path, evidence_dir / f"{output_path.name}.gz"
            )
            tests_by_log[str(output_path.relative_to(source))] = {
                "sha256": compressed["raw_sha256"],
                "bytes": compressed["raw_bytes"],
                "regression_cases": case_markers,
                "job_passed": shard_status.get(output_path, False),
                "artifact": compressed["gzip_path"],
            }
    missing = [
        test_name
        for test_name in EXPECTED_REGRESSION_TESTS
        if not any(test_name in metadata["regression_cases"] for metadata in tests_by_log.values())
    ]
    if missing:
        raise RuntimeError(
            f"make check succeeded without recorded RUN/OK output for regression "
            f"case(s): {missing}; inspected {len(logs_by_path)} target shard log(s)"
        )
    return {
        "check_progress": {key: progress.get(key) for key in ("status", "phase", "completed", "total", "failed", "percent")},
        "gnu_parallel_log_rows": len(rows),
        "gnu_parallel_failures": 0,
        "target_test_shards": len(shard_rows),
        "inspected_target_shard_logs": len(logs_by_path),
        "retained_target_shard_logs": tests_by_log,
        "regression_test_cases_ran_and_passed": list(EXPECTED_REGRESSION_TESTS),
        "status_mode_exclusions": STATUS_CHECK_EXCLUSIONS if mode == "status-checked" else None,
    }


def generated_db_split_merge_info(source: Path, artifact_dir: Path, mode: str, run: dict) -> dict:
    """Inventory the one generated tool by its Make rule and run output; never assume ELF."""
    path = source / "db_split_merge"
    info = {"path": "db_split_merge", "status": source_status(source).get("db_split_merge")}
    if not path.exists() and not path.is_symlink():
        return {**info, "observed": False}

    file_stat = path.lstat()
    info.update({
        "observed": True,
        "filesystem_type": (
            "regular" if stat.S_ISREG(file_stat.st_mode)
            else "symlink" if stat.S_ISLNK(file_stat.st_mode)
            else "directory" if stat.S_ISDIR(file_stat.st_mode)
            else "other"
        ),
        "mode_octal": oct(stat.S_IMODE(file_stat.st_mode)),
        "executable": os.access(path, os.X_OK),
        "mtime_epoch": file_stat.st_mtime,
        "size_bytes": file_stat.st_size if stat.S_ISREG(file_stat.st_mode) else None,
    })
    if stat.S_ISREG(file_stat.st_mode):
        with path.open("rb") as stream:
            info["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            stream.seek(0)
            info["first_16_bytes_hex"] = stream.read(16).hex()
    else:
        info["sha256"] = None

    try:
        file_probe = subprocess.run(
            ["file", "-b", "--", str(path)],
            cwd=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        info["file_utility"] = {
            "exit_code": file_probe.returncode,
            "classification": file_probe.stdout.strip(),
            "stderr": file_probe.stderr[-1000:],
        }
    except (OSError, subprocess.TimeoutExpired) as error:
        info["file_utility"] = {"error": f"{type(error).__name__}: {error}"}

    makefile = (source / "Makefile").read_text(encoding="utf-8")
    lines = makefile.splitlines()
    target_index = next((i for i, line in enumerate(lines) if line.startswith("db_split_merge:")), None)
    if target_index is None:
        info["make_rule"] = None
    else:
        rule_lines = [lines[target_index]]
        for line in lines[target_index + 1:]:
            if line.startswith("\t"):
                rule_lines.append(line)
            else:
                break
        info["make_rule"] = "\n".join(rule_lines)
    info["source_file"] = "tools/db_split_merge.cc"
    info["source_file_git_blob"] = git(source, "rev-parse", "HEAD:tools/db_split_merge.cc")
    src_mk = (source / "src.mk").read_text(encoding="utf-8", errors="replace")
    info["source_registered_in_src_mk"] = "tools/db_split_merge.cc" in src_mk

    log_record = run.get("stdout", {})
    log_path = Path(log_record.get("gzip_path", ""))
    matching_lines = []
    if log_path.is_file():
        with gzip.open(log_path, "rt", encoding="utf-8", errors="replace") as stream:
            matching_lines = [line.strip() for line in stream if "db_split_merge" in line]
    info["make_log_lines"] = matching_lines[:40]
    info["make_link_marker_observed"] = any(
        "CCLD" in line and re.search(r"\bdb_split_merge\b", line)
        for line in matching_lines
    )
    try:
        start_epoch = dt.datetime.fromisoformat(run["started_utc"]).timestamp()
    except Exception:
        start_epoch = None
    info["mtime_after_full_check_started"] = (
        start_epoch is not None and file_stat.st_mtime >= start_epoch
    )
    dependencies = ("$(OBJ_DIR)/tools/db_split_merge.o", "$(LIBRARY)")
    info["generated_make_target_verified"] = (
        info["filesystem_type"] == "regular"
        and info["executable"]
        and info["size_bytes"] > 0
        and info["make_rule"] is not None
        and all(dep in info["make_rule"].splitlines()[0] for dep in dependencies)
        and "$(AM_LINK)" in info["make_rule"]
        and info["source_registered_in_src_mk"]
        and info["make_link_marker_observed"]
        and info["mtime_after_full_check_started"]
    )
    return info


def audit_source_after_check(source: Path, artifact_dir: Path, mode: str, run: dict) -> dict:
    statuses = source_status(source)
    tracked = sorted(path for path, status in statuses.items() if status != "??")
    candidate_tracked = sorted(EXPECTED_TRACKED_PATHS)
    untracked = sorted(path for path, status in statuses.items() if status == "??")
    unexpected_tracked = sorted(set(tracked) - EXPECTED_TRACKED_PATHS)
    unexpected_untracked = sorted(set(untracked) - {
        "unreleased_history/bug_fixes/reverse_bytewise_common_prefix.md",
        "db_split_merge",
    })
    generated = None
    errors = []
    if tracked != candidate_tracked:
        errors.append(f"tracked path set differs from the frozen candidate: {tracked}")
    if unexpected_tracked:
        errors.append(f"unexpected tracked source paths after make check: {unexpected_tracked}")
    if unexpected_untracked:
        errors.append(f"unexpected untracked paths after make check: {unexpected_untracked}")
    if "db_split_merge" in untracked:
        generated = generated_db_split_merge_info(source, artifact_dir, mode, run)
        if not generated.get("generated_make_target_verified"):
            errors.append("db_split_merge is untracked but its generated-target provenance was not verified")
    return {
        "status_paths": statuses,
        "tracked_candidate_paths": tracked,
        "untracked_paths": untracked,
        "db_split_merge_generated_artifact": generated,
        "errors": errors,
    }


def environment(source: Path, artifact_dir: Path, mode: str) -> tuple[dict[str, str], Path]:
    runner_temp = Path(os.environ.get("RUNNER_TEMP", str(artifact_dir / "runner-temp")))
    run_tmp = runner_temp / f"rocksdb-tests-{mode}"
    run_tmp.mkdir(parents=True, exist_ok=True)
    env = os.environ.copy()
    for name in list(env):
        if name.startswith("ROCKSDB_DISABLE_"):
            env.pop(name)
    for name in (
        "ASSERT_STATUS_CHECKED",
        "ROCKSDBTESTS_SUBSET",
        "TESTS",
        "DRIVER",
        "ROCKSDB_PLUGINS",
        "EXCLUDE_TESTS_REGEX",
        "tests-regexp",
        "SKIP_FORMAT_BUCK_CHECKS",
        "CI_TOTAL_SHARDS",
        "CI_SHARD_INDEX",
    ):
        env.pop(name, None)
    env.update(
        {
            "AUTO_CLEAN": "1",
            "DEBUG_LEVEL": "1",
            "LIB_MODE": "shared",
            "NCORES": "2",
            "TEST_TMPDIR": str(run_tmp),
        }
    )
    if mode == "status-checked":
        env["ASSERT_STATUS_CHECKED"] = "1"
    else:
        env.pop("ASSERT_STATUS_CHECKED", None)
    return env, run_tmp


def tool_output(command: list[str], cwd: Path) -> dict:
    result = subprocess.run(command, cwd=cwd, capture_output=True, check=False)
    return {
        "command": command,
        "exit_code": result.returncode,
        "stdout": result.stdout.decode("utf-8", errors="replace")[-4000:],
        "stderr": result.stderr.decode("utf-8", errors="replace")[-4000:],
    }


def fingerprint(source: Path, artifact_dir: Path) -> dict:
    facts = {
        "uname": tool_output(["uname", "-a"], source),
        "cpu": tool_output(["lscpu"], source),
        "nproc": tool_output(["nproc"], source),
        "memory": tool_output(["bash", "-lc", "grep MemTotal /proc/meminfo"], source),
        "disk": tool_output(["df", "-h", ".", "/tmp", "/dev/shm"], source),
        "g++": tool_output(["g++", "--version"], source),
        "make": tool_output(["make", "--version"], source),
        "python": tool_output(["python3", "--version"], source),
        "git": tool_output(["git", "--version"], source),
        "source_head": git(source, "rev-parse", "HEAD"),
        "source_disk": tool_output(["df", "-h", str(source)], source),
    }
    write_json(artifact_dir / "runner.json", facts)
    return facts


def compress_log(raw_path: Path, gzip_path: Path) -> dict:
    digest = hashlib.sha256()
    size = 0
    tail: list[str] = []
    with raw_path.open("rb") as source, gzip.open(gzip_path, "wb", compresslevel=6) as dest:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            dest.write(chunk)
            digest.update(chunk)
            size += len(chunk)
            tail.extend(chunk.decode("utf-8", errors="replace").splitlines())
            tail = tail[-60:]
    return {"gzip_path": str(gzip_path), "raw_bytes": size, "raw_sha256": digest.hexdigest(), "tail": tail}


def make_progress(source: Path, env: dict[str, str]) -> dict:
    try:
        result = subprocess.run(
            ["make", "check-progress"],
            cwd=source,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return {"exit_code": None, "raw": f"progress probe failed: {error}", "json": None}
    text = (result.stdout + result.stderr).strip()
    try:
        progress = json.loads(result.stdout.strip())
    except (json.JSONDecodeError, TypeError):
        progress = None
    return {"exit_code": result.returncode, "raw": text[-4000:], "json": progress}


def run_mode(source: Path, artifact_dir: Path, mode: str, receipt: dict,
             manifest: dict) -> dict:
    env, run_tmp = environment(source, artifact_dir, mode)
    env["SKIP_FORMAT_BUCK_CHECKS"] = "1"
    logs = artifact_dir / "logs" / mode
    logs.mkdir(parents=True, exist_ok=True)
    temp = logs / "make-check.stdout.raw"
    progress_path = logs / "check-progress.jsonl"
    command = ["make", "-j2", "J=2", "check"]
    if mode == "status-checked":
        command.append("ASSERT_STATUS_CHECKED=1")
    start_utc = utc()
    started = time.monotonic()
    command_result: dict = {
        "mode": mode,
        "command": command,
        "environment": {key: env[key] for key in (
            "AUTO_CLEAN", "DEBUG_LEVEL", "LIB_MODE", "NCORES", "TEST_TMPDIR"
        )},
        "assert_status_checked": mode == "status-checked",
        "skip_format_buck_checks": env["SKIP_FORMAT_BUCK_CHECKS"],
        "native_check_extras_preserved": ["check-all-python"]
        + ([] if mode == "status-checked" else ["ldb_tests", "db_crashtest_tests", "rocksdb_dump_test"]),
        "per_gtest_case_timeout_seconds": CASE_TIMEOUT_SECONDS,
        "status_check_exclusions": STATUS_CHECK_EXCLUSIONS if mode == "status-checked" else None,
        "started_utc": start_utc,
        "timeout_seconds": JOB_TIMEOUT_SECONDS,
    }
    if mode == "status-checked":
        command_result["upstream_exclusions"] = STATUS_CHECK_EXCLUSIONS
    receipt.setdefault("runs", {})[mode] = command_result
    write_json(artifact_dir / "receipt.json", receipt)
    next_progress = time.monotonic()
    timed_out = False
    exit_code = None
    raw_fd = temp.open("wb")
    try:
        process = subprocess.Popen(
            command,
            cwd=source,
            env=env,
            stdout=raw_fd,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        while process.poll() is None:
            now = time.monotonic()
            if now - started >= JOB_TIMEOUT_SECONDS:
                timed_out = True
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    process.wait()
                break
            if now >= next_progress:
                snapshot = {"captured_utc": utc(), **make_progress(source, env)}
                if snapshot.get("json") is not None:
                    print(json.dumps({"make_check_progress": snapshot["json"]}), flush=True)
                with progress_path.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(snapshot, sort_keys=True) + "\n")
                receipt["runs"][mode]["last_progress"] = snapshot
                write_json(artifact_dir / "receipt.json", receipt)
                next_progress = now + 45
            time.sleep(2)
        exit_code = process.returncode
    finally:
        raw_fd.close()

    # Preserve the native Make result before any secondary evidence processing.
    # A later archive, progress, log-schema, or source-audit failure must not
    # erase or relabel a real make-check failure.
    native_make_succeeded = exit_code == 0 and not timed_out
    receipt["runs"][mode]["native_process_outcome"] = {
        "exit_code": exit_code,
        "timed_out": timed_out,
        "native_make_succeeded": native_make_succeeded,
        "captured_utc": utc(),
    }
    write_json(artifact_dir / "receipt.json", receipt)

    final_progress = {"captured_utc": utc(), **make_progress(source, env)}
    with progress_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(final_progress, sort_keys=True) + "\n")
    log_info = compress_log(temp, logs / "make-check.stdout.gz")
    temp.unlink(missing_ok=True)
    elapsed = time.monotonic() - started
    head_after = git(source, "rev-parse", "HEAD")
    frozen_blob_error = None
    post_build_blobs = None
    if head_after == SOURCE_PIN:
        try:
            post_build_blobs = verify_frozen_blobs(source, manifest, allow_build_outputs=True)
        except Exception as error:
            frozen_blob_error = str(error)
    else:
        frozen_blob_error = f"HEAD changed during full check: {head_after}"
    test_execution = None
    execution_evidence_error = None
    command_result.update(
        {
            "finished_utc": utc(),
            "elapsed_seconds": round(elapsed, 2),
            "exit_code": exit_code,
            "timed_out": timed_out,
            "native_make_succeeded": native_make_succeeded,
            "success": False,
            "final_progress": final_progress,
            "test_execution": test_execution,
            "head_after": head_after,
            "head_remained_pinned": head_after == SOURCE_PIN,
            "candidate_blobs_after": post_build_blobs,
            "frozen_blob_verification_error": frozen_blob_error,
            "stdout": log_info,
            "progress_jsonl": str(progress_path),
            "make_log_sha256": sha256(source / "LOG") if (source / "LOG").is_file() else None,
        }
    )
    make_config = source / "make_config.mk"
    if make_config.is_file():
        config_text = make_config.read_text(encoding="utf-8", errors="replace")
        command_result["make_config"] = {
            "sha256": sha256(make_config),
            "bytes": make_config.stat().st_size,
            "snappy_enabled": "-DSNAPPY" in config_text,
        }
    log_path = source / "LOG"
    if log_path.is_file():
        shutil.copy2(log_path, logs / "make.LOG")
    # Keep raw test evidence even if a later progress/log-schema check fails.
    test_logs = sorted(path for path in (source / "t").glob("log-*") if path.is_file())
    if test_logs:
        archive = artifact_dir / "test-logs.tar.gz"
        with tarfile.open(archive, "w:gz") as bundle:
            for path in test_logs:
                bundle.add(path, arcname=path.name, recursive=False)
        command_result["test_log_archive"] = {
            "files": len(test_logs), "path": archive.name,
            "bytes": archive.stat().st_size, "sha256": sha256(archive),
        }
    if native_make_succeeded and not frozen_blob_error:
        try:
            test_execution = assert_full_check_execution(
                source, artifact_dir, final_progress.get("json"), mode
            )
        except Exception as error:
            execution_evidence_error = f"{type(error).__name__}: {error}"
    command_result["test_execution"] = test_execution
    command_result["execution_evidence_error"] = execution_evidence_error
    command_result["secondary_source_error"] = frozen_blob_error
    command_result["success"] = (
        native_make_succeeded
        and test_execution is not None
        and frozen_blob_error is None
        and execution_evidence_error is None
    )
    write_json(artifact_dir / "receipt.json", receipt)
    return command_result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--mode", required=True, choices=("normal", "status-checked"))
    parser.add_argument("--candidate-patch", required=True, type=Path)
    parser.add_argument("--candidate-manifest", required=True, type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": 1,
        "project": REPO_URL,
        "issue": "https://github.com/meta/rocksdb/issues/15439",
        "phase": "full_make_check",
        "source_pin": SOURCE_PIN,
        "frozen_patch_sha256": CANDIDATE_SHA256,
        "workflow": {
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "ref": os.environ.get("GITHUB_REF"),
            "helper_sha": os.environ.get("GITHUB_SHA"),
        },
        "recorded_utc": utc(),
    }
    write_json(artifacts / "receipt.json", receipt)
    try:
        receipt["source_before"] = shared.verify_source_pin(source)
        receipt["source_before"]["status_porcelain_preserved"] = git(
            source, "status", "--porcelain=v1", "--untracked-files=normal"
        )
        receipt["runner"] = fingerprint(source, artifacts)
        receipt["product_patch"] = verify_candidate(
            source, args.candidate_patch.resolve(), args.candidate_manifest.resolve()
        )
        manifest = json.loads(args.candidate_manifest.resolve().read_text(encoding="utf-8"))
        receipt["source_makefile_sha256"] = sha256(source / "Makefile")
        receipt["verified_status_mode_exclusions"] = verify_status_exclusions(source)
        receipt["test_timeout_policy"] = (
            "stock_make_check_does_not_cap_each_gtest_case; focused_regressions_are_capped_at_60s"
        )
        make_config = source / "make_config.mk"
        if make_config.is_file():
            receipt["make_config_before_check"] = {
                "sha256": sha256(make_config),
                "bytes": make_config.stat().st_size,
            }
        receipt["mode"] = args.mode
        write_json(artifacts / "receipt.json", receipt)
        precheck_env, _ = environment(source, artifacts, "normal")
        receipt["precheck_environment"] = {
            "skip_format_buck_checks": precheck_env.get("SKIP_FORMAT_BUCK_CHECKS"),
            "formatter_binary": precheck_env.get("CLANG_FORMAT_BINARY"),
            "formatter_diff": precheck_env.get("CLANG_FORMAT_DIFF"),
        }
        precheck_targets = [
            ("format_auto", ["make", "format-auto"]),
            ("check_format", ["make", "VERBOSE_CHECK=1", "check-format"]),
            ("check_buck_targets", ["make", "check-buck-targets"]),
            ("check_sources", ["make", "check-sources"]),
            ("check_workflow_yaml", ["make", "check-workflow-yaml"]),
            (
                "check_c_api_gen",
                [
                    "make",
                    f"API_COMPAT_REF={SOURCE_PIN}",
                    "CLANG_FORMAT_BINARY=clang-format",
                    "check-c-api-gen",
                ],
            ),
        ]
        receipt["prechecks"] = {}
        for label, command in precheck_targets:
            receipt["prechecks"][label] = run_precheck(
                source, artifacts, precheck_env, label, command, manifest
            )
            write_json(artifacts / "receipt.json", receipt)
        write_json(artifacts / "receipt.json", receipt)
        result = run_mode(source, artifacts, args.mode, receipt, manifest)
        try:
            receipt["source_after"] = audit_source_after_check(
                source, artifacts, args.mode, receipt["runs"][args.mode]
            )
        except Exception as audit_error:
            receipt["source_after"] = {"errors": [f"{type(audit_error).__name__}: {audit_error}"]}
        native_succeeded = result["native_make_succeeded"]
        source_audit_failed = bool(receipt["source_after"].get("errors"))
        if not native_succeeded:
            receipt["status"] = "full_make_check_failed"
        elif not result["success"] or source_audit_failed:
            receipt["status"] = "validation_runner_failed_after_make_success"
        else:
            receipt["status"] = "passed"
    except Exception as error:
        receipt.setdefault("errors", []).append(f"{type(error).__name__}: {error}")
        receipt["status"] = "validation_runner_failed"
        try:
            receipt["source_after_failure"] = {"status_paths": source_status(source), "head": git(source, "rev-parse", "HEAD")}
        except Exception as snapshot_error:
            receipt["errors"].append(f"source snapshot failed: {type(snapshot_error).__name__}: {snapshot_error}")
    finally:
        receipt["finished_utc"] = utc()
        write_json(artifacts / "receipt.json", receipt)
    run = receipt.get("runs", {}).get(args.mode, {})
    if "stdout" in run:
        run["stdout"] = {key: value for key, value in run["stdout"].items() if key != "tail"}
    print(json.dumps({"status": receipt["status"], "mode": receipt.get("mode"), "run": run}, sort_keys=True))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
