"""Measure public OpenVINO issue39337 on an unmodified, pinned release wheel."""

import argparse
import datetime
import json
import platform
from pathlib import Path
import sys

import numpy as np
import openvino as ov
import openvino.opset13 as ops


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, default=Path('artifacts/report.json'))
    parser.add_argument('--expect-known-failure', action='store_true')
    args = parser.parse_args()
    core = ov.Core()
    a = np.array([16777217, 16777215, -16777217, 2147483646, -2147483646, 11], dtype=np.int32).reshape(-1, 1)
    b = np.array([16777216, 16777216, -16777216, 1, -1, 7], dtype=np.int32).reshape(-1, 1)
    expected = {
        'Add': (a.astype(np.int64) + b.astype(np.int64)).astype(np.int32),
        'Greater': a > b,
        'Equal': a == b,
    }
    result = {
        'started_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
        'kind': 'Unmodified PyPI wheel baseline; not a source build or a candidate validation',
        'issue': 'https://github.com/openvinotoolkit/openvino/issues/39337',
        'openvino_version': ov.get_version(),
        'python': sys.version,
        'platform': platform.platform(),
        'machine': platform.machine(),
        'processor': core.get_property('CPU', 'FULL_DEVICE_NAME'),
        'numpy_version': np.__version__,
        'input_a': a.tolist(),
        'input_b': b.tolist(),
        'cases': [],
    }
    for name, constructor in [('Add', ops.add), ('Greater', ops.greater), ('Equal', ops.equal)]:
        pa = ops.parameter(a.shape, np.int32)
        pb = ops.parameter(b.shape, np.int32)
        model = ov.Model([constructor(pa, pb)], [pa, pb], f'i32_{name}')
        compiled = core.compile_model(model, 'CPU')
        values = [compiled([a, b])[0].copy() for _ in range(3)]
        assert all(np.array_equal(values[0], value) for value in values[1:]), name
        actual = values[0]
        matches = actual == expected[name]
        # Last row is a small, exactly representable control in every case.
        assert bool(matches[-1, 0]), (name, 'small-value control failed')
        result['cases'].append({
            'name': name,
            'expected': expected[name].tolist(),
            'actual': actual.tolist(),
            'matches': matches.tolist(),
            'mismatches': int(np.count_nonzero(~matches)),
            'output_dtype': str(actual.dtype),
            'three_runs_identical': True,
            'small_control_passes': True,
        })
    result['total_mismatches'] = sum(case['mismatches'] for case in result['cases'])
    result['completed_at_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(result, indent=2))
    if args.expect_known_failure and result['total_mismatches'] == 0:
        raise SystemExit('The reported baseline mismatch did not reproduce')


if __name__ == '__main__':
    main()
