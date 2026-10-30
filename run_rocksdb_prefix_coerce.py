#!/usr/bin/env python3
"""Repeat the four RocksDB #15439 regressions with coerced context switches."""

from __future__ import annotations

import argparse
import datetime as dt
import gzip
import hashlib
import json
import os
import re
import shutil
from pathlib import Path

import run_rocksdb_prefix as shared


SOURCE_PIN = "87d442749a6a9f0fe5b9f07de7346466f8dd5acf"
CANDIDATE_SHA256 = "cac9fade4b4bbb5c11fc0884e7febdd512734fe8c0a82d4678ab0a360b07cbd5"
EXPECTED_PATHS = set(shared.EXPECTED_CANDIDATE_PATHS)
BLOCK_CASES = sorted(shared.EXPECTED_BLOCK_GTESTS)
DATABASE_CASES = [shared.FOCUSED_GTEST]
EXPECTED_CASES = BLOCK_CASES + DATABASE_CASES
REPEAT_COUNT = 100
WORKERS = 2
PER_TEST_TIMEOUT_SECONDS = 60
RUN_TIMEOUT_SECONDS = 10 * 60
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "rocksdb-prefix-15439"
FIXTURE_SOURCE_RUN = "https://github.com/xgbtxy/contribution-checks/actions/runs/41003363684"
FIXTURE_SOURCE_ARTIFACT_ID = 12955321158
FIXTURE_SOURCE_RECEIPT_SHA256 = "0ea532b35df50ee8ee28a40c63aef26433c9a0d1807bb7325d8b1d98f109ce02"
EXPECTED_SST_COUNTS = {"update": 2, "delete": 2, "new": 1}
EXPECTED_DB_METADATA = {"CURRENT", "IDENTITY", "MANIFEST-000005", "OPTIONS-000007"}


def utc() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_fixture_manifest() -> tuple[dict, dict]:
    manifest_path = FIXTURE_DIR / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    provenance = {
        "source_commit": SOURCE_PIN,
        "source_run": FIXTURE_SOURCE_RUN,
        "source_artifact_id": FIXTURE_SOURCE_ARTIFACT_ID,
        "source_receipt_sha256": FIXTURE_SOURCE_RECEIPT_SHA256,
    }
    if any(manifest.get(key) != value for key, value in provenance.items()):
        raise RuntimeError("old-SST fixture manifest provenance mismatch")
    if set(manifest.get("scenarios", {})) != set(EXPECTED_SST_COUNTS):
        raise RuntimeError("old-SST fixture manifest does not contain exactly three scenarios")

    sources, byte_total, sst_total = {}, 0, 0
    for mode, expected_ssts in EXPECTED_SST_COUNTS.items():
        directory = FIXTURE_DIR / mode
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError(f"old-SST fixture directory is missing or unsafe: {mode}")
        declared = manifest["scenarios"][mode].get("files", [])
        if any(Path(item.get("path", "")).name != item.get("path") for item in declared):
            raise RuntimeError(f"unsafe fixture file name in {mode}")
        if any((directory / item["path"]).is_symlink() for item in declared):
            raise RuntimeError(f"old-SST fixture must contain regular files: {mode}")
        expected = sorted(
            [{"path": item["path"], "bytes": item["bytes"], "sha256": item["sha256"]}
             for item in declared], key=lambda item: item["path"]
        )
        actual = shared.old_sst_inventory(directory)
        if actual != expected:
            raise RuntimeError(f"old-SST fixture bytes differ from manifest: {mode}")
        names = {item["path"] for item in actual}
        sst_count = sum(name.endswith((".sst", ".ldb")) for name in names)
        if sst_count != expected_ssts or sum(name.endswith(".log") for name in names) != 1:
            raise RuntimeError(f"unexpected SST/WAL count in old-SST fixture: {mode}")
        if not EXPECTED_DB_METADATA.issubset(names) or names != (
            {name for name in names if name.endswith((".sst", ".ldb", ".log"))}
            | EXPECTED_DB_METADATA
        ):
            raise RuntimeError(f"unexpected metadata files in old-SST fixture: {mode}")
        sources[mode] = actual
        byte_total += sum(item["bytes"] for item in actual)
        sst_total += sst_count
    if sst_total != 5:
        raise RuntimeError(f"expected five source SSTs, found {sst_total}")
    return manifest, {"manifest_sha256": sha256(manifest_path), "source_files": sources,
                      "source_file_bytes": byte_total, "source_sst_count": sst_total}


def copy_fixture_databases(artifact_dir: Path, source_inventory: dict) -> dict:
    artifact_root = artifact_dir / "old-sst"
    artifact_root.mkdir(parents=True, exist_ok=True)
    inventory = {}
    for mode in EXPECTED_SST_COUNTS:
        destination = artifact_root / f"baseline-v8-{mode}"
        if destination.exists():
            raise RuntimeError(f"old-SST copy destination already exists: {destination}")
        shutil.copytree(FIXTURE_DIR / mode, destination)
        copied = shared.old_sst_inventory(destination)
        if copied != source_inventory["source_files"][mode]:
            raise RuntimeError(f"copied old-SST fixture differs from verified manifest: {mode}")
        inventory[mode] = {
            "database": str(destination.relative_to(artifact_dir)),
            "files_before_candidate_read": copied,
        }
    return inventory


def coerce_compile_evidence(artifacts: Path, receipt: dict) -> dict:
    """Check verbose C++ compile commands, since Make config omits this flag."""
    build = receipt.get("commands", {}).get("coerce-debug-targets")
    if not build or not build.get("ok"):
        raise RuntimeError("coerce debug-build command receipt is missing or failed")
    log = artifacts / build["stdout_log_gzip"]
    required_sources = (
        "db/db_basic_test.cc",
        "table/block_based/block_test.cc",
        "table/block_based/block_builder.cc",
        "table/block_based/block.cc",
    )
    found = {name: [] for name in required_sources}
    all_coerced_commands = []
    with gzip.open(log, "rt", encoding="utf-8", errors="replace") as stream:
        pending = ""
        for line in stream:
            pending += line.rstrip("\r\n").rstrip("\\") + " "
            if line.rstrip().endswith("\\"):
                continue
            command = pending.replace("\\", "/")
            pending = ""
            if "-DCOERCE_CONTEXT_SWITCH" not in command or not re.search(r"(?:^|\s)-c(?:\s|$)", command):
                continue
            all_coerced_commands.append(command.strip())
            for source in required_sources:
                if source in command:
                    found[source].append(command.strip())
    missing = [source for source, commands in found.items() if not commands]
    if missing:
        raise RuntimeError(
            "verbose build output has no -DCOERCE_CONTEXT_SWITCH compile command for "
            f"{missing}; make_config.mk does not record this Makefile flag"
        )
    compiler_commands = sorted(all_coerced_commands)
    signature_material = {
        "source_pin": SOURCE_PIN,
        "candidate_patch_sha256": CANDIDATE_SHA256,
        "make_config_sha256": receipt["make_config"]["sha256"],
        "compile_commands": compiler_commands,
    }
    signature = hashlib.sha256(
        json.dumps(signature_material, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return {
        "coerce_macro_observed_in_compile_commands": True,
        "required_source_command_counts": {source: len(commands) for source, commands in found.items()},
        "coerced_compile_command_count": len(compiler_commands),
        "sample_compile_commands": [commands[0][:1600] for commands in found.values()],
        "compile_command_set_sha256": hashlib.sha256("\n".join(compiler_commands).encode("utf-8")).hexdigest(),
        "coerce_evidence_sha256": signature,
        "make_config_sha256": receipt["make_config"]["sha256"],
    }


def verify_fixture_sources_unchanged(original: dict) -> dict:
    _, after = verify_fixture_manifest()
    if after != original:
        raise RuntimeError("checked-in old-SST fixture source bytes changed during validation")
    return after


def verify_and_apply_candidate(
    source: Path, patch: Path, manifest_path: Path, log_dir: Path,
    receipt: dict, env: dict[str, str]
) -> dict:
    pristine = shared.verify_source_pin(source)
    if pristine["head"] != SOURCE_PIN:
        raise RuntimeError("RocksDB source checkout does not match the reviewed pin")
    if sha256(patch) != CANDIDATE_SHA256:
        raise RuntimeError("candidate patch hash differs from the frozen review")
    patch = shared.resolve_helper_path(str(patch))
    tests_patch = shared.ROOT / "patches/rocksdb-prefix-15439/TESTS_ONLY.patch"
    manifest = shared.verify_candidate_manifest(source, manifest_path, tests_patch, patch, EXPECTED_PATHS)
    paths, patch_info = shared.apply_review_patch(
        receipt, source, log_dir, env, patch, "coerce-candidate", EXPECTED_PATHS
    )
    blob_hashes = shared.verify_candidate_blobs(source, manifest, paths)
    return {"source": pristine, "manifest": manifest, "patch_paths": sorted(paths),
            "blobs": blob_hashes, "patch_application": patch_info}


def verify_candidate_still_frozen(source: Path, manifest: dict) -> dict:
    head = shared.git(source, "rev-parse", "HEAD")
    if head != SOURCE_PIN:
        raise RuntimeError(f"RocksDB HEAD changed during validation: {head}")
    status_paths = shared.source_status_paths(
        shared.git(source, "status", "--porcelain=v1", "--untracked-files=normal")
    )
    if status_paths != EXPECTED_PATHS:
        raise RuntimeError(f"working tree paths changed during validation: {sorted(status_paths)}")
    blobs = shared.verify_candidate_blobs(source, manifest, EXPECTED_PATHS)
    return {"head": head, "status_paths": sorted(status_paths), "candidate_blobs": blobs}


def validate_json_results(path: Path) -> dict:
    if not path.is_file():
        raise RuntimeError("gtest-parallel did not write its JSON results file")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("version") != 3:
        raise RuntimeError(f"unexpected gtest-parallel JSON result version: {payload.get('version')}")
    if payload.get("interrupted") is not False:
        raise RuntimeError("gtest-parallel marked the repeated test run interrupted")
    counts = payload.get("num_failures_by_type", {})
    expected_total = len(EXPECTED_CASES) * REPEAT_COUNT
    if counts.get("PASS") != expected_total or counts.get("FAIL") != 0 or counts.get("TIMEOUT") != 0:
        raise RuntimeError(f"expected {expected_total} PASS results and no failures/timeouts; got {counts}")
    tests = payload.get("tests", {})
    case_results = {}
    for case in EXPECTED_CASES:
        suite, name = case.split(".", 1)
        entry = tests.get(suite, {}).get(name)
        if not isinstance(entry, dict):
            raise RuntimeError(f"gtest-parallel results omit selected case {case}")
        statuses = entry.get("actual", "").split()
        runtimes = entry.get("times", [])
        if len(statuses) != REPEAT_COUNT or set(statuses) != {"PASS"}:
            raise RuntimeError(f"{case} did not pass exactly {REPEAT_COUNT} times")
        if len(runtimes) != REPEAT_COUNT:
            raise RuntimeError(f"{case} has {len(runtimes)} runtime samples, expected {REPEAT_COUNT}")
        case_results[case] = {"pass_count": len(statuses), "runtime_samples": len(runtimes)}
    observed_cases = {
        f"{suite}.{name}"
        for suite, suite_tests in tests.items()
        for name in suite_tests
    }
    if observed_cases != set(EXPECTED_CASES):
        raise RuntimeError(f"gtest-parallel ran an unexpected test set: {sorted(observed_cases)}")
    return {
        "json_version": payload["version"],
        "counts": counts,
        "case_results": case_results,
        "total_named_pass_executions": expected_total,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--artifacts", required=True, type=Path)
    parser.add_argument("--candidate-patch", required=True, type=Path)
    parser.add_argument("--candidate-manifest", required=True, type=Path)
    args = parser.parse_args()
    source = args.source.resolve()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    receipt = {
        "schema_version": 1,
        "project": "https://github.com/meta/rocksdb",
        "issue": "https://github.com/meta/rocksdb/issues/15439",
        "source_pin": SOURCE_PIN,
        "candidate_patch_sha256": CANDIDATE_SHA256,
        "coerce_context_switch": 1,
        "debug_level": 1,
        "workers": WORKERS,
        "per_test_timeout_seconds": PER_TEST_TIMEOUT_SECONDS,
        "repeat_count_per_named_case": REPEAT_COUNT,
        "expected_cases": EXPECTED_CASES,
        "recorded_utc": utc(),
        "workflow": {
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "ref": os.environ.get("GITHUB_REF"),
            "helper_sha": os.environ.get("GITHUB_SHA"),
        },
    }
    shared.write_json(artifacts / "receipt.json", receipt)
    original_fixture_snapshot = None
    try:
        receipt["runtime_metadata"] = shared.capture_runtime_metadata(source)
        env = shared.make_env()
        env.pop("ROCKSDB_DISABLE_SNAPPY", None)
        env["AUTO_CLEAN"] = "1"
        env["DEBUG_LEVEL"] = "1"
        env["NCORES"] = str(WORKERS)
        env["COERCE_CONTEXT_SWITCH"] = "1"
        env["V"] = "1"
        log_dir = artifacts / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        candidate = verify_and_apply_candidate(
            source, args.candidate_patch.resolve(), args.candidate_manifest.resolve(),
            log_dir, receipt, env
        )
        manifest = candidate["manifest"]
        receipt["candidate"] = {key: value for key, value in candidate.items() if key != "manifest"}

        shared.write_json(artifacts / "receipt.json", receipt)

        debug_env = shared.build_debug_targets(
            receipt, source, log_dir, env, "coerce-debug-targets"
        )
        make_config = shared.make_config_facts(source)
        receipt["make_config"] = make_config
        receipt["coerce_build_environment"] = {
            "COERCE_CONTEXT_SWITCH": env["COERCE_CONTEXT_SWITCH"],
            "DEBUG_LEVEL": env["DEBUG_LEVEL"],
            "V": env["V"],
        }
        receipt["coerce_compile_evidence"] = coerce_compile_evidence(artifacts, receipt)
        receipt["post_build_source"] = verify_candidate_still_frozen(source, manifest)

        fixture_manifest, original_fixture_snapshot = verify_fixture_manifest()
        receipt["old_sst_fixture_source"] = {
            **original_fixture_snapshot,
            "source_commit": fixture_manifest["source_commit"],
            "source_run": fixture_manifest["source_run"],
            "source_artifact_id": fixture_manifest["source_artifact_id"],
            "source_receipt_sha256": fixture_manifest["source_receipt_sha256"],
        }
        receipt["old_sst"] = copy_fixture_databases(artifacts, fixture_manifest)
        old_sst_helper = shared.ROOT / "rocksdb_prefix_old_sst.cc"
        receipt["old_sst_reader_helper_source_sha256"] = sha256(old_sst_helper)
        (artifacts / "bin-temp").mkdir(parents=True, exist_ok=True)
        compatibility = shared.compile_program(
            receipt,
            source,
            artifacts / "bin-temp",
            log_dir,
            debug_env,
            "rocksdb-prefix-debug-candidate-old-sst",
            shared.ROOT / "rocksdb_prefix_old_sst.cc",
            debug_level=1,
        )
        receipt["old_sst_reader_helper_binary_sha256"] = sha256(compatibility)
        shared.run_candidate_compatibility(
            receipt, compatibility, source, artifacts, log_dir, debug_env
        )
        for mode in EXPECTED_SST_COUNTS:
            command_record = receipt["commands"][f"candidate-read-old-sst-{mode}"]
            stdout = "\n".join(command_record.get("stdout_tail", []))
            required = [f"OLD_SST_READ mode={mode}", "seek=PASS", "result=PASS"]
            if mode == "delete":
                required.append("seek_key_hex=<end>")
            if any(marker not in stdout for marker in required):
                raise RuntimeError(f"old-SST {mode} read log lacks strict seek evidence: {required}")
        receipt["old_sst_strict_seek_evidence"] = {
            mode: receipt["commands"][f"candidate-read-old-sst-{mode}"]["stdout_tail"]
            for mode in EXPECTED_SST_COUNTS
        }

        result_path = artifacts / "gtest-parallel-results.json"
        output_dir = artifacts / "gtest-output"
        if output_dir.exists():
            raise RuntimeError("gtest output directory already exists; use a fresh artifact path")
        output_dir.mkdir(parents=True)
        filter_value = ":".join(EXPECTED_CASES)
        command = [
            str(source / "build_tools/gtest-parallel"),
            "./block_test",
            "./db_basic_test",
            "--workers=2",
            "--timeout=600",
            "--timeout_per_test=60",
            "--serialize_test_cases",
            "--repeat=100",
            f"--gtest_filter={filter_value}",
            f"--dump_json_test_results={result_path}",
            f"--output_dir={output_dir}",
        ]
        run = shared.run_command(
            receipt,
            "coerce-repeat-100",
            command,
            source,
            log_dir,
            timeout=RUN_TIMEOUT_SECONDS,
            env=debug_env,
        )
        result_evidence = {
            "command": command,
            "exit_code": run["exit_code"],
            "timed_out": run["timed_out"],
            "stdout_gzip": run["stdout_log_gzip"],
            "stderr_gzip": run["stderr_log_gzip"],
            "stdout_tail": run["stdout_tail"],
            "stderr_tail": run["stderr_tail"],
        }
        receipt["run"] = result_evidence
        if result_path.is_file():
            try:
                partial = json.loads(result_path.read_text(encoding="utf-8"))
                receipt["observed_test_results"] = {
                    "sha256": sha256(result_path),
                    "num_failures_by_type": partial.get("num_failures_by_type"),
                    "test_cases": {
                        f"{suite}.{name}": {
                            "runs": len(entry.get("times", [])),
                            "actual": entry.get("actual"),
                        }
                        for suite, cases in partial.get("tests", {}).items()
                        for name, entry in cases.items()
                    },
                }
            except (json.JSONDecodeError, OSError) as error:
                receipt["test_results_parse_error"] = str(error)
        if not run["ok"]:
            raise RuntimeError("repeat-100 gtest-parallel command failed or timed out")
        evidence = validate_json_results(result_path)
        receipt["test_evidence"] = evidence
        receipt["test_results_json_sha256"] = sha256(result_path)
        receipt["post_test_source"] = verify_candidate_still_frozen(source, manifest)
        receipt["status"] = "400_named_pass_executions_verified"
    except Exception as error:
        receipt.setdefault("errors", []).append(f"{type(error).__name__}: {error}")
        receipt["status"] = "validation_failed"
        try:
            receipt["source_after_failure"] = {
                "head": shared.git(source, "rev-parse", "HEAD"),
                "status_paths": sorted(
                    shared.source_status_paths(shared.git(source, "status", "--porcelain=v1"))
                ),
            }
        except Exception as snapshot_error:
            receipt["errors"].append(
                f"source snapshot failed: {type(snapshot_error).__name__}: {snapshot_error}"
            )
    finally:
        if original_fixture_snapshot is not None:
            try:
                receipt["old_sst_fixture_sources_after_test"] = verify_fixture_sources_unchanged(
                    original_fixture_snapshot
                )
            except Exception as fixture_error:
                receipt.setdefault("errors", []).append(
                    f"fixture source changed: {type(fixture_error).__name__}: {fixture_error}"
                )
                receipt["status"] = "validation_failed"
        receipt["finished_utc"] = utc()
        shared.write_json(artifacts / "receipt.json", receipt)
    print(json.dumps({"status": receipt["status"], "test_evidence": receipt.get("test_evidence"), "errors": receipt.get("errors", [])}, sort_keys=True))
    return 0 if receipt["status"] == "400_named_pass_executions_verified" else 1


if __name__ == "__main__":
    raise SystemExit(main())
