#!/usr/bin/env python3
"""Apply the pinned #26691 candidate and run one review check group."""

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

import run_baseline as base


ROOT = Path(__file__).resolve().parent
PIN = "039c0b99d5b05e73ff5def029c0131b76b481c6f"
MANIFEST_PATH = ROOT / "patches" / "datafusion-26691.json"
PATCH_PATHS = {
    "tests": ROOT / "patches" / "datafusion-26691-tests.patch",
    "production": ROOT / "patches" / "datafusion-26691-production.patch",
}
GROUPS = {"fmt-clippy", "extended", "public-api", "lint"}
BASE_ENV = {
    "CARGO_BUILD_JOBS": "4",
    "CARGO_PROFILE_DEV_DEBUG": "0",
    "CARGO_INCREMENTAL": "0",
    "RUST_BACKTRACE": "1",
}
PROTOC_VERSION = "libprotoc 3.21.12"


def sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def status_paths(status):
    paths = set()
    for line in status.splitlines():
        if line.strip():
            paths.add(line[3:].strip().strip('"'))
    return paths


def artifact_path(receipt, *parts):
    return Path(receipt["artifact_dir"]).joinpath(*parts)


def add_error(receipt, message):
    if message not in receipt["errors"]:
        receipt["errors"].append(message)
    base.dump(artifact_path(receipt, "receipt.json"), receipt)


def run_command(receipt, source, name, command, timeout, env_overrides=None):
    env = os.environ.copy()
    env.update(BASE_ENV)
    if env_overrides:
        env.update(env_overrides)
    result = base.run(
        command,
        source,
        artifact_path(receipt, "logs", f"{name}.log"),
        timeout=timeout,
        env=env,
    )
    receipt["commands"][name] = {
        "command": [str(part) for part in command],
        "cwd": str(source),
        "env_overrides": {**BASE_ENV, **(env_overrides or {})},
        "started_utc": result["started_utc"],
        "finished_utc": result["finished_utc"],
        "timeout_seconds": timeout,
        "exit_code": result["exit_code"],
        "timed_out": result["timed_out"],
        "stdout_bytes": result["stdout_bytes"],
        "stderr_bytes": result["stderr_bytes"],
        "logs": {
            "stdout": result["stdout_log"],
            "stderr": result["stderr_log"],
            "metadata": result["meta_log"],
        },
    }
    if result["exit_code"] != 0 or result["timed_out"]:
        add_error(
            receipt,
            f"{name}: command failed or timed out (exit={result['exit_code']}, timeout={result['timed_out']})",
        )
    else:
        base.dump(artifact_path(receipt, "receipt.json"), receipt)
    return result


def validate_source_facts(facts, expected_files=None):
    errors = []
    if facts["head"] != PIN:
        errors.append(f"source HEAD is {facts['head']}, expected {PIN}")
    paths = status_paths(facts["status_porcelain"])
    if paths != (expected_files or set()):
        errors.append(f"unexpected source status paths: {sorted(paths)}")
    changed = set(facts["diff_names"])
    if changed != (expected_files or set()):
        errors.append(f"unexpected source diff paths: {sorted(changed)}")
    if any(not line or line[0] != " " for line in facts["submodules"]):
        errors.append("one or more source submodules are uninitialized or differ from the pinned gitlink")
    for name, hashes in facts["tracked_hashes"].items():
        if hashes["head_blob"] != hashes["worktree_blob"] and name not in (expected_files or set()):
            errors.append(f"baseline tracked file differs from HEAD: {name}")
    return errors


def load_manifest(receipt):
    manifest_bytes = MANIFEST_PATH.read_bytes()
    manifest = json.loads(manifest_bytes.decode("utf-8"))
    receipt["manifest"] = {
        "path": str(MANIFEST_PATH.relative_to(ROOT)),
        "sha256": hashlib.sha256(manifest_bytes).hexdigest(),
        "base": manifest.get("base"),
        "issue": manifest.get("issue"),
        "files": manifest.get("files"),
    }
    if manifest.get("base") != PIN:
        raise ValueError(f"manifest base {manifest.get('base')} does not match {PIN}")
    if set(manifest.get("files", {})) != set(PATCH_PATHS):
        raise ValueError("manifest must name exactly the tests and production patches")

    target_paths = set()
    for name, patch_path in PATCH_PATHS.items():
        spec = manifest["files"][name]
        patch_bytes = patch_path.read_bytes()
        actual_sha = hashlib.sha256(patch_bytes).hexdigest()
        if actual_sha != spec.get("patch_sha256"):
            raise ValueError(f"{name} patch SHA-256 differs from the manifest")
        headers = {
            line[6:]
            for line in patch_bytes.decode("utf-8").splitlines()
            if line.startswith("+++ b/")
        }
        if headers != {spec.get("path")}:
            raise ValueError(f"{name} patch changes unexpected paths: {sorted(headers)}")
        target_paths.add(spec["path"])
        receipt.setdefault("patches", {})[name] = {
            "path": str(patch_path.relative_to(ROOT)),
            "sha256": actual_sha,
            "target": spec["path"],
            "head_blob": spec.get("head_blob"),
            "candidate_blob": spec.get("candidate_blob"),
        }
    receipt["allowed_source_files"] = sorted(target_paths)
    base.dump(artifact_path(receipt, "receipt.json"), receipt)
    return manifest, target_paths


def source_blob(source, path):
    return base.git(source, "hash-object", str(source / path))


def capture_diff(receipt, source, name):
    result = run_command(
        receipt,
        source,
        f"{name}-git-diff",
        ["git", "diff", "--no-ext-diff", "--binary", "HEAD"],
        timeout=60,
    )
    path = artifact_path(receipt, "diffs", f"{name}.patch")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(result["stdout"], encoding="utf-8")
    receipt.setdefault("diffs", {})[name] = {
        "path": str(path.relative_to(ROOT)),
        "sha256": sha256(path),
        "bytes": path.stat().st_size,
        "exit_code": result["exit_code"],
    }
    base.dump(artifact_path(receipt, "receipt.json"), receipt)


def apply_patch(receipt, source, manifest, name, expected_files):
    patch_path = PATCH_PATHS[name]
    for suffix, args in (
        ("check", ["git", "apply", "--check", str(patch_path)]),
        ("apply", ["git", "apply", str(patch_path)]),
    ):
        result = run_command(receipt, source, f"patch-{name}-{suffix}", args, timeout=60)
        if result["exit_code"] != 0 or result["timed_out"]:
            raise RuntimeError(f"git apply {suffix} failed for {name}")

    facts = base.source_facts(source)
    receipt.setdefault("source_after_patch", {})[name] = facts
    base.dump(artifact_path(receipt, "receipt.json"), receipt)
    failures = validate_source_facts(facts, expected_files)
    spec = manifest["files"][name]
    actual_blob = source_blob(source, spec["path"])
    receipt["patches"][name]["actual_blob"] = actual_blob
    if actual_blob != spec.get("candidate_blob"):
        failures.append(
            f"{name} result blob is {actual_blob}, expected {spec.get('candidate_blob')}"
        )
    if failures:
        raise RuntimeError("; ".join(failures))
    receipt["patches"][name]["applied"] = True
    base.dump(artifact_path(receipt, "receipt.json"), receipt)


def record_versions(receipt, source):
    results = {}
    for name, command in (
        ("rustc", ["rustc", "+1.99.0", "--version", "--verbose"]),
        ("cargo", ["cargo", "+1.99.0", "--version"]),
        ("protoc", ["protoc", "--version"]),
    ):
        result = run_command(receipt, source, f"version-{name}", command, timeout=30)
        version = (result["stdout"] + result["stderr"]).strip()
        results[name] = version
        if result["exit_code"] != 0 or result["timed_out"]:
            add_error(receipt, f"could not query {name} version")
        elif name == "rustc" and not version.startswith("rustc 1.99.0 "):
            add_error(receipt, f"unexpected rustc version: {version}")
        elif name == "cargo" and not version.startswith("cargo 1.99.0 "):
            add_error(receipt, f"unexpected cargo version: {version}")
        elif name == "protoc" and version != PROTOC_VERSION:
            add_error(receipt, f"expected {PROTOC_VERSION}, got {version}")
    if receipt["check_group"] == "lint":
        for name, command, prefix in (
            ("uv", ["uv", "--version"], "uv "),
            ("node", ["node", "--version"], "v24."),
        ):
            result = run_command(receipt, source, f"version-{name}", command, timeout=30)
            version = (result["stdout"] + result["stderr"]).strip()
            results[name] = version
            if result["exit_code"] != 0 or result["timed_out"] or not version.startswith(prefix):
                add_error(receipt, f"unexpected {name} version: {version}")
    receipt["toolchain"] = results
    base.dump(artifact_path(receipt, "receipt.json"), receipt)


def run_group(receipt, source):
    extended = [
        "cargo",
        "test",
        "--profile",
        "ci",
        "--exclude",
        "datafusion-examples",
        "--exclude",
        "datafusion-benchmarks",
        "--exclude",
        "datafusion-cli",
        "--workspace",
        "--lib",
        "--tests",
        "--bins",
        "--features",
        "avro,json,backtrace,extended_tests,recursive_protection,parquet_encryption",
    ]
    group = receipt["check_group"]
    if group == "fmt-clippy":
        fmt = run_command(receipt, source, "cargo-fmt-all", ["cargo", "fmt", "--all"], timeout=20 * 60)
        facts = base.source_facts(source)
        receipt["source_after_fmt"] = facts
        if fmt["exit_code"] == 0 and not fmt["timed_out"]:
            expected = {spec["path"]: spec["candidate_blob"] for spec in receipt["manifest"]["files"].values()}
            actual = {name: hashes["worktree_blob"] for name, hashes in facts["tracked_hashes"].items() if name in expected}
            mismatched = {name: {"expected": expected[name], "actual": actual.get(name)} for name in expected if actual.get(name) != expected[name]}
            unexpected_paths = set(facts["diff_names"]) != set(expected) or status_paths(facts["status_porcelain"]) != set(expected)
            if mismatched or unexpected_paths:
                receipt["format_drift"] = mismatched
                capture_diff(receipt, source, "after-fmt-actual-diff")
                if mismatched:
                    add_error(receipt, "cargo fmt changed a manifest-pinned candidate blob; actual diff was retained")
                if unexpected_paths:
                    add_error(receipt, f"cargo fmt changed unexpected source paths: {sorted(set(facts['diff_names']))}")
        else:
            capture_diff(receipt, source, "after-fmt-failure-diff")
        run_command(
            receipt,
            source,
            "cargo-clippy-all-targets-all-features",
            ["cargo", "clippy", "--all-targets", "--all-features", "--", "-D", "warnings"],
            timeout=55 * 60,
        )
    elif group == "extended":
        run_command(receipt, source, "cargo-test-extended-workspace", extended, timeout=80 * 60, env_overrides={"RUST_BACKTRACE": "1"})
    elif group == "public-api":
        run_command(receipt, source, "cargo-test-datafusion", ["cargo", "test", "-p", "datafusion"], timeout=38 * 60)
        run_command(receipt, source, "cargo-test-datafusion-cli", ["cargo", "test", "-p", "datafusion-cli"], timeout=38 * 60)
    elif group == "lint":
        run_command(receipt, source, "uv-run-rust-lint", ["uv", "run", "./dev/rust_lint.sh"], timeout=80 * 60)
    else:
        add_error(receipt, f"unknown check group: {group}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--group", required=True, choices=sorted(GROUPS))
    parser.add_argument("--source", type=Path, default=ROOT / "datafusion")
    parser.add_argument("--artifacts", type=Path, default=ROOT / "artifacts")
    args = parser.parse_args()

    source, artifacts = args.source.resolve(), args.artifacts.resolve()
    (artifacts / "logs").mkdir(parents=True, exist_ok=True)
    receipt = {
        "status": "running",
        "started_utc": base.utc(),
        "source_pin": PIN,
        "check_group": args.group,
        "artifact_dir": str(artifacts),
        "workflow": {
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "workflow": os.environ.get("GITHUB_WORKFLOW"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
            "job": os.environ.get("GITHUB_JOB"),
            "matrix_group": os.environ.get("CHECK_GROUP"),
            "ref": os.environ.get("GITHUB_REF"),
            "harness_sha": os.environ.get("GITHUB_SHA"),
        },
        "source_path": str(source),
        "commands": {},
        "patches": {},
        "errors": [],
        "junit_created": False,
    }
    base.dump(artifacts / "receipt.json", receipt)
    manifest = None
    allowed = set()
    applied = False

    try:
        manifest, allowed = load_manifest(receipt)
        if not source.is_dir():
            raise FileNotFoundError(f"DataFusion checkout does not exist: {source}")
        before = base.source_facts(source)
        receipt["source_before"] = before
        failures = validate_source_facts(before)
        for name, spec in manifest["files"].items():
            actual = before["tracked_hashes"].get(spec["path"], {}).get("head_blob")
            if actual != spec.get("head_blob"):
                failures.append(f"{name} source blob is {actual}, expected {spec.get('head_blob')}")
        if failures:
            raise RuntimeError("baseline source validation failed: " + "; ".join(failures))
        base.dump(artifacts / "receipt.json", receipt)

        record_versions(receipt, source)
        if receipt["errors"]:
            raise RuntimeError("pinned toolchain validation failed")

        apply_patch(receipt, source, manifest, "tests", {manifest["files"]["tests"]["path"]})
        apply_patch(receipt, source, manifest, "production", allowed)
        applied = True
        receipt["source_after_patch"] = base.source_facts(source)
        base.dump(artifacts / "receipt.json", receipt)

        run_group(receipt, source)
    except Exception as exc:
        add_error(receipt, f"{type(exc).__name__}: {exc}")
        if source.is_dir():
            try:
                receipt["source_after_failure"] = base.source_facts(source)
                base.dump(artifacts / "receipt.json", receipt)
            except Exception as source_exc:
                add_error(receipt, f"could not record failed source state: {type(source_exc).__name__}: {source_exc}")
    finally:
        if source.is_dir():
            try:
                after = base.source_facts(source)
                receipt["source_after"] = after
                receipt["allowed_files_final"] = sorted(allowed)
                if applied:
                    failures = validate_source_facts(after, allowed)
                    if manifest:
                        for spec in manifest["files"].values():
                            actual = after["tracked_hashes"].get(spec["path"], {}).get("worktree_blob")
                            if actual != spec.get("candidate_blob"):
                                failures.append(
                                    f"final blob for {spec['path']} is {actual}, expected {spec.get('candidate_blob')}"
                                )
                    if failures:
                        capture_diff(receipt, source, "final-source-validation-diff")
                    for failure in failures:
                        add_error(receipt, failure)
            except Exception as exc:
                add_error(receipt, f"could not verify final source state: {type(exc).__name__}: {exc}")
        receipt["finished_utc"] = base.utc()
        receipt["status"] = "checks_pass" if not receipt["errors"] else "checks_fail"
        receipt.pop("artifact_dir", None)
        base.dump(artifacts / "receipt.json", receipt)

    print(json.dumps({"status": receipt["status"], "check_group": args.group, "errors": receipt["errors"]}, indent=2))
    return 0 if not receipt["errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
