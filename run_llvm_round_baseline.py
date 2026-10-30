"""Compare LLVM's native f32 math.round with the math-expand-ops lowering."""

from __future__ import annotations

import argparse
import ctypes
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import struct
import subprocess
import sys
import time


CASES = (
    {"name": "positive-half-predecessor", "input_bits": 0x3EFFFFFF, "expected_bits": 0x00000000},
    {"name": "negative-half-predecessor", "input_bits": 0xBEFFFFFF, "expected_bits": 0x80000000},
    {"name": "positive-half-tie", "input_bits": 0x3F000000, "expected_bits": 0x3F800000},
    {"name": "negative-half-tie", "input_bits": 0xBF000000, "expected_bits": 0xBF800000},
    {"name": "positive-zero", "input_bits": 0x00000000, "expected_bits": 0x00000000},
    {"name": "negative-zero", "input_bits": 0x80000000, "expected_bits": 0x80000000},
)
EXPECTED_TARGET_MISMATCHES = {"positive-half-predecessor", "negative-half-predecessor"}
EXPECTED_WRONG_BITS = {"positive-half-predecessor": "0x3f800000", "negative-half-predecessor": "0xbf800000"}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def run_command(name: str, command: list[str], output_dir: Path, timeout: int = 60) -> dict:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=output_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
            text=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        returncode = completed.returncode
        timed_out = False
    except subprocess.TimeoutExpired as error:
        stdout = error.stdout or b""
        stderr = error.stderr or b""
        returncode = 124
        timed_out = True

    (output_dir / f"{name}.stdout").write_bytes(stdout)
    (output_dir / f"{name}.stderr").write_bytes(stderr)
    record = {
        "name": name,
        "argv": command,
        "returncode": returncode,
        "timed_out": timed_out,
        "elapsed_seconds": round(time.monotonic() - started, 4),
    }
    with (output_dir / "commands.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
    return record


def check_command(command: list[str], name: str, output_dir: Path, timeout: int = 60) -> dict:
    record = run_command(name, command, output_dir, timeout)
    if record["returncode"] != 0:
        raise RuntimeError(f"{name} failed; inspect {name}.stderr in the uploaded artifact")
    return record


def get_float_bits(function, input_bits: int) -> int:
    value = struct.unpack("<f", struct.pack("<I", input_bits))[0]
    result = function(value)
    return struct.unpack("<I", struct.pack("<f", result))[0]


def inspect_dpkg_versions() -> dict[str, str]:
    packages = ("llvm-24", "llvm-24-tools", "mlir-24-tools")
    versions = {}
    for package in packages:
        proc = subprocess.run(
            ["dpkg-query", "-W", "-f=${Version}", package],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if proc.returncode:
            raise RuntimeError(f"Required matched LLVM package {package} is missing")
        versions[package] = proc.stdout.strip()

    # LLVM's official Debian snapshots encode the upstream source commit in the
    # common version prefix. Debian packaging revisions may differ per package.
    source_revisions = {}
    for package, version in versions.items():
        match = re.search(r"(24~\+\+\d{14}\+[0-9a-fA-F]{7,40})", version)
        if not match:
            raise RuntimeError(f"Unrecognized apt.llvm.org LLVM 24 version: {package}={version}")
        source_revisions[package] = match.group(1)
    if len(set(source_revisions.values())) != 1:
        raise RuntimeError(f"Installed LLVM tools do not share one upstream source revision: {versions}")
    return versions


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("artifacts/llvm-round-235156"))
    parser.add_argument("--mlir-opt", type=Path, default=Path("/usr/lib/llvm-24/bin/mlir-opt"))
    parser.add_argument("--mlir-translate", type=Path, default=Path("/usr/lib/llvm-24/bin/mlir-translate"))
    parser.add_argument("--llc", type=Path, default=Path("/usr/lib/llvm-24/bin/llc"))
    parser.add_argument("--cc", type=Path, default=Path("/usr/bin/cc"))
    args = parser.parse_args()

    output_dir = args.output.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    manifest = {
        "started_at_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "kind": "Native x86_64 baseline measurement for #235156; no LLVM product changes",
        "issue": "https://github.com/llvm/llvm-project/issues/235156",
        "platform": platform.platform(),
        "machine": platform.machine(),
        "python": sys.version,
        "runner_expected": "Ubuntu 24.04 x86_64",
        "fenv_requested": "FE_TONEAREST (0)",
        "input_abi": "System V x86_64: repro(float) -> float",
        "fast_math": "none",
        "cases": list(CASES),
        "apt_versions": None,
        "tool_hashes": {},
        "tool_versions": {},
        "source": {"status": "not-run", "observations": []},
        "expanded": {"status": "not-run", "observations": []},
    }
    write_json(output_dir / "environment.json", manifest)

    if platform.machine().lower() not in {"x86_64", "amd64"}:
        raise RuntimeError(f"Expected native x86_64; got {platform.machine()}")
    manifest["apt_versions"] = inspect_dpkg_versions()

    tools = {
        "mlir_opt": args.mlir_opt,
        "mlir_translate": args.mlir_translate,
        "llc": args.llc,
        "cc": args.cc,
    }
    resolved_tools = {}
    for label, unresolved_path in tools.items():
        path = unresolved_path.resolve(strict=True)
        if not path.is_file():
            raise FileNotFoundError(f"LLVM validation tool is not a file: {path}")
        resolved_tools[label] = path
        manifest["tool_hashes"][label] = sha256(path)
        check_command([str(path), "--version"], f"{label}-version", output_dir)
        version_path = output_dir / f"{label}-version.stdout"
        version_text = version_path.read_text(encoding="utf-8", errors="replace").strip()
        manifest["tool_versions"][label] = version_text
        if label != "cc" and "24.0.0" not in version_text:
            raise RuntimeError(f"Expected LLVM 24 for {label}, got: {version_text}")

    libc = ctypes.CDLL("libm.so.6")
    libc.fegetround.argtypes = []
    libc.fegetround.restype = ctypes.c_int
    libc.fesetround.argtypes = [ctypes.c_int]
    libc.fesetround.restype = ctypes.c_int
    previous_rounding_mode = libc.fegetround()
    if libc.fesetround(0) != 0 or libc.fegetround() != 0:
        raise RuntimeError("This runner could not set FE_TONEAREST")
    manifest["fenv_previous"] = previous_rounding_mode
    manifest["fenv_verified"] = libc.fegetround()

    inputs = output_dir / "inputs.json"
    write_json(inputs, list(CASES))
    source_mlir = output_dir / "source.mlir"
    parsed_source_mlir = output_dir / "parsed-source.mlir"
    expanded_mlir = output_dir / "expanded.mlir"
    source_llvm = output_dir / "source.llvm.mlir"
    expanded_llvm = output_dir / "expanded.llvm.mlir"
    source_ir = output_dir / "source.ll"
    expanded_ir = output_dir / "expanded.ll"
    source_text = (
        "func.func @repro(%x: f32) -> f32 {\n"
        "  %result = math.round %x : f32\n"
        "  return %result : f32\n"
        "}\n"
    )
    source_mlir.write_text(source_text, encoding="utf-8")
    manifest["source"]["input_sha256"] = sha256(source_mlir)
    write_json(output_dir / "environment.json", manifest)

    source_pipeline = [
        "--convert-math-to-llvm",
        "--convert-math-to-libm",
        "--convert-arith-to-llvm",
        "--convert-func-to-llvm",
        "--reconcile-unrealized-casts",
    ]
    prefix = ["--mlir-disable-threading"]
    check_command(
        [str(resolved_tools["mlir_opt"]), *prefix, str(source_mlir), "-o", str(parsed_source_mlir)],
        "source-parse",
        output_dir,
    )
    # Keep a separate copy of the original math.round program. The source and
    # expansion pipelines are lowered independently from the same input bytes.
    source_lowered = output_dir / "source-original.mlir"
    source_lowered.write_bytes(parsed_source_mlir.read_bytes())
    check_command(
        [str(resolved_tools["mlir_opt"]), *prefix, "--math-expand-ops=ops=round", str(source_mlir), "-o", str(expanded_mlir)],
        "expand-round",
        output_dir,
    )
    (output_dir / "round-expansion.mlir").write_bytes(expanded_mlir.read_bytes())
    check_command(
        [str(resolved_tools["mlir_opt"]), *prefix, *source_pipeline, str(source_lowered), "-o", str(source_llvm)],
        "source-lower",
        output_dir,
    )
    check_command(
        [str(resolved_tools["mlir_opt"]), *prefix, *source_pipeline, str(expanded_mlir), "-o", str(expanded_llvm)],
        "expanded-lower",
        output_dir,
    )
    check_command(
        [str(resolved_tools["mlir_translate"]), "--mlir-to-llvmir", str(source_llvm), "-o", str(source_ir)],
        "source-translate",
        output_dir,
    )
    check_command(
        [str(resolved_tools["mlir_translate"]), "--mlir-to-llvmir", str(expanded_llvm), "-o", str(expanded_ir)],
        "expanded-translate",
        output_dir,
    )

    compiled_functions = {}
    for path_label, llvm_ir in (("source", source_ir), ("expanded", expanded_ir)):
        for level in ("0", "2"):
            obj = output_dir / f"{path_label}-O{level}.o"
            library = output_dir / f"{path_label}-O{level}.so"
            check_command(
                [str(resolved_tools["llc"]), "-filetype=obj", "-relocation-model=pic", f"-O{level}", str(llvm_ir), "-o", str(obj)],
                f"{path_label}-O{level}-codegen",
                output_dir,
            )
            check_command(
                [str(resolved_tools["cc"]), "-shared", str(obj), "-lm", "-o", str(library)],
                f"{path_label}-O{level}-link",
                output_dir,
            )
            function = ctypes.CDLL(str(library), mode=ctypes.RTLD_LOCAL).repro
            function.argtypes = [ctypes.c_float]
            function.restype = ctypes.c_float
            observations = []
            mismatches = []
            for case in CASES:
                actual_bits = get_float_bits(function, case["input_bits"])
                observation = {
                    "name": case["name"],
                    "input_bits": f"0x{case['input_bits']:08x}",
                    "expected_bits": f"0x{case['expected_bits']:08x}",
                    "actual_bits": f"0x{actual_bits:08x}",
                    "matches": actual_bits == case["expected_bits"],
                }
                observations.append(observation)
                if not observation["matches"]:
                    mismatches.append(case["name"])
            manifest[path_label].setdefault("optimization_levels", {})[f"O{level}"] = {
                "library_sha256": sha256(library),
                "llvm_ir_sha256": sha256(llvm_ir),
                "observations": observations,
                "mismatches": mismatches,
            }
            compiled_functions[(path_label, level)] = mismatches
            write_json(output_dir / "environment.json", manifest)

    source_ok = all(not mismatches for (label, _), mismatches in compiled_functions.items() if label == "source")
    expanded_ok = all(
        set(mismatches) == EXPECTED_TARGET_MISMATCHES
        and all(observation["actual_bits"] == EXPECTED_WRONG_BITS[observation["name"]]
                for observation in manifest["expanded"]["optimization_levels"][f"O{level}"]["observations"]
                if observation["name"] in EXPECTED_WRONG_BITS)
        for (label, level), mismatches in compiled_functions.items()
        if label == "expanded"
    )
    if libc.fegetround() != 0:
        raise RuntimeError("Native evaluation changed the requested FE_TONEAREST environment")
    manifest["source"]["status"] = "6/6 exact outputs match the independent oracle" if source_ok else "unexpected source-path mismatch"
    manifest["expanded"]["status"] = "both half-predecessors fail at O0 and O2; all four controls pass" if expanded_ok else "expanded-path results differ from the reported two-error baseline"
    manifest["expected_baseline_reproduced"] = bool(source_ok and expanded_ok)
    manifest["completed_at_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    write_json(output_dir / "environment.json", manifest)
    write_json(output_dir / "results.json", {
        "expected_source_matches": source_ok,
        "expected_expanded_mismatches": expanded_ok,
        "reproduced": manifest["expected_baseline_reproduced"],
        "counts": {
            f"{label}-O{level}": {"total": len(CASES), "mismatches": len(mismatches)}
            for (label, level), mismatches in compiled_functions.items()
        },
        "source_file_sha256": sha256(source_mlir),
    })
    print(json.dumps({
        "reproduced": manifest["expected_baseline_reproduced"],
        "source": "6/6 pass" if source_ok else "unexpected mismatch",
        "expanded_O0": compiled_functions[("expanded", "0")],
        "expanded_O2": compiled_functions[("expanded", "2")],
        "tools": manifest["tool_versions"],
        "apt_versions": manifest["apt_versions"],
    }, indent=2))
    if not manifest["expected_baseline_reproduced"]:
        return 1
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        # Setup/lowering/build failures have a distinct status from a numeric repro.
        print(f"native round baseline setup/execution failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise
