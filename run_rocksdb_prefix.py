#!/usr/bin/env python3
"""Validate RocksDB issue #15439 from an exact upstream source pin."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SOURCE_PIN = "87d442749a6a9f0fe5b9f07de7346466f8dd5acf"
ISSUE_URL = "https://github.com/meta/rocksdb/issues/15439"
FOCUSED_GTEST = "DBBasicTest.ReverseBytewiseCommonPrefixKeepsLatestValueVisible"
SCENARIOS = ("update", "delete", "new")
REPORTER_MODE_ARGUMENT = {"update": 0, "delete": 1, "new": 2}
DISABLED_OPTIONAL_FEATURES = {
    "ROCKSDB_DISABLE_GFLAGS": "1",
    "ROCKSDB_DISABLE_SNAPPY": "1",
    "ROCKSDB_DISABLE_ZLIB": "1",
    "ROCKSDB_DISABLE_BZIP": "1",
    "ROCKSDB_DISABLE_LZ4": "1",
    "ROCKSDB_DISABLE_ZSTD": "1",
    "ROCKSDB_DISABLE_JEMALLOC": "1",
    "ROCKSDB_DISABLE_TCMALLOC": "1",
    "ROCKSDB_DISABLE_NUMA": "1",
    "ROCKSDB_DISABLE_MEMKIND": "1",
    "ROCKSDB_DISABLE_BACKTRACE": "1",
}
SOURCE_FILES = (
    "table/block_based/block_builder.cc",
    "table/block_based/block.h",
    "table/block_based/block.cc",
    "table/block_based/block_test.cc",
    "db/dbformat.h",
    "db/db_basic_test.cc",
)
HELPER_COMPILE_FLAGS = (
    "-DNDEBUG",
    "-fno-rtti",
    "-DOS_LINUX",
    "-DROCKSDB_PLATFORM_POSIX",
    "-DROCKSDB_LIB_IO_POSIX",
)
HELPER_FILES = (
    ".github/workflows/hdf5-unsigned.yml",
    "REPORTER-ATTRIBUTION.md",
    "report-15439-repro.cc",
    "rocksdb_prefix_block_fixtures.cc",
    "rocksdb_prefix_old_sst.cc",
    "run_rocksdb_prefix.py",
)


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


def tail(data: bytes, count: int = 30) -> list[str]:
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").splitlines()[-count:]


def run_command(
    receipt: dict,
    name: str,
    command: list[str],
    cwd: Path,
    log_dir: Path,
    timeout: int,
    env: dict[str, str],
    expected_exit: int | None = 0,
) -> dict:
    command = [str(part) for part in command]
    started = utc()
    try:
        process = subprocess.run(
            command,
            cwd=cwd,
            env=env,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        stdout = process.stdout
        stderr = process.stderr
        exit_code = process.returncode
        timed_out = False
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        if isinstance(stdout, str):
            stdout = stdout.encode("utf-8")
        if isinstance(stderr, str):
            stderr = stderr.encode("utf-8")
        exit_code = None
        timed_out = True

    stdout_path = log_dir / f"{name}.stdout.gz"
    stderr_path = log_dir / f"{name}.stderr.gz"
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(stdout_path, "wb", compresslevel=6) as stream:
        stream.write(stdout)
    with gzip.open(stderr_path, "wb", compresslevel=6) as stream:
        stream.write(stderr)

    result = {
        "command": command,
        "cwd": str(cwd),
        "started_utc": started,
        "finished_utc": utc(),
        "timeout_seconds": timeout,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "expected_exit_code": expected_exit,
        "stdout_bytes": len(stdout),
        "stderr_bytes": len(stderr),
        "stdout_sha256": sha256_bytes(stdout),
        "stderr_sha256": sha256_bytes(stderr),
        "stdout_tail": tail(stdout),
        "stderr_tail": tail(stderr),
        "stdout_log_gzip": str(stdout_path.relative_to(log_dir.parent)),
        "stderr_log_gzip": str(stderr_path.relative_to(log_dir.parent)),
        "ok": not timed_out and (expected_exit is None or exit_code == expected_exit),
    }
    receipt.setdefault("commands", {})[name] = result
    write_json(log_dir.parent / "receipt.json", receipt)
    print(
        json.dumps(
            {
                "step": name,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "stdout_bytes": len(stdout),
                "stderr_bytes": len(stderr),
                "ok": result["ok"],
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return {**result, "stdout": stdout, "stderr": stderr}


def source_snapshot(source: Path) -> dict:
    hashes = {}
    for relative in SOURCE_FILES:
        path = source / relative
        if path.is_file():
            hashes[relative] = {
                "worktree_sha256": sha256_file(path),
                "head_blob": git(source, "rev-parse", f"HEAD:{relative}"),
            }
    return {
        "head": git(source, "rev-parse", "HEAD"),
        "origin_url": git(source, "remote", "get-url", "origin"),
        "status_porcelain": git(source, "status", "--porcelain=v1"),
        "tracked_diff_paths": git(source, "diff", "--name-only").splitlines(),
        "tracked_source_hashes": hashes,
    }


def verify_source_pin(source: Path) -> dict:
    snapshot = source_snapshot(source)
    if snapshot["head"] != SOURCE_PIN:
        raise RuntimeError(f"source HEAD {snapshot['head']} != pin {SOURCE_PIN}")
    if snapshot["tracked_diff_paths"]:
        raise RuntimeError(f"baseline source has tracked changes: {snapshot['tracked_diff_paths']}")
    normalized_origin = snapshot["origin_url"].rstrip("/")
    if normalized_origin.endswith(".git"):
        normalized_origin = normalized_origin[:-4]
    if not normalized_origin.endswith("/meta/rocksdb") and not normalized_origin.endswith(":meta/rocksdb"):
        raise RuntimeError(f"unexpected source origin: {snapshot['origin_url']}")
    return snapshot


def make_env() -> dict[str, str]:
    env = os.environ.copy()
    env.update(DISABLED_OPTIONAL_FEATURES)
    env["AUTO_CLEAN"] = "1"
    env["DEBUG_LEVEL"] = "0"
    env["LC_ALL"] = "C.UTF-8"
    return env


def build_static_library(receipt: dict, source: Path, log_dir: Path, env: dict[str, str], name: str) -> None:
    result = run_command(
        receipt,
        name,
        ["make", "AUTO_CLEAN=1", "DEBUG_LEVEL=0", "-j2", "static_lib"],
        source,
        log_dir,
        timeout=30 * 60,
        env=env,
    )
    if not result["ok"]:
        raise RuntimeError(f"{name} failed or timed out")
    library = source / "librocksdb.a"
    if not library.is_file():
        raise RuntimeError("static_lib succeeded but librocksdb.a is missing")
    receipt.setdefault("libraries", {})[name] = {
        "path": "librocksdb.a",
        "bytes": library.stat().st_size,
        "sha256": sha256_file(library),
        "debug_level": "0",
        "optional_compression_and_allocator_features": "disabled for the CPU repro",
    }
    config = source / "make_config.mk"
    config_text = config.read_text(encoding="utf-8", errors="replace") if config.is_file() else ""
    forbidden_flags = ["-DSNAPPY", "-DZLIB", "-DBZIP2", "-DLZ4", "-DZSTD"]
    found_flags = [flag for flag in forbidden_flags if flag in config_text]
    receipt["disabled_feature_check"] = {
        "make_config_sha256": sha256_file(config) if config.is_file() else None,
        "unexpected_optional_flags": found_flags,
    }
    if found_flags:
        raise RuntimeError(f"optional codec unexpectedly enabled: {found_flags}")
    write_json(log_dir.parent / "receipt.json", receipt)


def compile_program(
    receipt: dict,
    source: Path,
    output_dir: Path,
    log_dir: Path,
    env: dict[str, str],
    name: str,
    helper_source: Path,
) -> Path:
    output = output_dir / name
    command = [
        "g++",
        "-std=c++20",
        "-O2",
        *HELPER_COMPILE_FLAGS,
        "-Iinclude",
        "-I.",
        str(helper_source),
        "librocksdb.a",
        "-pthread",
        "-ldl",
        "-lrt",
        "-latomic",
        "-o",
        str(output),
    ]
    result = run_command(
        receipt,
        f"compile-{name}",
        command,
        source,
        log_dir,
        timeout=5 * 60,
        env=env,
    )
    if not result["ok"] or not output.is_file():
        raise RuntimeError(f"failed to compile {name}")
    return output


def run_repro_matrix(
    receipt: dict,
    binary: Path,
    source: Path,
    artifact_dir: Path,
    log_dir: Path,
    env: dict[str, str],
    phase: str,
) -> list[dict]:
    results = []
    for version in (7, 8):
        for mode in SCENARIOS:
            db_path = artifact_dir / "repro-db" / phase / f"v{version}-{mode}"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            baseline_v8 = phase == "baseline" and version == 8
            expected_exit = 1 if baseline_v8 else 0
            result = run_command(
                receipt,
                f"{phase}-repro-v{version}-{mode}",
                [str(binary), str(db_path), str(version), str(REPORTER_MODE_ARGUMENT[mode])],
                source,
                log_dir,
                timeout=30,
                env=env,
                expected_exit=expected_exit,
            )
            stdout = result["stdout"].decode("utf-8", errors="replace")
            reports_wrong = "<- WRONG" in stdout
            if not result["ok"]:
                raise RuntimeError(
                    f"{phase} v{version}/{mode}: expected exit {expected_exit}, "
                    f"got {result['exit_code']}"
                )
            if baseline_v8 != reports_wrong:
                raise RuntimeError(
                    f"{phase} v{version}/{mode}: WRONG marker mismatch, exit={result['exit_code']}"
                )
            expected_value = "<NotFound>" if mode == "delete" else "balance=250"
            if baseline_v8:
                actual_value = {
                    "update": "balance=100",
                    "delete": "balance=100",
                    "new": "<NotFound>",
                }[mode]
            else:
                actual_value = expected_value
            seek_value = (
                "<end>" if actual_value == "<NotFound>" else f"user1={actual_value}"
            )
            expected_outputs = (
                f"Get={actual_value}",
                f"MultiGet={actual_value}",
                f"Seek(user1)={seek_value}",
            )
            missing_outputs = [value for value in expected_outputs if value not in stdout]
            if missing_outputs:
                raise RuntimeError(
                    f"{phase} v{version}/{mode}: expected report output missing: {missing_outputs}"
                )
            if phase == "candidate" and reports_wrong:
                raise RuntimeError(f"candidate still reproduces the v8 {mode} mismatch")
            results.append(
                {
                    "format_version": version,
                    "mode": mode,
                    "reporter_mode_argument": REPORTER_MODE_ARGUMENT[mode],
                    "expected_exit": expected_exit,
                    "actual_exit": result["exit_code"],
                    "expected_baseline_red": baseline_v8,
                    "observed_get_value": actual_value,
                    "expected_correct_value": expected_value,
                    "expected_get_multiget_seek": list(expected_outputs),
                    "missing_expected_output": missing_outputs,
                    "wrong_marker": reports_wrong,
                    "db_path": str(db_path.relative_to(artifact_dir)),
                    "stdout_tail": result["stdout_tail"],
                    "ok": result["ok"] and baseline_v8 == reports_wrong,
                }
            )
    return results


def fixture_manifest(
    receipt: dict,
    binary: Path,
    source: Path,
    artifact_dir: Path,
    log_dir: Path,
    env: dict[str, str],
    phase: str,
) -> list[dict]:
    fixture_dir = artifact_dir / "fixtures" / phase
    fixture_dir.mkdir(parents=True, exist_ok=True)
    result = run_command(
        receipt,
        f"{phase}-generate-block-fixtures",
        [str(binary), str(fixture_dir)],
        source,
        log_dir,
        timeout=30,
        env=env,
    )
    if not result["ok"]:
        raise RuntimeError(f"{phase} block fixture generation failed")
    manifest = []
    for raw_path in sorted(fixture_dir.glob("*.bin")):
        data = raw_path.read_bytes()
        hex_path = raw_path.with_suffix(".hex")
        hex_text = data.hex()
        hex_path.write_text(hex_text + "\n", encoding="ascii")
        manifest.append(
            {
                "name": raw_path.stem,
                "bytes": len(data),
                "sha256": sha256_bytes(data),
                "raw_path": str(raw_path.relative_to(artifact_dir)),
                "hex_path": str(hex_path.relative_to(artifact_dir)),
                "hex": hex_text,
            }
        )
    if not manifest:
        raise RuntimeError(f"{phase} generated no raw block fixtures")
    receipt.setdefault("block_fixtures", {})[phase] = manifest
    write_json(artifact_dir / "block-fixtures.json", receipt["block_fixtures"])
    return manifest


def old_sst_inventory(database: Path) -> list[dict]:
    inventory = []
    for path in sorted(database.rglob("*")):
        if path.is_file():
            inventory.append(
                {
                    "path": str(path.relative_to(database)),
                    "bytes": path.stat().st_size,
                    "sha256": sha256_file(path),
                }
            )
    return inventory


def write_old_sst_fixtures(
    receipt: dict,
    binary: Path,
    source: Path,
    artifact_dir: Path,
    log_dir: Path,
    env: dict[str, str],
) -> dict:
    result = {}
    for mode in SCENARIOS:
        database = artifact_dir / "old-sst" / f"baseline-v8-{mode}"
        database.parent.mkdir(parents=True, exist_ok=True)
        run = run_command(
            receipt,
            f"baseline-create-old-sst-{mode}",
            [str(binary), "write", str(database), mode],
            source,
            log_dir,
            timeout=30,
            env=env,
        )
        if not run["ok"]:
            raise RuntimeError(f"baseline old-SST writer failed for {mode}")
        inventory = old_sst_inventory(database)
        if not any(item["path"].endswith((".sst", ".ldb")) for item in inventory):
            raise RuntimeError(f"baseline old-SST writer produced no SST for {mode}")
        result[mode] = {
            "database": str(database.relative_to(artifact_dir)),
            "files_before_candidate_read": inventory,
        }
    receipt["old_sst"] = result
    return result


def run_candidate_compatibility(
    receipt: dict,
    binary: Path,
    source: Path,
    artifact_dir: Path,
    log_dir: Path,
    env: dict[str, str],
) -> None:
    for mode in SCENARIOS:
        database = artifact_dir / "old-sst" / f"baseline-v8-{mode}"
        before = old_sst_inventory(database)
        result = run_command(
            receipt,
            f"candidate-read-old-sst-{mode}",
            [str(binary), "read", str(database), mode],
            source,
            log_dir,
            timeout=30,
            env=env,
        )
        stdout = result["stdout"].decode("utf-8", errors="replace")
        if not result["ok"] or "result=PASS" not in stdout:
            raise RuntimeError(f"candidate failed to read baseline-created v8 SSTs ({mode})")
        after = old_sst_inventory(database)
        before_ssts = [item for item in before if item["path"].endswith((".sst", ".ldb"))]
        after_ssts = [item for item in after if item["path"].endswith((".sst", ".ldb"))]
        if before_ssts != after_ssts:
            raise RuntimeError(f"candidate read modified SST files for {mode}")
        receipt["old_sst"][mode]["files_after_candidate_read"] = after
        receipt["old_sst"][mode]["candidate_read_result"] = "PASS"


def candidate_patch_paths(patch: bytes) -> set[str]:
    return {
        line[6:]
        for line in patch.decode("utf-8", errors="replace").splitlines()
        if line.startswith("+++ b/")
    }


def resolve_helper_path(raw: str) -> Path:
    path = (ROOT / raw).resolve()
    if not path.is_relative_to(ROOT.resolve()):
        raise RuntimeError("candidate patch must be inside the checked-out validation helper")
    if not path.is_file():
        raise FileNotFoundError(f"candidate patch does not exist: {path}")
    return path


def record_helper_hashes() -> dict:
    result = {}
    for relative in HELPER_FILES:
        path = ROOT / relative
        result[relative] = {
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
        }
    return result


def run_candidate(
    receipt: dict,
    source: Path,
    artifact_dir: Path,
    log_dir: Path,
    output_dir: Path,
    env: dict[str, str],
    patch_path: Path,
    run_focused_gtest: bool,
) -> None:
    patch_bytes = patch_path.read_bytes()
    expected_paths = candidate_patch_paths(patch_bytes)
    if not expected_paths:
        raise RuntimeError("candidate patch has no file paths")
    receipt["candidate"] = {
        "status": "applying_patch",
        "patch_path": str(patch_path.relative_to(ROOT)),
        "patch_sha256": sha256_bytes(patch_bytes),
        "expected_paths": sorted(expected_paths),
    }
    check = run_command(
        receipt,
        "candidate-patch-check",
        ["git", "apply", "--check", str(patch_path)],
        source,
        log_dir,
        timeout=30,
        env=env,
    )
    if not check["ok"]:
        raise RuntimeError("candidate patch does not apply cleanly to the pinned source")
    apply = run_command(
        receipt,
        "candidate-patch-apply",
        ["git", "apply", str(patch_path)],
        source,
        log_dir,
        timeout=30,
        env=env,
    )
    if not apply["ok"]:
        raise RuntimeError("candidate patch application failed")
    snapshot = source_snapshot(source)
    changed = set(snapshot["tracked_diff_paths"])
    if changed != expected_paths:
        raise RuntimeError(
            f"patched source paths {sorted(changed)} differ from patch headers {sorted(expected_paths)}"
        )
    receipt["candidate"]["source_after_patch"] = snapshot

    build_static_library(receipt, source, log_dir, env, "candidate-static-lib")
    repro = compile_program(
        receipt,
        source,
        output_dir,
        log_dir,
        env,
        "rocksdb-prefix-candidate-repro",
        ROOT / "report-15439-repro.cc",
    )
    compat = compile_program(
        receipt,
        source,
        output_dir,
        log_dir,
        env,
        "rocksdb-prefix-candidate-old-sst",
        ROOT / "rocksdb_prefix_old_sst.cc",
    )
    fixtures = compile_program(
        receipt,
        source,
        output_dir,
        log_dir,
        env,
        "rocksdb-prefix-candidate-block-fixtures",
        ROOT / "rocksdb_prefix_block_fixtures.cc",
    )
    receipt["candidate"]["repro_matrix"] = run_repro_matrix(
        receipt, repro, source, artifact_dir, log_dir, env, "candidate"
    )
    receipt["candidate"]["block_fixtures"] = fixture_manifest(
        receipt, fixtures, source, artifact_dir, log_dir, env, "candidate"
    )
    run_candidate_compatibility(receipt, compat, source, artifact_dir, log_dir, env)

    if run_focused_gtest:
        debug_env = dict(env)
        debug_env["DEBUG_LEVEL"] = "1"
        build_test = run_command(
            receipt,
            "candidate-debug-db-basic-test-build",
            ["make", "AUTO_CLEAN=1", "DEBUG_LEVEL=1", "-j2", "db_basic_test"],
            source,
            log_dir,
            timeout=35 * 60,
            env=debug_env,
        )
        if not build_test["ok"]:
            raise RuntimeError("candidate db_basic_test build failed")
        test = run_command(
            receipt,
            "candidate-focused-gtest",
            [
                "timeout",
                "60s",
                str(source / "db_basic_test"),
                f"--gtest_filter={FOCUSED_GTEST}",
            ],
            source,
            log_dir,
            timeout=70,
            env=debug_env,
        )
        if not test["ok"]:
            raise RuntimeError(f"candidate focused gtest failed: {FOCUSED_GTEST}")
        receipt["candidate"]["focused_gtest"] = {
            "filter": FOCUSED_GTEST,
            "status": "PASS",
            "timeout_seconds": 60,
        }
    else:
        receipt["candidate"]["focused_gtest"] = {"status": "not_requested"}
    receipt["candidate"]["status"] = "candidate_checks_pass"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument("--candidate-patch", default="")
    parser.add_argument("--run-focused-gtest", action="store_true")
    args = parser.parse_args()

    source = args.source.resolve()
    artifact_dir = args.artifacts.resolve()
    log_dir = artifact_dir / "logs"
    output_dir = artifact_dir / "bin-temp"
    artifact_dir.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)

    receipt = {
        "status": "running",
        "issue": ISSUE_URL,
        "reporter": "hanke580",
        "coordination_comment": "https://github.com/meta/rocksdb/issues/15439#issuecomment-6514576612",
        "started_utc": utc(),
        "source_pin": SOURCE_PIN,
        "source_path": str(source),
        "workflow": {
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "workflow": os.environ.get("GITHUB_WORKFLOW"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "ref": os.environ.get("GITHUB_REF"),
            "harness_sha": os.environ.get("GITHUB_SHA"),
        },
        "build": {
            "runner": "ubuntu-24.04",
            "execution": "CPU-only",
            "make_jobs": 2,
            "debug_level": 0,
            "helper_compile_flags": list(HELPER_COMPILE_FLAGS),
            "optional_features": DISABLED_OPTIONAL_FEATURES,
        },
        "helper_file_hashes": record_helper_hashes(),
        "block_fixture_spec": {
            "restart_interval": {
                "value_tag_overlap_restart_boundary": 2,
                "delete_tag_overlap": 2,
                "empty_user_footer_overlap": 16,
            },
            "use_common_prefix": True,
            "use_delta_encoding": True,
            "use_value_delta_encoding": False,
            "data_block_index": "binary search",
            "use_separated_kv_storage": False,
            "is_user_key": False,
            "timestamp_size": 0,
            "uniform_cv_threshold": -1.0,
            "entries": {
                "value_tag_overlap_restart_boundary": [
                    {"user_key_hex": "7573657231017a", "sequence": 9, "type": "value", "value": "v0"},
                    {"user_key_hex": "75736572310179", "sequence": 8, "type": "value", "value": "v1"},
                    {"user_key_hex": "757365723101", "sequence": 3, "type": "value", "value": "v2"},
                    {"user_key_hex": "7573657231", "sequence": 2, "type": "value", "value": "v3"},
                ],
                "delete_tag_overlap": [
                    {"user_key_hex": "757365723100", "sequence": 3, "type": "value", "value": "v0"},
                    {"user_key_hex": "7573657231", "sequence": 2, "type": "deletion", "value": "v1"},
                ],
                "empty_user_footer_overlap": [
                    {"user_key_hex": "0100000000000000", "sequence": 1, "type": "value", "value": "n0"},
                    {"user_key_hex": "", "sequence": 0, "type": "value", "value": "v0"},
                ],
            },
        },
        "commands": {},
        "errors": [],
    }
    write_json(artifact_dir / "receipt.json", receipt)

    try:
        baseline_source = verify_source_pin(source)
        receipt["source_before_baseline"] = baseline_source
        env = make_env()

        output_dir.mkdir(parents=True, exist_ok=True)
        build_static_library(receipt, source, log_dir, env, "baseline-static-lib")
        base_repro = compile_program(
            receipt,
            source,
            output_dir,
            log_dir,
            env,
            "rocksdb-prefix-baseline-repro",
            ROOT / "report-15439-repro.cc",
        )
        base_compat = compile_program(
                receipt,
                source,
                output_dir,
                log_dir,
                env,
                "rocksdb-prefix-baseline-old-sst",
                ROOT / "rocksdb_prefix_old_sst.cc",
            )
        base_fixtures = compile_program(
                receipt,
                source,
                output_dir,
                log_dir,
                env,
                "rocksdb-prefix-baseline-block-fixtures",
                ROOT / "rocksdb_prefix_block_fixtures.cc",
            )
        after_build = source_snapshot(source)
        if after_build["tracked_source_hashes"] != baseline_source["tracked_source_hashes"]:
            raise RuntimeError("baseline build changed one or more tracked source files")
        receipt["source_after_baseline_build"] = after_build
        receipt["baseline"] = {
            "repro_matrix": run_repro_matrix(
                receipt, base_repro, source, artifact_dir, log_dir, env, "baseline"
            ),
            "block_fixtures": fixture_manifest(
                receipt, base_fixtures, source, artifact_dir, log_dir, env, "baseline"
            ),
            "old_sst": write_old_sst_fixtures(
                receipt, base_compat, source, artifact_dir, log_dir, env
            ),
        }
        receipt["baseline"]["status"] = "reproduction_confirmed"

        if args.candidate_patch:
            patch_path = resolve_helper_path(args.candidate_patch)
            run_candidate(
                receipt,
                source,
                artifact_dir,
                log_dir,
                output_dir,
                env,
                patch_path,
                args.run_focused_gtest,
            )
        else:
            if args.run_focused_gtest:
                raise RuntimeError("focused gtest requires --candidate-patch")
            receipt["candidate"] = {"status": "not_requested"}

        receipt["source_after"] = source_snapshot(source)
        receipt["status"] = (
            "candidate_checks_pass"
            if receipt["candidate"]["status"] == "candidate_checks_pass"
            else "baseline_reproduction_confirmed"
        )
    except Exception as error:  # Keep a structured receipt for every failed phase.
        receipt["errors"].append(f"{type(error).__name__}: {error}")
        receipt["status"] = "validation_failed"
        try:
            receipt["source_after_failure"] = source_snapshot(source)
        except Exception as snapshot_error:
            receipt["errors"].append(
                f"source snapshot failed: {type(snapshot_error).__name__}: {snapshot_error}"
            )
    finally:
        receipt["finished_utc"] = utc()
        if output_dir.exists():
            shutil.rmtree(output_dir)
        write_json(artifact_dir / "receipt.json", receipt)

    print(
        json.dumps(
            {
                "status": receipt["status"],
                "baseline": receipt.get("baseline", {}).get("status"),
                "candidate": receipt.get("candidate", {}).get("status"),
                "errors": receipt["errors"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if receipt["status"] in {"baseline_reproduction_confirmed", "candidate_checks_pass"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
