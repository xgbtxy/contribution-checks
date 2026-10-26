"""Validate one pinned XSF patch; this is not an upstream submission."""
import datetime
import hashlib
import json
import os
import pathlib
import re
import subprocess
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parent
SOURCE = ROOT / 'source'
ARTIFACTS = ROOT / 'artifacts'
ARTIFACTS.mkdir(exist_ok=True)
MANIFEST = json.loads((ROOT / 'xsf-gammainc/manifest.json').read_text())
RECEIPT = {'started': datetime.datetime.now(datetime.timezone.utc).isoformat(),
           'manifest': MANIFEST, 'commands': []}

def save():
    (ARTIFACTS / 'receipt.json').write_text(json.dumps(RECEIPT, indent=2))

def run(name, command):
    print(f'{name}: {command}', flush=True)
    start = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with (ARTIFACTS / f'{name}.log').open('w') as log:
        result = subprocess.run(command, cwd=SOURCE, stdout=log, stderr=subprocess.STDOUT,
                                timeout=2700)
    RECEIPT['commands'].append({'name': name, 'command': command, 'exit': result.returncode,
                                'started': start})
    save()
    print(f'{name}: exit={result.returncode}', flush=True)
    return result.returncode

def git(*args):
    return subprocess.check_output(['git', *args], cwd=SOURCE, text=True).strip()

def xml_result(name):
    root = ET.parse(ARTIFACTS / f'{name}.xml').getroot()
    cases = list(root.iter('testcase'))
    assert cases, f'{name}: no tests recorded'
    result = {'tests': len(cases),
              'failed': sorted(c.attrib['name'] for c in cases
                               if c.find('failure') is not None or c.find('error') is not None),
              'skipped': sum(c.find('skipped') is not None for c in cases)}
    RECEIPT[name] = result
    save()
    return result

def apply(name, expected_files):
    patch = ROOT / 'xsf-gammainc' / f'{name}.patch'
    assert hashlib.sha256(patch.read_bytes()).hexdigest() == MANIFEST['patches'][name]
    subprocess.run(['git', 'apply', '--check', str(patch)], cwd=SOURCE, check=True)
    subprocess.run(['git', 'apply', str(patch)], cwd=SOURCE, check=True)
    for file in expected_files:
        assert git('hash-object', file) == MANIFEST['candidate_blobs'][file], file

def tests(name, *extra):
    exit_code = run(name, ['pixi', 'run', '--locked', 'tests', *extra,
                           '--output-junit', str(ARTIFACTS / f'{name}.xml')])
    assert exit_code in (0, 8), f'{name}: failed before a normal CTest result'
    return xml_result(name), exit_code

def rebuild_environment(phase):
    # These project-local caches belong to this disposable validation runner.
    assert os.environ.get('GITHUB_REPOSITORY') == 'xgbtxy/contribution-checks'
    assert SOURCE.resolve().parent == pathlib.Path(os.environ['GITHUB_WORKSPACE']).resolve()
    assert run(phase + '-clean-environment', ['pixi', 'clean', '--environment', 'tests-release']) == 0
    assert run(phase + '-clean-build', ['pixi', 'clean', '--build']) == 0

def main():
    assert git('rev-parse', 'HEAD') == MANIFEST['base']
    assert not git('status', '--porcelain')
    assert git('hash-object', MANIFEST['production']) == MANIFEST['base_blob']
    save()
    baseline, _ = tests('baseline', '-j', '2')
    apply('tests', [MANIFEST['test']])
    subprocess.run(['git', 'add', '-N', MANIFEST['test']], cwd=SOURCE, check=True)
    rebuild_environment('red')
    red, red_code = tests('red', '-R', '^xsf_tests_test_gammainc$')
    assert red_code == 8 and red['tests'] == 1
    assert red['failed'] == ['xsf_tests_test_gammainc']
    red_log = (ARTIFACTS / 'red.log').read_text()
    assert re.search(r'test cases:\s*4\s*\|\s*(?:0 passed\s*\|\s*)?4 failed', red_log)
    apply('production', [MANIFEST['production']])
    rebuild_environment('candidate')
    candidate, candidate_code = tests('candidate', '-j', '2')
    assert candidate['tests'] == baseline['tests'] + 1
    assert set(candidate['failed']) <= set(baseline['failed'])
    assert 'xsf_tests_test_gammainc' not in candidate['failed']
    assert run('format', ['pixi', 'run', '--locked', 'format-dry-error']) == 0
    assert git('diff', '--check') == ''
    changed = set(git('diff', '--name-only').splitlines())
    assert changed == set(MANIFEST['candidate_blobs'])
    for file, blob in MANIFEST['candidate_blobs'].items():
        assert git('hash-object', file) == blob
    RECEIPT['candidate_all_tests_passed'] = candidate_code == 0
    RECEIPT['validation_passed'] = True
    save()

try:
    main()
except BaseException as exc:
    RECEIPT['validation_passed'] = False
    RECEIPT['error'] = repr(exc)
    save()
    raise
