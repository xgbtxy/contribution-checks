#!/usr/bin/env python3
"""Validate HDF5 h5diff unsigned-threshold behavior on Linux LP64."""

from __future__ import annotations

import argparse
import ctypes
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time
import xml.etree.ElementTree as ET


MANIFEST_NAME = "hdf5-unsigned-manifest.json"


def git_text(source: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=source, text=True, encoding="utf-8").strip()


def git_blob(source: Path, relative: str) -> str:
    return git_text(source, "hash-object", relative)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def record_artifact_metadata(artifacts: Path, receipt: dict[str, object]) -> None:
    receipt["finished_at"] = datetime.now(timezone.utc).isoformat()
    artifacts.mkdir(parents=True, exist_ok=True)
    (artifacts / "receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


def run_command(
    label: str,
    command: list[str],
    *,
    cwd: Path,
    artifacts: Path,
    receipt: dict[str, object],
    timeout: int,
    expected: set[int] = {0},
) -> subprocess.CompletedProcess[str]:
    started = datetime.now(timezone.utc).isoformat()
    print(f"[hdf5-unsigned] {label}: start {started}", flush=True)
    timed_out = False
    start_time = time.monotonic()
    process = subprocess.Popen(
        command,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(process.pid, signal.SIGTERM)
        try:
            output, _ = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            output, _ = process.communicate()
    completed = subprocess.CompletedProcess(
        command, 124 if timed_out else process.returncode, stdout=output, stderr=None
    )
    elapsed = round(time.monotonic() - start_time, 3)
    (artifacts / f"{label}.log").write_text(completed.stdout or "", encoding="utf-8")
    commands = receipt.setdefault("commands", [])
    assert isinstance(commands, list)
    commands.append(
        {
            "label": label,
            "argv": command,
            "cwd": str(cwd),
            "started_at": started,
            "finished_at": datetime.now(timezone.utc).isoformat(),
            "elapsed_seconds": elapsed,
            "timeout_seconds": timeout,
            "timed_out": timed_out,
            "exit_code": completed.returncode,
            "expected_exit_codes": sorted(expected),
            "result_matches_expectation": not timed_out and completed.returncode in expected,
            "log": f"{label}.log",
            "log_sha256": hashlib.sha256((completed.stdout or "").encode("utf-8")).hexdigest(),
        }
    )
    (artifacts / "receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    if timed_out or completed.returncode not in expected:
        tail = "\n".join((completed.stdout or "").splitlines()[-60:])
        raise RuntimeError(
            f"{label} returned {completed.returncode} (expected {sorted(expected)}); "
            f"see {artifacts / (label + '.log')}\n{tail}"
        )
    return completed


def run_ctest(
    *,
    label: str,
    build: Path,
    source: Path,
    artifacts: Path,
    receipt: dict[str, object],
    expected_exit: int,
    timeout: int,
) -> dict[str, object]:
    junit = artifacts / f"{label}.xml"
    command = [
        "ctest",
        "--test-dir",
        str(build),
        "-C",
        "Release",
        "-R",
        r"^H5DIFF-" if label == "full_h5diff" else r"^H5DIFF-h5diff_unsigned_",
        "--output-on-failure",
        "--output-junit",
        str(junit),
        "-j",
        "2",
        "--timeout",
        str(timeout),
    ]
    completed = run_command(
        label,
        command,
        cwd=source,
        artifacts=artifacts,
        receipt=receipt,
        timeout=timeout + 60,
        expected={expected_exit},
    )
    if not junit.is_file():
        raise RuntimeError(f"{label} did not produce its JUnit file")
    root = ET.parse(junit).getroot()
    cases = list(root.iter("testcase"))
    skipped = [case.attrib.get("name", "") for case in cases if case.find("skipped") is not None]
    failed = [
        case.attrib.get("name", "")
        for case in cases
        if case.find("failure") is not None or case.find("error") is not None
    ]
    case_names = {case.attrib.get("name", "") for case in cases}
    expected_names = {
        *receipt["baseline_test_cases"]["must_fail"],
        *receipt["baseline_test_cases"]["must_pass"],
    }
    full_suite = label == "full_h5diff"
    if len(case_names) != len(cases):
        raise RuntimeError(f"{label} has duplicate test case names")
    if (full_suite and (not expected_names.issubset(case_names) or len(cases) <= len(expected_names))) or (
        not full_suite and case_names != expected_names
    ):
        raise RuntimeError(
            f"{label} test set differs from the manifest; actual={sorted(case_names)}"
        )
    if skipped:
        raise RuntimeError(f"{label} skipped unsigned regression cases: {skipped}")
    case_result = {
        "label": label,
        "junit": f"{label}.xml",
        "junit_sha256": sha256(junit),
        "tests": len(cases),
        "passed": len(cases) - len(failed),
        "skipped": skipped,
        "failed_names": failed,
        "expected_exit_code": expected_exit,
        "ctest_exit_code": completed.returncode,
    }
    if label == "baseline":
        expected_failures = set(receipt["baseline_test_cases"]["must_fail"])
        if set(failed) != expected_failures:
            raise RuntimeError(
                f"baseline failures differ; expected={sorted(expected_failures)}, actual={sorted(failed)}"
            )
    elif failed:
        raise RuntimeError(f"candidate unsigned regression failures: {failed}")
    results = receipt.setdefault("ctest", [])
    assert isinstance(results, list)
    results.append(case_result)
    (artifacts / "receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return case_result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, default=Path(MANIFEST_NAME))
    parser.add_argument("--artifacts", type=Path, required=True)
    args = parser.parse_args()

    source = args.source.resolve()
    manifest_path = args.manifest.resolve()
    artifacts = args.artifacts.resolve()
    artifacts.mkdir(parents=True, exist_ok=True)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected_head = manifest["source_head"]
    expected_parent = manifest["source_parent"]
    actual_head = git_text(source, "rev-parse", "HEAD")
    actual_parent = git_text(source, "rev-parse", "HEAD^")
    receipt: dict[str, object] = {
        "status": "started",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "workflow_run_id": os.environ.get("GITHUB_RUN_ID"),
        "workflow_run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        "workflow_repository": os.environ.get("GITHUB_REPOSITORY"),
        "workflow_sha": os.environ.get("GITHUB_SHA"),
        "source": str(source),
        "source_repository": manifest["repository"],
        "upstream_repository": manifest["upstream_repository"],
        "pull_request": manifest["pull_request"],
        "source_head": actual_head,
        "source_parent": actual_parent,
        "expected_head": expected_head,
        "expected_parent": expected_parent,
        "manifest": str(manifest_path),
        "manifest_sha256": sha256(manifest_path),
        "python": sys.version,
        "platform": platform.platform(),
        "architecture": platform.machine(),
        "ctypes_sizes": {
            "unsigned_int": ctypes.sizeof(ctypes.c_uint),
            "unsigned_long": ctypes.sizeof(ctypes.c_ulong),
            "unsigned_long_long": ctypes.sizeof(ctypes.c_ulonglong),
            "pointer": ctypes.sizeof(ctypes.c_void_p),
        },
        "changed_blobs_expected": manifest["changed_blobs"],
        "changed_blobs_actual": {},
        "baseline_test_cases": manifest["baseline_test_cases"],
        "commands": [],
        "ctest": [],
        "source_worktree_clean": None,
        "product_changes": False,
        "restoration_verified": False,
    }
    (artifacts / "receipt.json").write_text(
        json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    candidate_source: bytes | None = None
    candidate_blob: str | None = None
    production_file = "tools/lib/h5diff_array.c"
    status = "failed"
    try:
        if actual_head != expected_head or actual_parent != expected_parent:
            raise RuntimeError(
                f"source revision mismatch: HEAD={actual_head}, parent={actual_parent}"
            )
        actual_blobs = {path: git_blob(source, path) for path in manifest["changed_blobs"]}
        receipt["changed_blobs_actual"] = actual_blobs
        for path, expected_blob in manifest["changed_blobs"].items():
            if actual_blobs[path] != expected_blob:
                raise RuntimeError(f"source blob mismatch: {path}")
        if receipt["ctypes_sizes"] != {
            "unsigned_int": 4,
            "unsigned_long": 8,
            "unsigned_long_long": 8,
            "pointer": 8,
        }:
            raise RuntimeError(f"runner is not the expected LP64 ABI: {receipt['ctypes_sizes']}")
        production_path = source / production_file
        candidate_source = production_path.read_bytes()
        candidate_blob = git_blob(source, production_file)
        if candidate_blob != manifest["changed_blobs"][production_file]:
            raise RuntimeError(f"candidate source blob mismatch: {candidate_blob}")
        baseline_source = subprocess.check_output(
            ["git", "show", f"{actual_parent}:{production_file}"], cwd=source
        )
        baseline_blob = subprocess.run(
            ["git", "hash-object", "--stdin"],
            input=baseline_source,
            cwd=source,
            stdout=subprocess.PIPE,
            check=True,
        ).stdout.decode("ascii").strip()
        expected_baseline_blob = manifest["production_only_blobs"][production_file]
        if baseline_blob != expected_baseline_blob:
            raise RuntimeError(f"unexpected baseline production blob: {baseline_blob}")
        build = artifacts.parent / "build"
        receipt.update(
            {
                "status": "checking_baseline",
                "candidate_production_blob": candidate_blob,
                "baseline_production_blob": baseline_blob,
                "build": str(build),
                "build_configuration": [
                    "Ninja",
                    "Release",
                    "BUILD_SHARED_LIBS=OFF",
                    "HDF5_BUILD_STATIC_TOOLS=ON",
                    "HDF5_BUILD_EXAMPLES=OFF",
                    "Default HDF5 compression/plugin settings",
                ],
            }
        )
        (artifacts / "receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        production_path.write_bytes(baseline_source)
        if git_blob(source, production_file) != expected_baseline_blob:
            raise RuntimeError("baseline production source was not installed before compilation")
        cmake = [
            "cmake",
            "-S",
            str(source),
            "-B",
            str(build),
            "-G",
            "Ninja",
            "-DCMAKE_BUILD_TYPE=Release",
            "-DBUILD_SHARED_LIBS=OFF",
            "-DHDF5_BUILD_STATIC_TOOLS=ON",
            "-DHDF5_BUILD_EXAMPLES=OFF",
        ]
        run_command(
            "configure",
            cmake,
            cwd=source,
            artifacts=artifacts,
            receipt=receipt,
            timeout=manifest["runner"]["limits"]["configure_seconds"],
        )
        run_command(
            "build_baseline",
            ["cmake", "--build", str(build), "--target", "h5diff", "h5diff_files", "-j2"],
            cwd=source,
            artifacts=artifacts,
            receipt=receipt,
            timeout=manifest["runner"]["limits"]["baseline_build_seconds"],
        )
        if git_blob(source, production_file) != expected_baseline_blob:
            raise RuntimeError("baseline production source changed during compilation")
        receipt["baseline_h5diff_sha256"] = sha256(build / "bin" / "h5diff")
        run_ctest(
            label="baseline",
            build=build,
            source=source,
            artifacts=artifacts,
            receipt=receipt,
            expected_exit=8,
            timeout=manifest["runner"]["limits"]["ctest_seconds"],
        )

        receipt["status"] = "restoring_candidate"
        (artifacts / "receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        production_path.write_bytes(candidate_source)
        if git_blob(source, production_file) != candidate_blob:
            raise RuntimeError("candidate production source did not restore exactly")
        receipt["status"] = "building_candidate"
        (artifacts / "receipt.json").write_text(
            json.dumps(receipt, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        run_command(
            "build_candidate",
            ["cmake", "--build", str(build), "--target", "h5diff", "h5diff_files", "-j2"],
            cwd=source,
            artifacts=artifacts,
            receipt=receipt,
            timeout=manifest["runner"]["limits"]["candidate_build_seconds"],
        )
        run_ctest(
            label="candidate_unsigned",
            build=build,
            source=source,
            artifacts=artifacts,
            receipt=receipt,
            expected_exit=0,
            timeout=manifest["runner"]["limits"]["ctest_seconds"],
        )
        run_ctest(
            label="full_h5diff",
            build=build,
            source=source,
            artifacts=artifacts,
            receipt=receipt,
            expected_exit=0,
            timeout=manifest["runner"]["limits"]["ctest_seconds"],
        )
        receipt["candidate_h5diff_sha256"] = sha256(build / "bin" / "h5diff")
        receipt["source_worktree_clean"] = not git_text(source, "status", "--porcelain")
        if not receipt["source_worktree_clean"]:
            raise RuntimeError("validation altered the source worktree")
        receipt["status"] = "passed"
        status = "passed"
    except Exception as exc:
        receipt["status"] = status
        receipt["error"] = repr(exc)
        print(f"[hdf5-unsigned] validation failed: {exc}", flush=True)
        return_code = 1
    else:
        return_code = 0
    finally:
        if candidate_source is not None and candidate_blob is not None:
            candidate_path = source / production_file
            candidate_path.write_bytes(candidate_source)
            receipt["restoration_verified"] = git_blob(source, production_file) == candidate_blob
            if not receipt["restoration_verified"]:
                receipt["status"] = "failed"
                receipt["error"] = "candidate production blob failed final restoration check"
                return_code = 1
        record_artifact_metadata(artifacts, receipt)
    print(
        json.dumps(
            {
                "status": receipt.get("status"),
                "ctest": receipt.get("ctest", []),
                "artifacts": str(artifacts),
            },
            sort_keys=True,
        ),
        flush=True,
    )
    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
