"""Run the i32 elementwise precision regression against a local OpenVINO source build."""

import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import sys

import numpy as np
import openvino as ov
import openvino.opset13 as ops


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/source-baseline.json"))
    parser.add_argument("--expect-known-failure", action="store_true")
    args = parser.parse_args()

    plugin_config = Path(os.environ["OPENVINO_PLUGIN_CONFIG"])
    if not plugin_config.is_file():
        raise FileNotFoundError(f"CPU plugin registry is missing: {plugin_config}")
    core = ov.Core(str(plugin_config))
    devices = core.available_devices
    if "CPU" not in devices:
        raise RuntimeError(f"CPU device is not registered in {plugin_config}: {devices}")

    a = np.array([16777217, 16777215, -16777217, 2147483646, -2147483646, 11], dtype=np.int32).reshape(-1, 1)
    b = np.array([16777216, 16777216, -16777216, 1, -1, 7], dtype=np.int32).reshape(-1, 1)
    expected = {
        "Add": (a.astype(np.int64) + b.astype(np.int64)).astype(np.int32),
        "Greater": a > b,
        "Equal": a == b,
    }
    result = {
        "started_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "kind": "Unmodified current-master source-build baseline",
        "issue": "https://github.com/openvinotoolkit/openvino/issues/39337",
        "openvino_version": ov.get_version(),
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "processor": core.get_property("CPU", "FULL_DEVICE_NAME"),
        "numpy_version": np.__version__,
        "plugins_xml": str(plugin_config),
        "available_devices": devices,
        "input_a": a.tolist(),
        "input_b": b.tolist(),
        "cases": [],
    }
    for name, constructor in [("Add", ops.add), ("Greater", ops.greater), ("Equal", ops.equal)]:
        pa = ops.parameter(a.shape, np.int32)
        pb = ops.parameter(b.shape, np.int32)
        model = ov.Model([constructor(pa, pb)], [pa, pb], f"source_i32_{name}")
        compiled = core.compile_model(model, "CPU")
        values = [compiled([a, b])[0].copy() for _ in range(3)]
        if not all(np.array_equal(values[0], value) for value in values[1:]):
            raise AssertionError(f"{name} was nondeterministic across three runs")
        actual = values[0]
        matches = actual == expected[name]
        # The final row provides an exact small-integer control for every operation.
        if not bool(matches[-1, 0]):
            raise AssertionError(f"{name} small-value control did not pass")
        result["cases"].append(
            {
                "name": name,
                "expected": expected[name].tolist(),
                "actual": actual.tolist(),
                "matches": matches.tolist(),
                "mismatches": int(np.count_nonzero(~matches)),
                "output_dtype": str(actual.dtype),
                "three_runs_identical": True,
                "small_control_passes": True,
            }
        )
    result["total_mismatches"] = sum(case["mismatches"] for case in result["cases"])
    result["completed_at_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    if args.expect_known_failure and result["total_mismatches"] == 0:
        raise SystemExit("The current-source ARM64 mismatch did not reproduce")


if __name__ == "__main__":
    main()
