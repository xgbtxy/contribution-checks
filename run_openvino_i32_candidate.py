"""Validate the reviewed OpenVINO patch in original-red then candidate-green order."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parent
PATCH_ROOT = ROOT / "patches" / "openvino-i32"
UPSTREAM_LFS_ATTRIBUTE_MISMATCHES = {
    "docs/articles_en/physical-ai/assets/physicalai.png",
    "docs/articles_en/physical-ai/assets/physicalai.svg",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(command: list[str], cwd: Path, log: Path, timeout: int, env=None) -> subprocess.CompletedProcess:
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("w", encoding="utf-8", newline="\n") as output:
        output.write("$ " + subprocess.list2cmdline(command) + "\n")
        output.write(f"cwd: {cwd}\n\n")
        output.flush()
        try:
            completed = subprocess.run(
                command,
                cwd=cwd,
                env=env,
                text=True,
                encoding="utf-8",
                errors="replace",
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            tail = exc.stdout or ""
            if isinstance(tail, bytes):
                tail = tail.decode("utf-8", errors="replace")
            output.write(f"\nPROCESS TIMED OUT after {timeout} seconds\n{tail}")
            return subprocess.CompletedProcess(command, 124, stdout=tail)
        output.write(completed.stdout)
    for line in completed.stdout.splitlines()[-100:]:
        print(line)
    return completed


def git(source: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(source), *args],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    return result.stdout.rstrip("\r\n")


def record(path: Path, state: dict) -> None:
    state["updated_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def tracked_changes(source: Path) -> set[str]:
    lines = git(source, "status", "--porcelain=v1", "--untracked-files=all").splitlines()
    changed = set()
    for line in lines:
        relative = line[3:].replace("\\", "/")
        path = source / relative
        # Upstream stores these two assets as full blobs while declaring LFS.
        # Ignore a clean-filter-only report only when the raw bytes and mode are
        # still exactly the committed regular file. No source edits are waived.
        if line[:2] == " M" and relative in UPSTREAM_LFS_ATTRIBUTE_MISMATCHES:
            tree_entry = git(source, "ls-tree", "HEAD", "--", relative)
            if (tree_entry.startswith("100644 blob ") and path.is_file()
                    and not (path.stat().st_mode & 0o111)
                    and git(source, "hash-object", "--no-filters", str(path))
                    == git(source, "rev-parse", f"HEAD:{relative}")):
                continue
        changed.add(relative)
    return changed


def expected_hashes(source: Path, manifest: dict, applied_groups: set[str]) -> None:
    for relative, spec in manifest["files"].items():
        path = source / relative
        if spec["group"] in applied_groups:
            if not path.is_file() or path.stat().st_size != spec["candidate_bytes"]:
                raise RuntimeError(f"Candidate file missing or has wrong byte size: {relative}")
            actual = sha256(path)
            if actual != spec["candidate_sha256"]:
                raise RuntimeError(f"Candidate hash mismatch for {relative}: {actual}")
        elif spec["original_sha256"] is None:
            if path.exists():
                raise RuntimeError(f"Test-only stage unexpectedly contains candidate file {relative}")
        else:
            if not path.is_file() or sha256(path) != spec["original_sha256"]:
                raise RuntimeError(f"Original hash mismatch for {relative}")


def apply_patch(source: Path, name: str, manifest: dict, state: dict, artifacts: Path) -> None:
    patch = PATCH_ROOT / name
    expected_patch_hash = manifest["patches"][name]["sha256"]
    actual_patch_hash = sha256(patch)
    if actual_patch_hash != expected_patch_hash:
        raise RuntimeError(f"Patch SHA256 mismatch for {name}: {actual_patch_hash}")
    check = subprocess.run(
        ["git", "-C", str(source), "apply", "--check", str(patch)],
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    (artifacts / f"{name}.check.log").write_text(check.stdout, encoding="utf-8")
    if check.returncode:
        raise RuntimeError(f"git apply --check failed for {name}: {check.stdout}")
    subprocess.run(["git", "-C", str(source), "apply", str(patch)], check=True)
    state.setdefault("patches", {})[name] = {
        "sha256": actual_patch_hash,
        "paths": manifest["patches"][name]["paths"],
        "applied": True,
    }


def junit_counts(path: Path) -> dict[str, int]:
    if not path.is_file():
        raise RuntimeError(f"CTest did not create the expected JUnit report: {path}")
    tree = ET.parse(path).getroot()
    suites = [tree] if tree.tag == "testsuite" else list(tree.iter("testsuite"))
    return {
        "tests": sum(int(suite.attrib.get("tests", 0)) for suite in suites),
        "failures": sum(int(suite.attrib.get("failures", 0)) for suite in suites),
        "errors": sum(int(suite.attrib.get("errors", 0)) for suite in suites),
        "skipped": sum(int(suite.attrib.get("skipped", 0)) for suite in suites),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, default=ROOT / "openvino")
    parser.add_argument("--build", type=Path, default=ROOT / "openvino" / "build")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts" / "openvino-i32-candidate")
    parser.add_argument("--python", type=Path, default=ROOT / ".venv" / "bin" / "python")
    args = parser.parse_args()
    source, build, artifacts = args.source.resolve(), args.build.resolve(), args.artifacts.resolve()
    manifest_path = PATCH_ROOT / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("schema_version") != 1:
        raise RuntimeError("Unsupported patch manifest schema")
    if git(source, "rev-parse", "HEAD") != manifest["base_commit"]:
        raise RuntimeError("Source checkout is not at the manifest's pinned base commit")
    artifacts.mkdir(parents=True, exist_ok=True)
    raw_initial_status = git(source, "status", "--porcelain=v1", "--untracked-files=all")
    (artifacts / "initial-source-status.txt").write_text(raw_initial_status + "\n", encoding="utf-8")
    lfs_asset_receipt = {}
    for relative in UPSTREAM_LFS_ATTRIBUTE_MISMATCHES:
        path = source / relative
        lfs_asset_receipt[relative] = {
            "committed_blob": git(source, "rev-parse", f"HEAD:{relative}"),
            "raw_worktree_blob": git(source, "hash-object", "--no-filters", str(path)),
            "committed_tree_entry": git(source, "ls-tree", "HEAD", "--", relative),
            "worktree_mode": oct(path.stat().st_mode),
        }
    (artifacts / "upstream-lfs-asset-check.json").write_text(json.dumps(lfs_asset_receipt, indent=2), encoding="utf-8")
    if tracked_changes(source):
        raise RuntimeError(f"Source checkout contains actual changes: {sorted(tracked_changes(source))}")

    artifacts.mkdir(parents=True, exist_ok=True)
    state = {
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "harness_commit": git(ROOT, "rev-parse", "HEAD"),
        "source_repository": manifest["source_repository"],
        "source_commit": manifest["base_commit"],
        "manifest_sha256": sha256(manifest_path),
        "test_target": manifest["test_target"],
        "test_filter": manifest["test_filter"],
        "candidate_kind": manifest["candidate_kind"],
        "baseline": {"status": "not-run"},
        "candidate": {"status": "not-run"},
        "optional_python_probes": {"status": "not-run"},
    }
    receipt = artifacts / "candidate-red-green.json"
    record(receipt, state)

    test_paths = set(manifest["patches"]["tests.patch"]["paths"])
    production_paths = set(manifest["patches"]["production.patch"]["paths"])
    if test_paths & production_paths:
        raise RuntimeError("Production and test patch paths overlap")
    if set(manifest["files"]) != test_paths | production_paths:
        raise RuntimeError("Manifest file hashes do not cover exactly the two patch path sets")

    apply_patch(source, "tests.patch", manifest, state, artifacts)
    expected_hashes(source, manifest, {"tests"})
    if tracked_changes(source) != test_paths:
        raise RuntimeError("Tests-only phase changed an unexpected source path")
    state["baseline"]["source_hashes_verified"] = True
    state["baseline"]["changed_paths"] = sorted(tracked_changes(source))
    (artifacts / "base-submodules.txt").write_text(git(source, "submodule", "status") + "\n", encoding="utf-8")
    record(receipt, state)

    configure = [
        "cmake", "-S", str(source), "-B", str(build), "-G", "Ninja",
        "-DCMAKE_BUILD_TYPE=Release", "-DBUILD_SHARED_LIBS=ON",
        "-DCMAKE_C_COMPILER_LAUNCHER=ccache", "-DCMAKE_CXX_COMPILER_LAUNCHER=ccache",
        "-DENABLE_PLUGINS_XML=ON", "-DENABLE_INTEL_CPU=ON",
        "-DENABLE_INTEL_GPU=OFF", "-DENABLE_INTEL_NPU=OFF",
        "-DENABLE_MLAS_FOR_CPU=ON", "-DENABLE_KLEIDIAI_FOR_CPU=ON",
        "-DTHREADING=SEQ", "-DENABLE_SYSTEM_TBB=ON",
        "-DARM_COMPUTE_SCONS_JOBS=2", "-DENABLE_ARM_COMPUTE_CMAKE=OFF",
        "-DENABLE_PYTHON=ON", f"-DPython3_EXECUTABLE={args.python.resolve()}",
        "-DENABLE_WHEEL=OFF", "-DENABLE_PYTHON_PACKAGING=OFF",
        "-DENABLE_TESTS=ON", "-DENABLE_FUNCTIONAL_TESTS=ON",
        "-DENABLE_CPU_SPECIFIC_TARGET_PER_TEST=ON", "-DENABLE_SAMPLES=OFF",
        "-DENABLE_DOCS=OFF", "-DENABLE_NCC_STYLE=OFF", "-DENABLE_CLANG_FORMAT=OFF",
        "-DENABLE_PROFILING_ITT=OFF", "-DENABLE_SNAPPY_COMPRESSION=OFF",
        "-DENABLE_OV_IR_FRONTEND=OFF", "-DENABLE_OV_ONNX_FRONTEND=OFF",
        "-DENABLE_OV_PADDLE_FRONTEND=OFF", "-DENABLE_OV_TF_FRONTEND=OFF",
        "-DENABLE_OV_TF_LITE_FRONTEND=OFF", "-DENABLE_OV_PYTORCH_FRONTEND=OFF",
        "-DENABLE_OV_JAX_FRONTEND=OFF", "-DENABLE_OV_GGUF_FRONTEND=OFF",
        "-DENABLE_PROXY=OFF", "-DENABLE_MULTI=OFF", "-DENABLE_AUTO=OFF",
        "-DENABLE_AUTO_BATCH=OFF", "-DENABLE_HETERO=OFF", "-DENABLE_TEMPLATE=ON",
        "-DCMAKE_EXPORT_COMPILE_COMMANDS=ON",
    ]
    config = run(configure, ROOT, artifacts / "configure.log", timeout=1800)
    if config.returncode:
        raise RuntimeError(f"Candidate CMake configure failed: {config.returncode}")
    cache = build / "CMakeCache.txt"
    (artifacts / "CMakeCache.txt").write_bytes(cache.read_bytes())
    record(receipt, state)

    target = manifest["test_target"]
    build_test = ["cmake", "--build", str(build), "--target", target, "--parallel", "2"]
    compile_base = run(build_test, ROOT, artifacts / "baseline-build.log", timeout=150 * 60)
    if compile_base.returncode:
        state["baseline"]["status"] = "build-failed-not-a-red-test"
        state["baseline"]["build_exit_code"] = compile_base.returncode
        record(receipt, state)
        raise RuntimeError("The original source failed to compile the C++ regression target; this is not red evidence")
    state["baseline"]["build_exit_code"] = 0
    record(receipt, state)

    listing = run(
        ["ctest", "--test-dir", str(build), "--show-only=json-v1"],
        ROOT,
        artifacts / "ctest-list.json.log",
        timeout=120,
    )
    if listing.returncode:
        raise RuntimeError("CTest could not enumerate the official CPU per-test target")
    tests_json = json.loads(listing.stdout)
    selected = [item for item in tests_json.get("tests", []) if item.get("name") == target]
    if len(selected) != 1:
        raise RuntimeError(f"Expected exactly one CTest entry for {target}, found {len(selected)}")
    command = selected[0].get("command", [])
    if not command or not Path(command[0]).is_file():
        raise RuntimeError("CTest per-test registration exists but its compiled executable is missing")
    suite, _, test_name = manifest["test_filter"].partition(".")
    gtest_list = run(command + ["--gtest_list_tests"], ROOT, artifacts / "gtest-list.log", timeout=120)
    if gtest_list.returncode or suite not in gtest_list.stdout or (test_name != "*" and test_name not in gtest_list.stdout):
        raise RuntimeError("The C++ regression named by the manifest was not registered in the binary")
    state["baseline"]["ctest_entry"] = target
    state["baseline"]["gtest_filter_found"] = manifest["test_filter"]
    record(receipt, state)

    baseline_junit = artifacts / "baseline.junit.xml"
    baseline_run = run(
        ["ctest", "--test-dir", str(build), "--output-on-failure", "--output-junit", str(baseline_junit), "-R", f"^{re.escape(target)}$"],
        ROOT,
        artifacts / "baseline-test.log",
        timeout=15 * 60,
        env={**os.environ, "GTEST_COLOR": "no"},
    )
    baseline_counts = junit_counts(baseline_junit)
    baseline_text = baseline_run.stdout
    # This concrete pair is the issue's reported Add precision failure. A compile
    # error, unsupported test, exception, or unrelated assertion cannot establish red.
    expected_pair = re.search(r"33554432.{0,500}33554433|33554433.{0,500}33554432", baseline_text, re.DOTALL)
    if baseline_run.returncode == 0 or not expected_pair or baseline_counts["failures"] == 0:
        state["baseline"].update({
            "status": "failed-to-prove-numeric-red",
            "ctest_exit_code": baseline_run.returncode,
            "junit": baseline_counts,
            "issue_add_mismatch_in_log": bool(expected_pair),
        })
        record(receipt, state)
        raise RuntimeError("The unmodified source did not produce a confirmed numeric Add mismatch in the C++ test")
    state["baseline"].update({
        "status": "expected-red-numeric-regression-confirmed",
        "ctest_exit_code": baseline_run.returncode,
        "junit": baseline_counts,
        "issue_add_mismatch_in_log": True,
    })
    expected_hashes(source, manifest, {"tests"})
    state["baseline"]["verified_file_hashes_after_test"] = {
        path: sha256(source / path) for path in manifest["files"]
    }
    record(receipt, state)

    apply_patch(source, "production.patch", manifest, state, artifacts)
    expected_hashes(source, manifest, {"tests", "production"})
    if tracked_changes(source) != test_paths | production_paths:
        raise RuntimeError("Candidate phase changed an unexpected source path")
    state["candidate"]["source_hashes_verified"] = True
    state["candidate"]["changed_paths"] = sorted(tracked_changes(source))
    record(receipt, state)

    compile_candidate = run(build_test, ROOT, artifacts / "candidate-build.log", timeout=150 * 60)
    if compile_candidate.returncode:
        state["candidate"].update({"status": "build-failed", "build_exit_code": compile_candidate.returncode})
        record(receipt, state)
        raise RuntimeError("The candidate failed to rebuild the C++ CPU test target")
    candidate_junit = artifacts / "candidate.junit.xml"
    candidate_run = run(
        ["ctest", "--test-dir", str(build), "--output-on-failure", "--output-junit", str(candidate_junit), "-R", f"^{re.escape(target)}$"],
        ROOT,
        artifacts / "candidate-test.log",
        timeout=15 * 60,
        env={**os.environ, "GTEST_COLOR": "no"},
    )
    candidate_counts = junit_counts(candidate_junit)
    if candidate_run.returncode or candidate_counts["failures"] or candidate_counts["errors"] or not candidate_counts["tests"]:
        state["candidate"].update({"status": "failed", "ctest_exit_code": candidate_run.returncode, "junit": candidate_counts})
        record(receipt, state)
        raise RuntimeError("The candidate's exact CPU regression did not pass")
    state["candidate"].update({"status": "passed", "build_exit_code": 0, "ctest_exit_code": 0, "junit": candidate_counts})
    expected_hashes(source, manifest, {"tests", "production"})
    state["candidate"]["verified_file_hashes_after_test"] = {
        path: sha256(source / path) for path in manifest["files"]
    }
    record(receipt, state)

    existing_targets = [
        ("ov_cpu_func_subgraph_arm_fuse_eltwise_convert", "*", "fusion-convert"),
        ("ov_cpu_func_slt_eltwise", "*smoke_CompareWithRefs_4D_int_jit*", "mixed-precision-eltwise"),
    ]
    existing_build = run(
        ["cmake", "--build", str(build), "--target", *(item[0] for item in existing_targets), "--parallel", "2"],
        ROOT, artifacts / "existing-regressions-build.log", timeout=25 * 60,
    )
    if existing_build.returncode:
        state["existing_regressions"] = {"status": "build-failed", "exit_code": existing_build.returncode}
        record(receipt, state)
        raise RuntimeError("Existing ARM regression targets did not build")
    state["existing_regressions"] = []
    for existing_target, test_filter, label in existing_targets:
        gtest_xml = artifacts / f"{label}.gtest.xml"
        ctest_xml = artifacts / f"{label}.ctest.xml"
        existing_run = run(
            ["ctest", "--test-dir", str(build), "--output-on-failure", "--output-junit", str(ctest_xml),
             "-R", f"^{re.escape(existing_target)}$"],
            ROOT, artifacts / f"{label}.log", timeout=15 * 60,
            env={**os.environ, "GTEST_COLOR": "no", "GTEST_FILTER": test_filter, "GTEST_OUTPUT": f"xml:{gtest_xml}"},
        )
        counts = junit_counts(gtest_xml)
        entry = {"target": existing_target, "filter": test_filter, "exit_code": existing_run.returncode, "gtest_counts": counts}
        state["existing_regressions"].append(entry)
        record(receipt, state)
        if (existing_run.returncode or counts["failures"] or counts["errors"]
                or counts["tests"] <= counts["skipped"]):
            raise RuntimeError(f"Existing ARM regression failed or ran no non-skipped tests: {existing_target}")
    expected_hashes(source, manifest, {"tests", "production"})

    # The precise C++ functional test is the gating validation. The matrix probes
    # are supplementary and report outstanding operations operation by operation.
    python_target = run(
        ["cmake", "--build", str(build), "--target", "pyopenvino", "--parallel", "2"],
        ROOT,
        artifacts / "python-api-build.log",
        timeout=35 * 60,
    )
    if python_target.returncode:
        state["optional_python_probes"] = {"status": "python-api-build-failed-supplementary", "build_exit_code": python_target.returncode}
        record(receipt, state)
        return 0
    plugin_matches = list((source / "bin").rglob("plugins.xml"))
    package_matches = list((source / "bin").rglob("python/openvino/__init__.py"))
    if not plugin_matches or not package_matches:
        state["optional_python_probes"] = {"status": "python-runtime-artifacts-not-found-supplementary"}
        record(receipt, state)
        return 0
    plugin_config = plugin_matches[0]
    python_package = package_matches[0].parents[1]
    env = {
        **os.environ,
        "OPENVINO_PLUGIN_CONFIG": str(plugin_config),
        "PYTHONPATH": str(python_package) + (os.pathsep + os.environ["PYTHONPATH"] if os.environ.get("PYTHONPATH") else ""),
        "LD_LIBRARY_PATH": str(plugin_config.parent) + (os.pathsep + os.environ["LD_LIBRARY_PATH"] if os.environ.get("LD_LIBRARY_PATH") else ""),
    }
    matrix_probe = run(
        [str(args.python.resolve()), str(ROOT / "run_openvino_i32_matrix.py"), "--plugin-config", str(plugin_config), "--output", str(artifacts / "candidate-22-op-matrix.json")],
        ROOT,
        artifacts / "candidate-22-op-matrix.log",
        timeout=20 * 60,
        env=env,
    )
    state["optional_python_probes"] = {
        "status": "completed" if matrix_probe.returncode == 0 else "probe-error-supplementary",
        "python_api_build_exit_code": python_target.returncode,
        "matrix_exit_code": matrix_probe.returncode,
        "plugins_xml": str(plugin_config),
        "source_python_package": str(python_package),
        "matrix_receipt": "candidate-22-op-matrix.json",
    }
    if (artifacts / "candidate-22-op-matrix.json").is_file():
        matrix = json.loads((artifacts / "candidate-22-op-matrix.json").read_text(encoding="utf-8"))
        state["optional_python_probes"]["matrix_summary"] = matrix.get("summary")
    record(receipt, state)

    expected_hashes(source, manifest, {"tests", "production"})
    state["candidate"]["source_hashes_verified_at_completion"] = True
    state["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    record(receipt, state)
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"candidate validation aborted: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
