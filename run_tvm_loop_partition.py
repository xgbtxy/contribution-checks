# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#   http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Run the TVM #20823 reduction reproducer and unpartitioned control."""

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

SOURCE_SHA = "2bf0532998d4dc7879cb342e873608b90c1d4497"
EXPECTED = [45, 145, 245, 345]
BASELINE_BUG = [24, 54, 84, 114]
def git(source, *args):
    return subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True, text=True).stdout.strip()


def source_state(source):
    modules = subprocess.run(
        ["git", "-C", str(source), "submodule", "status", "--recursive"],
        check=True, capture_output=True, text=True
    ).stdout.rstrip("\n").splitlines()
    return {
        "head": git(source, "rev-parse", "HEAD"),
        "status": git(source, "status", "--porcelain=v1"),
        "submodules": modules,
    }


def check_source(state):
    if state["head"] != SOURCE_SHA or state["status"]:
        raise RuntimeError(f"Unexpected source state: {state['head']} {state['status']}")
    if any(line[0] in "+U" for line in state["submodules"]):
        raise RuntimeError("A checked-out submodule differs from the source-pinned revision")
    return [line for line in state["submodules"] if line[0] == "-"]


def check_build(source):
    entries = {}
    for line in (source / "build" / "CMakeCache.txt").read_text().splitlines():
        if ":" in line and "=" in line and not line.startswith("//"):
            entries[line.split(":", 1)[0]] = line.split("=", 1)[1]
    required = {
        "TVM_USE_INSTALLED_TIRX": "OFF", "TVM_TIRX_USE_CUDA": "OFF",
        "TVM_TIRX_BUILD_TESTS": "OFF", "TVM_USE_CUTLASS": "OFF", "BUILD_TESTING": "OFF",
    }
    if any(entries.get(key, "").upper() != value for key, value in required.items()):
        raise RuntimeError("CMakeCache does not match the CPU LLVM build settings")
    llvm = entries.get("TVM_TIRX_USE_LLVM", "")
    if "llvm-config-18" not in llvm or not Path(llvm).exists():
        raise RuntimeError(f"LLVM 18 not configured: {llvm!r}")
    return {**required, "TVM_TIRX_USE_LLVM": llvm}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = args.source.resolve()
    receipt = {
        "issue": "https://github.com/apache/tvm/issues/20823", "expected": EXPECTED,
        "expected_baseline_bug": BASELINE_BUG, "python": sys.version,
        "library_paths": {k: os.environ.get(k) for k in ("TVM_LIBRARY_PATH", "TVM_TIRX_LIBRARY_PATH")},
        "status": "failed",
    }
    try:
        before = source_state(source)
        receipt["source"] = before
        receipt["uninitialized_submodules"] = check_source(before)
        receipt["build_config"] = check_build(source)
        expected_lib = (source / "build" / "lib").resolve()
        for key in ("TVM_LIBRARY_PATH", "TVM_TIRX_LIBRARY_PATH"):
            if Path(receipt["library_paths"][key]).resolve() != expected_lib:
                raise RuntimeError(f"{key} must resolve to {expected_lib}")

        import numpy as np
        import tvm
        from tvm import s_tir, te

        if not Path(tvm.__file__).resolve().is_relative_to(source / "python"):
            raise RuntimeError(f"TVM did not import from source: {tvm.__file__}")
        data = np.arange(40, dtype="int32").reshape(4, 10)
        a = te.placeholder((4, 10), "int32", name="A")
        k = te.reduce_axis((0, 10), name="k")
        s = te.compute((4,), lambda i: te.sum(a[i, k], axis=k), name="S")
        control = tvm.compile(te.create_function([a, s]), target="llvm")
        schedule = s_tir.Schedule(te.create_function([a, s]))
        _, loop = schedule.get_loops(schedule.get_sblock("S"))
        schedule.loop_partition(loop, factors=[None, 3])
        partitioned = tvm.compile(schedule.mod, target="llvm")

        def run(compiled):
            output = tvm.runtime.tensor(np.zeros(4, dtype="int32"))
            compiled(tvm.runtime.tensor(data), output)
            return output.numpy().tolist()

        receipt["numpy"] = np.__version__
        receipt["unpartitioned"] = run(control)
        receipt["partitioned"] = run(partitioned)
        if receipt["unpartitioned"] != EXPECTED or receipt["partitioned"] != BASELINE_BUG:
            raise RuntimeError("Observed reduction arrays differ from the expected baseline")
        after = source_state(source)
        if after != before:
            raise RuntimeError(f"Source changed during validation: {after}")
        receipt["source_after"] = after
        receipt["status"] = "passed"
    except Exception as exc:
        receipt["error"] = f"{type(exc).__name__}: {exc}"

    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(receipt, indent=2) + "\n")
    print(json.dumps(receipt, indent=2))
    return 0 if receipt["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
