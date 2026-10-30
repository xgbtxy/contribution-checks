"""Build and execute the actual upstream math expansion before and after a patch."""
import ctypes
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import xml.etree.ElementTree as ET

import run_llvm_round_baseline as native

ROOT = Path(__file__).resolve().parent
SOURCE = ROOT / 'llvm-src'
BUILD = ROOT / 'llvm-build'
OUT = ROOT / 'artifacts/llvm-round-source'
PATCHES = ROOT / 'patches/llvm-round'
MANIFEST = json.loads((PATCHES / 'manifest.json').read_text())
CASES = json.loads((ROOT / 'llvm-round-cases.json').read_text())['cases']
TOOLS = Path('/usr/lib/llvm-24/bin')
STATE = {'started_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
         'source_base': MANIFEST['base_commit'], 'validation_scope': 'Source-built MLIRMathTransforms and focused driver with assertions. Packaged LLVM24 performs independent lowering/codegen. Not a full mlir-opt build.',
         'phases': {}}


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def save():
    OUT.mkdir(parents=True, exist_ok=True)
    STATE['updated_at_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    (OUT / 'receipt.json').write_text(json.dumps(STATE, indent=2) + '\n')


def git(*args):
    return subprocess.run(['git', '-C', str(SOURCE), *args], check=True, capture_output=True, text=True).stdout.rstrip('\r\n')


def invoke(name, argv, *, timeout=180, allow_failure=False):
    save()
    print(f'START {name}', flush=True)
    log = OUT / f'{name}.log'
    with log.open('w') as output:
        result = subprocess.run([str(arg) for arg in argv], cwd=ROOT, stdout=output, stderr=subprocess.STDOUT, timeout=timeout)
    STATE.setdefault('commands', []).append({'name': name, 'argv': [str(arg) for arg in argv], 'exit_code': result.returncode, 'log': log.name})
    save()
    print(f'END {name}: {result.returncode}', flush=True)
    if result.returncode and not allow_failure:
        print(log.read_text(errors='replace')[-9000:], flush=True)
        raise RuntimeError(f'{name} failed; not numerical regression evidence')
    return result.returncode


def verify(groups):
    hashes = {}
    for path, spec in MANIFEST['files'].items():
        hashes[path] = sha(SOURCE / path)
        wanted = spec['candidate_sha256'] if spec['group'] in groups else spec['original_sha256']
        assert hashes[path] == wanted, f'Unexpected source bytes: {path}'
    changed = {line[3:] for line in git('status', '--porcelain', '--untracked-files=all').splitlines()}
    expected = {path for path, spec in MANIFEST['files'].items() if spec['group'] in groups}
    assert changed == expected, (changed, expected)
    return hashes


def patch(name):
    path = PATCHES / name
    assert sha(path) == MANIFEST['patches'][name]['sha256']
    invoke(name + '-check', ['git', '-C', SOURCE, 'apply', '--check', path])
    invoke(name + '-apply', ['git', '-C', SOURCE, 'apply', path])


def filecheck(label, testfile, *, expect_failure=False):
    output = OUT / f'{label}.mlir'
    invoke(label + '-expand', [BUILD / 'bin/mlir-round-expand-opt', '--mlir-disable-threading', '--split-input-file', '-math-expand-ops', testfile, '-o', output])
    code = invoke(label + '-filecheck', [BUILD / 'bin/FileCheck', testfile, '--input-file', output], allow_failure=expect_failure)
    if expect_failure:
        assert code != 0, 'The added guard checks unexpectedly pass on original source'
        return
    filtered = OUT / f'{label}-filtered.mlir'
    invoke(label + '-filter', [BUILD / 'bin/mlir-round-expand-opt', '--mlir-disable-threading', '--split-input-file', '-math-expand-ops=ops=tanh,tan', testfile, '-o', filtered])
    invoke(label + '-filter-filecheck', [BUILD / 'bin/FileCheck', testfile, '--input-file', filtered, '--check-prefix=CHECK-FILTER'])


def native_probe(label, *, expect_red):
    output = OUT / f'{label}-native'
    output.mkdir()
    source_mlir = output / 'source.mlir'
    source_mlir.write_text('func.func @repro(%x: f32) -> f32 {\n %r = math.round %x : f32\n return %r : f32\n}\n')
    expanded = output / 'expanded.mlir'
    native.check_command([str(BUILD / 'bin/mlir-round-expand-opt'), '--mlir-disable-threading', '--math-expand-ops=ops=round', str(source_mlir), '-o', str(expanded)], 'source-built-expansion', output)
    pipeline = ['--convert-math-to-llvm', '--convert-math-to-libm', '--convert-arith-to-llvm', '--convert-func-to-llvm', '--reconcile-unrealized-casts']
    libm = ctypes.CDLL('libm.so.6')
    assert libm.fegetround() == 0
    results = {}
    for kind, ir in [('source', source_mlir), ('expanded', expanded)]:
        lowered, ll = output / f'{kind}.llvm.mlir', output / f'{kind}.ll'
        native.check_command([str(TOOLS / 'mlir-opt'), '--mlir-disable-threading', *pipeline, str(ir), '-o', str(lowered)], kind + '-lower', output)
        native.check_command([str(TOOLS / 'mlir-translate'), '--mlir-to-llvmir', str(lowered), '-o', str(ll)], kind + '-translate', output)
        for level in ['0', '2']:
            obj, so = output / f'{kind}-O{level}.o', output / f'{kind}-O{level}.so'
            native.check_command([str(TOOLS / 'llc'), '-filetype=obj', '-relocation-model=pic', '-O' + level, str(ll), '-o', str(obj)], kind + level + '-codegen', output)
            native.check_command(['/usr/bin/cc', '-shared', str(obj), '-lm', '-o', str(so)], kind + level + '-link', output)
            library = ctypes.CDLL(str(so), mode=ctypes.RTLD_LOCAL)
            function = library.repro
            function.argtypes, function.restype = [ctypes.c_float], ctypes.c_float
            rows = []
            for case in CASES:
                actual = native.get_float_bits(function, case['input_bits'])
                match = ((actual & 0x7fffffff) > 0x7f800000) if case.get('expected_kind') == 'nan' else actual == case['expected_bits']
                rows.append({**case, 'actual_bits': actual, 'matches': match})
            bad = {row['name'] for row in rows if not row['matches']}
            results[kind + '-O' + level] = {'total': len(rows), 'mismatches': sorted(bad), 'observations': rows, 'library_sha256': sha(so)}
            expected = native.EXPECTED_TARGET_MISMATCHES if expect_red and kind == 'expanded' else set()
            assert bad == expected, f'{label}/{kind}/O{level}: {bad}'
            if expect_red and kind == 'expanded':
                assert all(f"0x{row['actual_bits']:08x}" == native.EXPECTED_WRONG_BITS[row['name']] for row in rows if not row['matches'])
    assert libm.fegetround() == 0
    (output / 'results.json').write_text(json.dumps(results, indent=2) + '\n')
    return {key: {'total': value['total'], 'mismatches': value['mismatches']} for key, value in results.items()}


def main():
    assert git('rev-parse', 'HEAD') == MANIFEST['base_commit']
    STATE['harness_commit'] = subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=ROOT, check=True, capture_output=True, text=True).stdout.strip()
    STATE['original_hashes'] = verify(set())
    STATE['backend_packages'] = native.inspect_dpkg_versions()
    save()
    invoke('configure', ['cmake', '-S', SOURCE / 'llvm', '-B', BUILD, '-G', 'Ninja',
           '-DCMAKE_BUILD_TYPE=Release', '-DLLVM_ENABLE_PROJECTS=mlir', '-DLLVM_ENABLE_ASSERTIONS=ON',
           '-DLLVM_EXTERNAL_PROJECTS=math-round-driver', f'-DLLVM_EXTERNAL_MATH_ROUND_DRIVER_SOURCE_DIR={ROOT / "llvm-round-driver"}',
           '-DLLVM_TARGETS_TO_BUILD=host', '-DLLVM_INCLUDE_TESTS=OFF', '-DMLIR_INCLUDE_TESTS=OFF',
           '-DLLVM_INCLUDE_EXAMPLES=OFF', '-DMLIR_INCLUDE_EXAMPLES=OFF', '-DLLVM_INCLUDE_BENCHMARKS=OFF',
           '-DMLIR_ENABLE_BINDINGS_PYTHON=OFF', '-DLLVM_INCLUDE_UTILS=ON', '-DLLVM_BUILD_UTILS=ON',
           '-DLLVM_PARALLEL_COMPILE_JOBS=2', '-DLLVM_PARALLEL_LINK_JOBS=1',
           '-DCMAKE_C_COMPILER_LAUNCHER=ccache', '-DCMAKE_CXX_COMPILER_LAUNCHER=ccache',
           '-DCMAKE_EXPORT_COMPILE_COMMANDS=ON'], timeout=900)
    cache = (BUILD / 'CMakeCache.txt').read_text()
    assert 'LLVM_ENABLE_ASSERTIONS:BOOL=ON' in cache
    (OUT / 'CMakeCache.txt').write_text(cache)
    build = ['cmake', '--build', BUILD, '--target', 'mlir-round-expand-opt', 'FileCheck', '--parallel', '2']
    invoke('original-build', build, timeout=150 * 60)
    invoke('source-driver-version', [BUILD / 'bin/mlir-round-expand-opt', '--version'])
    testfile = SOURCE / 'mlir/test/Dialect/Math/expand-math.mlir'
    filecheck('original-official-suite', testfile)
    STATE['phases']['original_native'] = native_probe('original', expect_red=True)
    STATE['phases']['original_hashes_after_tests'] = verify(set())
    save()
    patch('tests.patch')
    verify({'tests'})
    filecheck('regression-on-original', testfile, expect_failure=True)
    patch('production.patch')
    verify({'tests', 'production'})
    invoke('candidate-build', build, timeout=45 * 60)
    filecheck('candidate-official-suite', testfile)
    STATE['phases']['candidate_native'] = native_probe('candidate', expect_red=False)
    STATE['phases']['candidate_hashes_after_tests'] = verify({'tests', 'production'})
    save()
    cpp = SOURCE / MANIFEST['patches']['production.patch']['paths'][0]
    ranges = re.findall(r'^@@.*\+(\d+)(?:,(\d+))? @@', (PATCHES / 'production.patch').read_text(), re.M)
    cmd = ['clang-format-24', '--output-replacements-xml', *[f'--lines={start}:{int(start)+int(length or 1)-1}' for start, length in ranges if length != '0'], str(cpp)]
    invoke('changed-lines-format', cmd)
    replacements = ET.fromstring((OUT / 'changed-lines-format.log').read_text()).findall('replacement')
    STATE['format_replacements'] = len(replacements)
    save()
    assert not replacements, 'Changed source lines need clang-format24; functional results retained separately'
    STATE['completed_at_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    STATE['success'] = True
    save()
    print(json.dumps(STATE['phases'], indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        STATE['success'] = False
        STATE['error'] = f'{type(error).__name__}: {error}'
        save()
        raise
