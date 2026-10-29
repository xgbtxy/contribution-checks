"""Measure exact int32 eltwise results for OpenVINO issue #39337."""

from __future__ import annotations

import argparse
import datetime
import json
import platform
from pathlib import Path
import sys
import traceback

import numpy as np
import openvino as ov
import openvino.opset13 as ops


# Rank-2 values straddle +/-2**24. Every listed expected result remains in int32.
X = np.array(
    [[16777217, 16777216, -16777217, -16777216, 16777215],
     [-16777215, 2147483646, -2147483647, 7, -7]], dtype=np.int32
)
Y_ADD = np.array(
    [[16777216, 16777216, -16777216, -16777216, 1],
     [-1, 1, -1, 4, -4]], dtype=np.int32
)
Y_SUB = np.array(
    [[16777216, 16777215, -16777216, -16777215, -1],
     [1, -1, 1, 4, -4]], dtype=np.int32
)
Y_NEIGHBOR = np.array(
    [[16777216, 16777215, -16777216, -16777215, 16777216],
     [-16777216, 2147483645, -2147483646, 9, -9]], dtype=np.int32
)
Y_DIFFERENCE = np.array(
    [[16777216, 16777217, -16777216, -16777217, 16777216],
     [-16777216, 2147483645, -2147483646, 6, -6]], dtype=np.int32
)
Y_COMPARISON = np.array(
    [[16777216, 16777217, -16777216, -16777217, 16777216],
     [-16777216, 2147483645, -2147483646, 7, -7]], dtype=np.int32
)
Y_MOD = np.array(
    [[16777216, 16777217, 16777216, 16777217, 16777216],
     [16777216, 16777216, 3, 3, 3]], dtype=np.int32
)
Y_DIVIDE = np.array(
    [[1, 1, 1, 1, 1], [1, 1, 3, 3, 3]], dtype=np.int32
)
SLOPE = np.ones_like(X)
CONDITION = np.array(
    [[True, False, True, False, True], [False, True, False, True, False]], dtype=np.bool_
)
INT32_MIN = np.iinfo(np.int32).min
INT32_MAX = np.iinfo(np.int32).max


def _binary_case(name, op, left, right, oracle):
    params = [ops.parameter(left.shape, np.int32), ops.parameter(right.shape, np.int32)]
    node = op(*params)
    return name, params, node, lambda: oracle(left.astype(np.int64), right.astype(np.int64)), [left, right]


def _unary_case(name, op, data, oracle):
    params = [ops.parameter(data.shape, np.int32)]
    node = op(params[0])
    return name, params, node, lambda: oracle(data.astype(np.int64)), [data]


def cases():
    wide_x = X.astype(np.int64)
    wide_neighbor = Y_NEIGHBOR.astype(np.int64)

    definitions = [
        _binary_case("Add", ops.add, X, Y_ADD, lambda a, b: a + b),
        _binary_case("Subtract", ops.subtract, X, Y_SUB, lambda a, b: a - b),
        _binary_case("Multiply", ops.multiply, X, np.ones_like(X), lambda a, b: a * b),
        _binary_case("Minimum", ops.minimum, X, Y_NEIGHBOR, np.minimum),
        _binary_case("Maximum", ops.maximum, X, Y_NEIGHBOR, np.maximum),
        _unary_case("Abs", ops.absolute, X, np.abs),
        _unary_case("Negative", ops.negative, X, lambda a: -a),
        _binary_case("SquaredDifference", ops.squared_difference, X, Y_DIFFERENCE,
                     lambda a, b: (a - b) * (a - b)),
    ]

    params = [ops.parameter(X.shape, np.int32), ops.parameter(SLOPE.shape, np.int32)]
    definitions.append(("PRelu", params, ops.prelu(*params),
                        lambda: np.where(wide_x < 0, wide_x * SLOPE.astype(np.int64), wide_x),
                        [X, SLOPE]))

    params = [ops.parameter(CONDITION.shape, np.bool_), ops.parameter(X.shape, np.int32),
              ops.parameter(Y_NEIGHBOR.shape, np.int32)]
    definitions.append(("Select", params, ops.select(*params),
                        lambda: np.where(CONDITION, wide_x, wide_neighbor),
                        [CONDITION, X, Y_NEIGHBOR]))

    definitions.append(_unary_case("Floor", ops.floor, X, lambda a: a.copy()))
    definitions.append(_unary_case("Relu", ops.relu, X, lambda a: np.maximum(a, 0)))
    definitions.extend([
        _binary_case("FloorMod", ops.floor_mod, X, Y_MOD, np.remainder),
        _binary_case("Mod", ops.mod, X, Y_MOD, np.fmod),
        _binary_case("Greater", ops.greater, X, Y_COMPARISON, np.greater),
        _binary_case("GreaterEqual", ops.greater_equal, X, Y_COMPARISON, np.greater_equal),
        _binary_case("Less", ops.less, X, Y_COMPARISON, np.less),
        _binary_case("LessEqual", ops.less_equal, X, Y_COMPARISON, np.less_equal),
        _binary_case("Equal", ops.equal, X, Y_COMPARISON, np.equal),
        _binary_case("NotEqual", ops.not_equal, X, Y_COMPARISON, np.not_equal),
    ])
    params = [ops.parameter(X.shape, np.int32)]
    definitions.append(("Clamp", params, ops.clamp(params[0], INT32_MIN, INT32_MAX),
                        lambda: np.clip(wide_x, INT32_MIN, INT32_MAX), [X]))
    # opset13.divide constructs Divide with m_pythondiv=True by default.
    definitions.append(_binary_case("Divide", ops.divide, X, Y_DIVIDE, np.floor_divide))
    return definitions


def _json_values(array):
    return np.asarray(array).tolist()


def _runtime_metadata(compiled):
    """Best-effort runtime type/precision details; metadata never gates numeric results."""
    wanted = ("layerType", "runtimePrecision", "execType", "execOrder", "originalLayersNames")
    try:
        runtime_model = compiled.get_runtime_model()
        nodes = []
        for node in runtime_model.get_ordered_ops():
            info = node.get_rt_info()
            fields = {}
            for key in wanted:
                if key not in info:
                    continue
                value = info[key]
                get_value = getattr(value, "get", None)
                if callable(get_value):
                    value = get_value()
                fields[key] = str(value)
            if fields:
                nodes.append({
                    "friendly_name": node.get_friendly_name(),
                    "operation": node.get_type_name(),
                    "runtime_info": fields,
                })
        return {"status": "available", "nodes": nodes}
    except Exception as exc:
        return {"status": "unavailable", "error": f"{type(exc).__name__}: {exc}"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=Path("artifacts/matrix-report.json"))
    parser.add_argument("--device", default="CPU")
    parser.add_argument("--plugin-config", type=Path,
                        help="Optional OpenVINO plugins XML passed to ov.Core(config)")
    parser.add_argument("--require-all-pass", action="store_true")
    parser.add_argument("--expect-known-failure", action="store_true")
    args = parser.parse_args()
    if args.require_all_pass and args.expect_known_failure:
        parser.error("--require-all-pass and --expect-known-failure are mutually exclusive")

    plugin_config = str(args.plugin_config) if args.plugin_config else None
    core = ov.Core(plugin_config) if plugin_config else ov.Core()
    report = {
        "started_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "issue": "https://github.com/openvinotoolkit/openvino/issues/39337",
        "kind": "22-operation exact int32 measurement; exceptions and numeric mismatches are reported separately",
        "openvino_version": ov.get_version(),
        "openvino_module_file": ov.__file__,
        "python": sys.version,
        "platform": platform.platform(),
        "machine": platform.machine(),
        "device": args.device,
        "plugin_config": plugin_config,
        "device_full_name": core.get_property(args.device, "FULL_DEVICE_NAME"),
        "numpy_version": np.__version__,
        "cases": [],
    }

    for name, parameters, node, oracle, inputs in cases():
        result = {"name": name, "status": "error", "error": None, "mismatches": None,
                  "operator_attributes": node.get_attributes()}
        try:
            expected = np.asarray(oracle())
            if expected.dtype != np.bool_:
                if np.any(expected < INT32_MIN) or np.any(expected > INT32_MAX):
                    raise ValueError("test oracle produced an out-of-range int32 result")
                expected = expected.astype(np.int32)
            model = ov.Model([node], parameters, f"issue39337_{name}")
            compiled = core.compile_model(model, args.device)
            runtime_metadata = _runtime_metadata(compiled)
            observations = [np.asarray(compiled(inputs)[0]).copy() for _ in range(3)]
            if any(not np.array_equal(observations[0], value) for value in observations[1:]):
                raise AssertionError("three execution results were not identical")
            actual = observations[0]
            if actual.shape != expected.shape:
                raise AssertionError(f"output shape {actual.shape} != expected {expected.shape}")
            matches = actual == expected
            mismatches = []
            for coordinate in np.argwhere(~matches):
                idx = tuple(int(part) for part in coordinate)
                mismatches.append({"index": list(idx), "expected": expected[idx].item(),
                                   "actual": actual[idx].item()})
            result.update({
                "status": "mismatch" if mismatches else "pass",
                "mismatches": len(mismatches),
                "details": mismatches,
                "output_dtype": str(actual.dtype),
                "output_element_type": str(compiled.output(0).get_element_type()),
                "expected": _json_values(expected),
                "actual": _json_values(actual),
                "three_runs_identical": True,
                "runtime_model_metadata": runtime_metadata,
            })
        except Exception as exc:  # Record runtime/support failures distinctly from wrong values.
            result["error"] = f"{type(exc).__name__}: {exc}"
            result["traceback"] = traceback.format_exc()
        report["cases"].append(result)

    report["completed_at_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report["summary"] = {
        "cases_total": len(report["cases"]),
        "cases_passed": sum(row["status"] == "pass" for row in report["cases"]),
        "cases_with_mismatches": sum(row["status"] == "mismatch" for row in report["cases"]),
        "numeric_mismatches": sum(row.get("mismatches") or 0 for row in report["cases"]),
        "cases_with_errors": sum(row["status"] == "error" for row in report["cases"]),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))

    if args.require_all_pass and (report["summary"]["cases_with_errors"] or
                                  report["summary"]["numeric_mismatches"]):
        return 1
    issue_cases = report["cases"][:20]
    if args.expect_known_failure:
        if report["summary"]["cases_with_errors"]:
            return 1
        if not any(row["status"] == "mismatch" for row in issue_cases):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
