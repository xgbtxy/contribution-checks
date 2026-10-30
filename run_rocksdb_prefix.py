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
BLOCK_GTEST_SUITE = "BlockCommonPrefixTest"
TEST_ONLY_PATCH_PATHS = {
    "db/db_basic_test.cc",
    "table/block_based/block_test.cc",
}
EXPECTED_CANDIDATE_PATHS = {
    "db/db_basic_test.cc",
    "table/block_based/block.cc",
    "table/block_based/block.h",
    "table/block_based/block_builder.cc",
    "table/block_based/block_test.cc",
    "unreleased_history/bug_fixes/reverse_bytewise_common_prefix.md",
}
EXPECTED_BLOCK_GTESTS = {
    "BlockCommonPrefixTest.PrefixDoesNotIncludeInternalKeyFooter",
    "BlockCommonPrefixTest.LegacyDataBlocksPreserveSeekAndIteration",
    "BlockCommonPrefixTest.LegacyInternalKeyIndexBlockPreservesSeek",
}
EXPECTED_RED_MARKERS = {
    "BlockCommonPrefixTest.PrefixDoesNotIncludeInternalKeyFooter": (
        "user_key.size()",
        "prefix_size",
        "Which is: 0",
        "Which is: 1",
    ),
    "BlockCommonPrefixTest.LegacyDataBlocksPreserveSeekAndIteration": (
        "iter->Valid()",
        "Which is: false",
        "Which is: true",
    ),
    "BlockCommonPrefixTest.LegacyInternalKeyIndexBlockPreservesSeek": (
        "iter->Valid()",
        "Which is: false",
        "Which is: true",
    ),
    FOCUSED_GTEST: ("balance=250", "balance=100"),
}
SCENARIOS = ("update", "delete", "new")
REPORTER_MODE_ARGUMENT = {"update": 0, "delete": 1, "new": 2}
DISABLED_OPTIONAL_FEATURES = {
    "ROCKSDB_DISABLE_GFLAGS": "1",
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
ENABLED_REQUIRED_FEATURES = {"SNAPPY": "system libsnappy-dev"}
SOURCE_FILES = (
    "table/block_based/block_builder.cc",
    "table/block_based/block.h",
    "table/block_based/block.cc",
    "table/block_based/block_test.cc",
    "db/dbformat.h",
    "db/db_basic_test.cc",
)
HELPER_PLATFORM_FLAGS = (
    "-DOS_LINUX",
    "-DROCKSDB_PLATFORM_POSIX",
    "-DROCKSDB_LIB_IO_POSIX",
)
RELEASE_HELPER_COMPILE_FLAGS = HELPER_PLATFORM_FLAGS + ("-DNDEBUG", "-fno-rtti")
DEBUG_HELPER_COMPILE_FLAGS = HELPER_PLATFORM_FLAGS + ("-DROCKSDB_USE_RTTI",)
HELPER_FILES = (
    ".github/workflows/hdf5-unsigned.yml",
    "REPORTER-ATTRIBUTION.md",
    "patches/rocksdb-prefix-15439/TESTS_ONLY.patch",
    "patches/rocksdb-prefix-15439/CANDIDATE.patch",
    "patches/rocksdb-prefix-15439/candidate-manifest.json",
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


def capture_runtime_metadata(source: Path) -> dict:
    metadata = {}
    for name, command in (
        ("uname", ["uname", "-a"]),
        ("g++_version", ["g++", "--version"]),
        ("g++_numeric_version", ["g++", "-dumpfullversion", "-dumpversion"]),
        ("make_version", ["make", "--version"]),
        ("git_version", ["git", "--version"]),
        ("cpu_count", ["nproc"]),
        ("disk", ["df", "-h", "."]),
    ):
        result = subprocess.run(
            command,
            cwd=source,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=15,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(f"runtime metadata command failed: {command}")
        output = result.stdout.strip()
        if name in {"g++_version", "make_version"}:
            output = output.splitlines()[0] if output else ""
        metadata[name] = output
    meminfo = Path("/proc/meminfo").read_text(encoding="ascii", errors="replace")
    metadata["mem_total"] = next(
        (line.strip() for line in meminfo.splitlines() if line.startswith("MemTotal:")),
        "unavailable",
    )
    return metadata


def make_config_facts(source: Path) -> dict:
    path = source / "make_config.mk"
    if not path.is_file():
        return {"path": str(path), "exists": False}
    selected = []
    patterns = (
        "COMMON_FLAGS",
        "PLATFORM_CXXFLAGS",
        "OPT",
        "CXXFLAGS",
        "LDFLAGS",
        "PLATFORM_LDFLAGS",
    )
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if any(line.lstrip().startswith(name) for name in patterns):
            selected.append(line)
    return {
        "path": "make_config.mk",
        "sha256": sha256_file(path),
        "selected_flag_lines": selected,
    }


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


def source_status_paths(status: str) -> set[str]:
    paths = set()
    for line in status.splitlines():
        if line.strip():
            paths.add(line[3:].strip().strip('"'))
    return paths


def source_snapshot(source: Path, additional_paths: set[str] | None = None) -> dict:
    hashes = {}
    for relative in sorted(set(SOURCE_FILES) | (additional_paths or set())):
        path = source / relative
        if path.is_file():
            head_blob_result = subprocess.run(
                ["git", "-C", str(source), "rev-parse", "--verify", f"HEAD:{relative}"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=False,
            )
            hashes[relative] = {
                "worktree_sha256": sha256_file(path),
                "head_blob": head_blob_result.stdout.strip()
                if head_blob_result.returncode == 0
                else None,
            }
        else:
            hashes[relative] = {"exists": False, "head_blob": None}
    status = git(source, "status", "--porcelain=v1")
    return {
        "head": git(source, "rev-parse", "HEAD"),
        "origin_url": git(source, "remote", "get-url", "origin"),
        "status_porcelain": status,
        "status_paths": sorted(source_status_paths(status)),
        "tracked_diff_paths": git(source, "diff", "--name-only").splitlines(),
        "tracked_source_hashes": hashes,
    }


def verify_source_pin(source: Path) -> dict:
    snapshot = source_snapshot(source)
    if snapshot["head"] != SOURCE_PIN:
        raise RuntimeError(f"source HEAD {snapshot['head']} != pin {SOURCE_PIN}")
    if snapshot["status_paths"]:
        raise RuntimeError(f"baseline source has changes: {snapshot['status_paths']}")
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
        "enabled_required_features": ENABLED_REQUIRED_FEATURES,
        "disabled_optional_features": DISABLED_OPTIONAL_FEATURES,
    }
    config = source / "make_config.mk"
    config_text = config.read_text(encoding="utf-8", errors="replace") if config.is_file() else ""
    forbidden_flags = ["-DZLIB", "-DBZIP2", "-DLZ4", "-DZSTD"]
    found_flags = [flag for flag in forbidden_flags if flag in config_text]
    snappy_enabled = "-DSNAPPY" in config_text
    receipt["disabled_feature_check"] = {
        "make_config_sha256": sha256_file(config) if config.is_file() else None,
        "unexpected_optional_flags": found_flags,
        "required_feature_flags": {"-DSNAPPY": snappy_enabled},
    }
    if found_flags:
        raise RuntimeError(f"optional codec unexpectedly enabled: {found_flags}")
    if not snappy_enabled:
        raise RuntimeError("Snappy must be enabled for the baseline build")
    receipt["make_config"] = make_config_facts(source)
    write_json(log_dir.parent / "receipt.json", receipt)


def compile_program(
    receipt: dict,
    source: Path,
    output_dir: Path,
    log_dir: Path,
    env: dict[str, str],
    name: str,
    helper_source: Path,
    debug_level: int = 0,
) -> Path:
    output = output_dir / name
    compile_flags = (
        DEBUG_HELPER_COMPILE_FLAGS if debug_level == 1 else RELEASE_HELPER_COMPILE_FLAGS
    )
    command = [
        "g++",
        "-std=c++20",
        "-O2",
        *compile_flags,
        "-Iinclude",
        "-I.",
        str(helper_source),
        "librocksdb.a",
        "-pthread",
        "-ldl",
        "-lrt",
        "-latomic",
        "-lsnappy",
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
    receipt.setdefault("helper_compiles", {})[name] = {
        "debug_level": debug_level,
        "flags": list(compile_flags),
        "binary_sha256": sha256_file(output),
        "binary_bytes": output.stat().st_size,
    }
    write_json(log_dir.parent / "receipt.json", receipt)
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
        # DestroyDB locks path/LOCK before it can remove an existing database.
        database.mkdir(parents=True, exist_ok=True)
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


def list_block_common_prefix_tests(
    receipt: dict,
    block_test: Path,
    source: Path,
    log_dir: Path,
    env: dict[str, str],
    phase: str,
) -> list[str]:
    result = run_command(
        receipt,
        f"{phase}-list-block-common-prefix-tests",
        [
            str(block_test),
            "--gtest_list_tests",
            f"--gtest_filter={BLOCK_GTEST_SUITE}.*",
        ],
        source,
        log_dir,
        timeout=30,
        env=env,
    )
    if not result["ok"]:
        raise RuntimeError(f"could not list {BLOCK_GTEST_SUITE} tests")
    output = result["stdout"].decode("utf-8", errors="replace")
    suite = ""
    filters = []
    for line in output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if not line[:1].isspace() and stripped.endswith("."):
            suite = stripped[:-1]
        elif line[:1].isspace() and suite.startswith(BLOCK_GTEST_SUITE):
            test_name = stripped.split("#", 1)[0].strip().split(" ", 1)[0]
            if test_name and not test_name.startswith("DISABLED_"):
                filters.append(f"{suite}.{test_name}")
    if set(filters) != EXPECTED_BLOCK_GTESTS:
        raise RuntimeError(
            f"expected three {BLOCK_GTEST_SUITE} cases, discovered {filters}"
        )
    return filters


def run_focused_gtest_cases(
    receipt: dict,
    source: Path,
    log_dir: Path,
    env: dict[str, str],
    phase: str,
    expect_pass: bool,
    build_targets: bool = True,
) -> list[dict]:
    debug_env = dict(env)
    debug_env["DEBUG_LEVEL"] = "1"
    if build_targets:
        build = run_command(
            receipt,
            f"{phase}-debug-test-build",
            [
                "make",
                "AUTO_CLEAN=1",
                "DEBUG_LEVEL=1",
                "-j2",
                "static_lib",
                "block_test",
                "db_basic_test",
            ],
            source,
            log_dir,
            timeout=35 * 60,
            env=debug_env,
        )
        if not build["ok"]:
            raise RuntimeError(f"{phase} debug test build failed")

    filters = list_block_common_prefix_tests(
        receipt, source / "block_test", source, log_dir, debug_env, phase
    )
    filters.append(FOCUSED_GTEST)
    results = []
    for filter_name in filters:
        is_block_test = filter_name.startswith(BLOCK_GTEST_SUITE + ".")
        binary = source / ("block_test" if is_block_test else "db_basic_test")
        safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", filter_name)
        result = run_command(
            receipt,
            f"{phase}-gtest-{safe_name}",
            [
                "timeout",
                "60s",
                str(binary),
                f"--gtest_filter={filter_name}",
            ],
            source,
            log_dir,
            timeout=70,
            env=debug_env,
            expected_exit=0 if expect_pass else None,
        )
        output = (result["stdout"] + result["stderr"]).decode(
            "utf-8", errors="replace"
        )
        if expect_pass:
            passed_markers = (
                f"[ RUN      ] {filter_name}",
                f"[       OK ] {filter_name}",
                "[  PASSED  ] 1 test",
            )
            missing = [marker for marker in passed_markers if marker not in output]
            ok = result["exit_code"] == 0 and not result["timed_out"] and not missing
            if not ok:
                raise RuntimeError(
                    f"{phase} focused gtest failed or did not execute one test: "
                    f"{filter_name}; missing={missing}"
                )
        else:
            # Only a named gtest assertion with expected mismatch context is
            # evidence of the regression. Timeouts, aborts, and unrelated
            # nonzero exits remain validation failures for human review.
            expected_markers = (
                f"[ RUN      ] {filter_name}",
                f"[  FAILED  ] {filter_name}",
                *EXPECTED_RED_MARKERS[filter_name],
            )
            missing = [marker for marker in expected_markers if marker not in output]
            ok = (
                result["exit_code"] not in (None, 0, 124)
                and not result["timed_out"]
                and not missing
            )
            if not ok:
                raise RuntimeError(
                    f"{phase} expected baseline assertion did not match: "
                    f"{filter_name}; missing={missing}; exit={result['exit_code']}"
                )
        results.append(
            {
                "filter": filter_name,
                "binary": binary.name,
                "expected": "pass" if expect_pass else "red",
                "exit_code": result["exit_code"],
                "timed_out": result["timed_out"],
                "expected_assertion_markers": list(
                    EXPECTED_RED_MARKERS.get(filter_name, ())
                ),
                "failure_marker": f"[  FAILED  ] {filter_name}" in output,
                "timeout_seconds": 60,
                "ok": ok,
            }
        )
    return results


def run_tests_only_red_patch(
    receipt: dict,
    source: Path,
    log_dir: Path,
    env: dict[str, str],
    patch_path: Path,
) -> None:
    patch = patch_path.read_bytes()
    expected_paths = candidate_patch_paths(patch)
    if expected_paths != TEST_ONLY_PATCH_PATHS:
        raise RuntimeError(
            f"tests-only patch paths {sorted(expected_paths)} differ from expected "
            f"{sorted(TEST_ONLY_PATCH_PATHS)}"
        )
    receipt["tests_only_baseline"] = {
        "status": "applying_patch",
        "patch_path": str(patch_path.relative_to(ROOT)),
        "patch_sha256": sha256_bytes(patch),
        "expected_paths": sorted(expected_paths),
    }
    for suffix, args in (
        ("check", ["git", "apply", "--check", str(patch_path)]),
        ("apply", ["git", "apply", str(patch_path)]),
    ):
        result = run_command(
            receipt,
            f"tests-only-patch-{suffix}",
            args,
            source,
            log_dir,
            timeout=30,
            env=env,
        )
        if not result["ok"]:
            raise RuntimeError(f"tests-only patch {suffix} failed")
    snapshot = source_snapshot(source, expected_paths)
    if set(snapshot["status_paths"]) != expected_paths:
        raise RuntimeError("tests-only source paths do not match the patch headers")
    receipt["tests_only_baseline"]["source_after_patch"] = snapshot
    red_results = run_focused_gtest_cases(
        receipt, source, log_dir, env, "tests-only-baseline-red", expect_pass=False
    )
    receipt["tests_only_baseline"]["tests"] = red_results
    reverse = run_command(
        receipt,
        "tests-only-patch-reverse",
        ["git", "apply", "--reverse", str(patch_path)],
        source,
        log_dir,
        timeout=30,
        env=env,
    )
    if not reverse["ok"]:
        raise RuntimeError("could not reverse the tests-only patch")
    clean = source_snapshot(source)
    if clean["status_paths"]:
        raise RuntimeError(f"source is not clean after reversing tests-only patch: {clean['status_paths']}")
    receipt["tests_only_baseline"]["source_after_reversal"] = clean
    receipt["tests_only_baseline"]["status"] = "expected_red_confirmed"


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
    snapshot = source_snapshot(source, expected_paths)
    changed = set(snapshot["status_paths"])
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


def apply_review_patch(
    receipt: dict,
    source: Path,
    log_dir: Path,
    env: dict[str, str],
    patch_path: Path,
    phase: str,
    required_paths: set[str] | None = None,
) -> tuple[set[str], dict]:
    patch_bytes = patch_path.read_bytes()
    expected_paths = candidate_patch_paths(patch_bytes)
    if not expected_paths:
        raise RuntimeError(f"{phase} patch has no file paths")
    if required_paths is not None and expected_paths != required_paths:
        raise RuntimeError(
            f"{phase} patch paths {sorted(expected_paths)} differ from expected "
            f"{sorted(required_paths)}"
        )
    patch_info = {
        "status": "checking_patch",
        "patch_path": str(patch_path.relative_to(ROOT)),
        "patch_sha256": sha256_bytes(patch_bytes),
        "expected_paths": sorted(expected_paths),
    }
    for suffix, command in (
        ("check", ["git", "apply", "--check", str(patch_path)]),
        ("apply", ["git", "apply", str(patch_path)]),
    ):
        result = run_command(
            receipt,
            f"{phase}-patch-{suffix}",
            command,
            source,
            log_dir,
            timeout=30,
            env=env,
        )
        if not result["ok"]:
            raise RuntimeError(f"{phase} patch {suffix} failed")
    snapshot = source_snapshot(source, expected_paths)
    changed = set(snapshot["status_paths"])
    if changed != expected_paths:
        raise RuntimeError(
            f"{phase} source paths {sorted(changed)} differ from patch headers "
            f"{sorted(expected_paths)}"
        )
    patch_info["source_after_patch"] = snapshot
    patch_info["status"] = "applied"
    return expected_paths, patch_info


def reverse_review_patch(
    receipt: dict,
    source: Path,
    log_dir: Path,
    env: dict[str, str],
    patch_path: Path,
    phase: str,
) -> dict:
    result = run_command(
        receipt,
        f"{phase}-patch-reverse",
        ["git", "apply", "--reverse", str(patch_path)],
        source,
        log_dir,
        timeout=30,
        env=env,
    )
    if not result["ok"]:
        raise RuntimeError(f"could not reverse the {phase} patch")
    snapshot = source_snapshot(source)
    if snapshot["status_paths"]:
        raise RuntimeError(
            f"source is not clean after reversing {phase} patch: {snapshot['status_paths']}"
        )
    return snapshot


def parse_cpp_hex_array(source: Path, array_name: str) -> bytes:
    text = source.read_text(encoding="utf-8")
    pattern = rf"const char\s+{re.escape(array_name)}\[\]\s*=([\s\S]*?);"
    match = re.search(pattern, text)
    if not match:
        raise RuntimeError(f"legacy golden array is missing: {array_name}")
    literals = re.findall(r'"((?:\\.|[^"\\])*)"', match.group(1))
    if not literals:
        raise RuntimeError(f"legacy golden array has no string literals: {array_name}")
    data = bytearray()
    for literal in literals:
        escapes = re.findall(r"\\x([0-9a-fA-F]{2})", literal)
        residual = re.sub(r"\\x[0-9a-fA-F]{2}", "", literal)
        if residual:
            data.extend(residual.encode("latin-1"))
        data.extend(int(value, 16) for value in escapes)
    return bytes(data)


def verify_legacy_fixture_literals(
    receipt: dict, source: Path, artifact_dir: Path, fixtures: list[dict]
) -> list[dict]:
    mapping = {
        "kLegacyValueTagOverlapRestartBoundary": "value_tag_overlap_restart_boundary",
        "kLegacyDeleteTagOverlap": "delete_tag_overlap",
        "kLegacyEmptyUserFullFooterOverlap": "empty_user_full_footer_overlap",
    }
    by_name = {fixture["name"]: fixture for fixture in fixtures}
    test_source = source / "table/block_based/block_test.cc"
    comparisons = []
    for array_name, fixture_name in mapping.items():
        if fixture_name not in by_name:
            raise RuntimeError(f"generated legacy fixture missing: {fixture_name}")
        expected = parse_cpp_hex_array(test_source, array_name)
        raw_path = artifact_dir / by_name[fixture_name]["raw_path"]
        actual = raw_path.read_bytes()
        comparison = {
            "array": array_name,
            "fixture": fixture_name,
            "expected_bytes": len(expected),
            "actual_bytes": len(actual),
            "expected_sha256": sha256_bytes(expected),
            "actual_sha256": sha256_bytes(actual),
            "matches": expected == actual,
        }
        if not comparison["matches"]:
            raise RuntimeError(
                f"baseline BlockBuilder bytes do not match immutable {array_name} literal"
            )
        comparisons.append(comparison)
    receipt["legacy_fixture_comparisons"] = comparisons
    write_json(artifact_dir / "legacy-fixture-comparisons.json", comparisons)
    return comparisons


def verify_candidate_manifest(
    source: Path,
    manifest_path: Path,
    tests_only_patch: Path,
    candidate_patch: Path,
    required_candidate_paths: set[str] | None = None,
) -> dict:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("source_base") != SOURCE_PIN:
        raise RuntimeError("candidate manifest base differs from the pinned source")
    if sha256_file(tests_only_patch) != manifest.get("tests_only_patch_sha256"):
        raise RuntimeError("tests-only patch SHA-256 differs from the candidate manifest")
    if sha256_file(candidate_patch) != manifest.get("candidate_patch_sha256"):
        raise RuntimeError("candidate patch SHA-256 differs from the candidate manifest")
    paths = set(manifest.get("files", {}))
    if required_candidate_paths is not None and paths != required_candidate_paths:
        raise RuntimeError("candidate manifest file list differs from the approved six-file set")
    for relative in TEST_ONLY_PATCH_PATHS:
        spec = manifest["files"].get(relative)
        if not spec:
            raise RuntimeError(f"candidate manifest omits tests-only path: {relative}")
    return manifest


def verify_candidate_blobs(
    source: Path, manifest: dict, paths: set[str]
) -> dict[str, str]:
    hashes = {}
    for relative in sorted(paths):
        if relative not in manifest["files"]:
            raise RuntimeError(f"candidate manifest has no blob for {relative}")
        path = source / relative
        if not path.is_file():
            raise RuntimeError(f"candidate source file is missing: {relative}")
        actual = git(source, "hash-object", str(path))
        expected = manifest["files"][relative]["git_blob"]
        if actual != expected:
            raise RuntimeError(
                f"candidate blob mismatch for {relative}: {actual} != {expected}"
            )
        hashes[relative] = actual
    return hashes


def build_debug_targets(
    receipt: dict,
    source: Path,
    log_dir: Path,
    env: dict[str, str],
    name: str,
) -> dict[str, str]:
    debug_env = dict(env)
    debug_env["DEBUG_LEVEL"] = "1"
    result = run_command(
        receipt,
        name,
        [
            "make",
            "AUTO_CLEAN=1",
            "DEBUG_LEVEL=1",
            "-j2",
            "static_lib",
            "block_test",
            "db_basic_test",
        ],
        source,
        log_dir,
        timeout=35 * 60,
        env=debug_env,
    )
    if not result["ok"]:
        raise RuntimeError(f"{name} failed or timed out")
    config = make_config_facts(source)
    selected_flags = "\n".join(config.get("selected_flag_lines", []))
    if "-DSNAPPY" not in selected_flags:
        raise RuntimeError("Snappy is not enabled in the debug make_config")
    for flag in ("-DZLIB", "-DBZIP2", "-DLZ4", "-DZSTD"):
        if flag in selected_flags:
            raise RuntimeError(f"unexpected optional codec in debug make_config: {flag}")
    receipt["make_config"] = config
    for binary in ("librocksdb.a", "block_test", "db_basic_test"):
        path = source / binary
        if not path.is_file():
            raise RuntimeError(f"debug build succeeded but {binary} is missing")
    receipt.setdefault("libraries", {})[name] = {
        "path": "librocksdb.a",
        "bytes": (source / "librocksdb.a").stat().st_size,
        "sha256": sha256_file(source / "librocksdb.a"),
        "debug_level": "1",
        "test_binaries": {
            binary: sha256_file(source / binary)
            for binary in ("block_test", "db_basic_test")
        },
    }
    write_json(log_dir.parent / "receipt.json", receipt)
    return debug_env


def run_focused_candidate_phase_impl(
    receipt: dict,
    source: Path,
    artifact_dir: Path,
    log_dir: Path,
    output_dir: Path,
    env: dict[str, str],
    tests_only_patch: Path,
    candidate_patch: Path,
    manifest_path: Path,
) -> None:
    manifest = verify_candidate_manifest(
        source,
        manifest_path,
        tests_only_patch,
        candidate_patch,
        EXPECTED_CANDIDATE_PATHS,
    )
    tests_paths, tests_info = apply_review_patch(
        receipt,
        source,
        log_dir,
        env,
        tests_only_patch,
        "tests-only",
        TEST_ONLY_PATCH_PATHS,
    )
    tests_info["source_blob_sha256"] = verify_candidate_blobs(
        source, manifest, tests_paths
    )
    receipt["tests_only_baseline"] = tests_info
    debug_env = build_debug_targets(
        receipt, source, log_dir, env, "tests-only-baseline-debug-build"
    )

    base_compat = compile_program(
        receipt,
        source,
        output_dir,
        log_dir,
        debug_env,
        "rocksdb-prefix-debug-baseline-old-sst",
        ROOT / "rocksdb_prefix_old_sst.cc",
        debug_level=1,
    )
    base_fixtures = compile_program(
        receipt,
        source,
        output_dir,
        log_dir,
        debug_env,
        "rocksdb-prefix-debug-baseline-block-fixtures",
        ROOT / "rocksdb_prefix_block_fixtures.cc",
        debug_level=1,
    )
    baseline_fixtures = fixture_manifest(
        receipt,
        base_fixtures,
        source,
        artifact_dir,
        log_dir,
        debug_env,
        "focused-baseline",
    )
    receipt["focused_baseline"] = {
        "block_fixtures": baseline_fixtures,
        "legacy_fixture_comparisons": verify_legacy_fixture_literals(
            receipt, source, artifact_dir, baseline_fixtures
        ),
        "old_sst": write_old_sst_fixtures(
            receipt, base_compat, source, artifact_dir, log_dir, debug_env
        ),
    }
    receipt["tests_only_baseline"]["tests"] = run_focused_gtest_cases(
        receipt,
        source,
        log_dir,
        debug_env,
        "tests-only-baseline-red",
        expect_pass=False,
        build_targets=False,
    )
    receipt["tests_only_baseline"]["source_after_reversal"] = reverse_review_patch(
        receipt, source, log_dir, debug_env, tests_only_patch, "tests-only"
    )
    receipt["tests_only_baseline"]["status"] = "expected_red_confirmed"

    candidate_paths, candidate_info = apply_review_patch(
        receipt,
        source,
        log_dir,
        debug_env,
        candidate_patch,
        "candidate",
        EXPECTED_CANDIDATE_PATHS,
    )
    candidate_info["source_blob_sha256"] = verify_candidate_blobs(
        source, manifest, candidate_paths
    )
    receipt["candidate"] = candidate_info
    debug_env = build_debug_targets(
        receipt, source, log_dir, debug_env, "candidate-incremental-debug-build"
    )
    candidate_repro = compile_program(
        receipt,
        source,
        output_dir,
        log_dir,
        debug_env,
        "rocksdb-prefix-debug-candidate-repro",
        ROOT / "report-15439-repro.cc",
        debug_level=1,
    )
    candidate_compat = compile_program(
        receipt,
        source,
        output_dir,
        log_dir,
        debug_env,
        "rocksdb-prefix-debug-candidate-old-sst",
        ROOT / "rocksdb_prefix_old_sst.cc",
        debug_level=1,
    )
    candidate_fixtures = compile_program(
        receipt,
        source,
        output_dir,
        log_dir,
        debug_env,
        "rocksdb-prefix-debug-candidate-block-fixtures",
        ROOT / "rocksdb_prefix_block_fixtures.cc",
        debug_level=1,
    )

    # Read baseline-created files before any candidate binary writes new files.
    run_candidate_compatibility(
        receipt, candidate_compat, source, artifact_dir, log_dir, debug_env
    )
    receipt["candidate"]["repro_matrix"] = run_repro_matrix(
        receipt, candidate_repro, source, artifact_dir, log_dir, debug_env, "candidate"
    )
    receipt["candidate"]["block_fixtures"] = fixture_manifest(
        receipt,
        candidate_fixtures,
        source,
        artifact_dir,
        log_dir,
        debug_env,
        "candidate-debug",
    )
    receipt["candidate"]["focused_gtest"] = run_focused_gtest_cases(
        receipt,
        source,
        log_dir,
        debug_env,
        "candidate-focused-green",
        expect_pass=True,
        build_targets=False,
    )
    receipt["candidate"]["status"] = "focused_candidate_checks_pass"


def run_release_baseline_phase(
    receipt: dict,
    source: Path,
    artifact_dir: Path,
    log_dir: Path,
    output_dir: Path,
    env: dict[str, str],
) -> None:
    baseline_source = verify_source_pin(source)
    receipt["source_before_baseline"] = baseline_source
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
        debug_level=0,
    )
    base_compat = compile_program(
        receipt,
        source,
        output_dir,
        log_dir,
        env,
        "rocksdb-prefix-baseline-old-sst",
        ROOT / "rocksdb_prefix_old_sst.cc",
        debug_level=0,
    )
    base_fixtures = compile_program(
        receipt,
        source,
        output_dir,
        log_dir,
        env,
        "rocksdb-prefix-baseline-block-fixtures",
        ROOT / "rocksdb_prefix_block_fixtures.cc",
        debug_level=0,
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
        "status": "reproduction_confirmed",
    }


def run_focused_candidate_phase(
    receipt: dict,
    source: Path,
    artifact_dir: Path,
    log_dir: Path,
    output_dir: Path,
    env: dict[str, str],
    tests_only_patch: Path,
    candidate_patch: Path,
    manifest_path: Path,
) -> None:
    source_before = verify_source_pin(source)
    receipt["source_before_focused"] = source_before
    run_focused_candidate_phase_impl(
        receipt,
        source,
        artifact_dir,
        log_dir,
        output_dir,
        env,
        tests_only_patch,
        candidate_patch,
        manifest_path,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--artifacts", type=Path, required=True)
    parser.add_argument(
        "--phase",
        choices=("release-baseline", "focused"),
        default="release-baseline",
    )
    parser.add_argument("--tests-only-patch", default="")
    parser.add_argument("--candidate-patch", default="")
    parser.add_argument(
        "--candidate-manifest",
        default="patches/rocksdb-prefix-15439/candidate-manifest.json",
    )
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
        "phase": args.phase,
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
            "debug_level": 0 if args.phase == "release-baseline" else 1,
            "helper_compile_flags": {
                "release": list(RELEASE_HELPER_COMPILE_FLAGS),
                "debug": list(DEBUG_HELPER_COMPILE_FLAGS),
            },
            "optional_features": DISABLED_OPTIONAL_FEATURES,
            "enabled_required_features": ENABLED_REQUIRED_FEATURES,
        },
        "helper_file_hashes": record_helper_hashes(),
        "block_fixture_spec": {
            "restart_interval": {
                "value_tag_overlap_restart_boundary": 2,
                "delete_tag_overlap": 2,
                "empty_user_full_footer_overlap": 16,
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
                "empty_user_full_footer_overlap": [
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
        receipt["source_before_phase"] = baseline_source
        receipt["system"] = capture_runtime_metadata(source)
        env = make_env()
        output_dir.mkdir(parents=True, exist_ok=True)
        if args.phase == "release-baseline":
            if args.tests_only_patch or args.candidate_patch:
                raise RuntimeError(
                    "release-baseline never applies tests-only or production patches"
                )
            run_release_baseline_phase(
                receipt, source, artifact_dir, log_dir, output_dir, env
            )
            receipt["tests_only_baseline"] = {"status": "not_requested"}
            receipt["candidate"] = {"status": "not_requested"}
            receipt["status"] = "baseline_reproduction_confirmed"
        else:
            if not args.tests_only_patch or not args.candidate_patch:
                raise RuntimeError(
                    "focused phase requires both frozen tests-only and candidate patches"
                )
            tests_patch = resolve_helper_path(args.tests_only_patch)
            candidate_patch = resolve_helper_path(args.candidate_patch)
            manifest_path = resolve_helper_path(args.candidate_manifest)
            receipt["release_baseline_reference"] = os.environ.get(
                "ROCKSDB_RELEASE_BASELINE_RUN_URL"
            )
            run_focused_candidate_phase(
                receipt,
                source,
                artifact_dir,
                log_dir,
                output_dir,
                env,
                tests_patch,
                candidate_patch,
                manifest_path,
            )
            receipt["status"] = "focused_candidate_checks_pass"

        additional_paths = set(receipt.get("candidate", {}).get("expected_paths", []))
        additional_paths.update(
            receipt.get("tests_only_baseline", {}).get("expected_paths", [])
        )
        receipt["source_after"] = source_snapshot(source, additional_paths)
    except Exception as error:  # Keep a structured receipt for every failed phase.
        receipt["errors"].append(f"{type(error).__name__}: {error}")
        receipt["status"] = "validation_failed"
        try:
            additional_paths = set(receipt.get("candidate", {}).get("expected_paths", []))
            additional_paths.update(
                receipt.get("tests_only_baseline", {}).get("expected_paths", [])
            )
            receipt["source_after_failure"] = source_snapshot(source, additional_paths)
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
                "phase": receipt.get("phase"),
                "baseline": receipt.get("baseline", {}).get("status"),
                "candidate": receipt.get("candidate", {}).get("status"),
                "errors": receipt["errors"],
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0 if receipt["status"] in {
        "baseline_reproduction_confirmed",
        "focused_candidate_checks_pass",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
