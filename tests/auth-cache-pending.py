#!/usr/bin/env python3
"""Run after auth-cache-config.py: simulate one pending TPP pickup per certificate.

Only the mock response is altered; plugin/SDK stay unmodified. Restores the mock
Deployment command afterward. This probes asynchronous issuance semantics, not a
claim of real TPP integration validation.
"""
import datetime
import json
import pathlib
import sys
import time
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import lab

SIMULATOR = '''import server
original_factory = server.handler_class
def pending_factory(store):
    handler = original_factory(store)
    original_respond = handler.respond
    seen = set()
    def respond(self, code, value, *args, **kwargs):
        if isinstance(value, dict) and value.get('CertificateData'):
            certificate = value['CertificateData']
            if certificate not in seen:
                seen.add(certificate)
                value = {'CertificateData': '', 'Status': 'Pending - research fixture'}
        return original_respond(self, code, value, *args, **kwargs)
    handler.respond = respond
    return handler
server.handler_class = pending_factory
server.main()
'''

def patch(command):
    lab.k('vault', '-n', 'lab', 'patch', 'deployment/mock-tpp', '--type=strategic', '-p',
          json.dumps({'spec': {'template': {'spec': {'containers': [{'name': 'mock-tpp', 'command': command}]}}}}), capture=True)
    lab.k('vault', '-n', 'lab', 'rollout', 'status', 'deployment/mock-tpp', '--timeout=120s', capture=True)
    lab.wait_http('http://127.0.0.1:18201/api/state')

def request(role, cn):
    before = lab.request('http://127.0.0.1:18201/api/state')['counters'].get('exp2', {})
    start = time.monotonic()
    try:
        value = lab.vault('venafi-agent/issue/' + role, {'common_name': cn, 'ttl': '1h'})['data']
        result = {'ok': True, 'serial': value['serial_number']}
    except urllib.error.HTTPError as exc:
        result = {'ok': False, 'status': exc.code, 'error': exc.read().decode()[:1500]}
    result['seconds'] = round(time.monotonic() - start, 3)
    after = lab.request('http://127.0.0.1:18201/api/state')['counters'].get('exp2', {})
    result['counter_delta'] = {k: after.get(k, 0) - before.get(k, 0) for k in set(before) | set(after)}
    return result

deployment = json.loads(lab.k('vault', '-n', 'lab', 'get', 'deployment/mock-tpp', '-o', 'json', capture=True))
original = next(c for c in deployment['spec']['template']['spec']['containers'] if c['name'] == 'mock-tpp').get('command')
report = {'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'fixture': 'First retrieval of each certificate returns HTTP 200 with empty CertificateData; next retrieval returns certificate.'}
try:
    patch(['python', '-c', SIMULATOR])
    stamp = str(int(time.time()))
    report['positive_timeout'] = request('auth-control', 'pending-positive-' + stamp + '.agent.test')
    report['zero_timeout'] = request('auth-zero-system', 'pending-zero-' + stamp + '.agent.test')
    report['checks'] = {
        'positive_timeout_polls_and_succeeds': report['positive_timeout']['ok'] and report['positive_timeout']['counter_delta']['retrieval'] == 2,
        'zero_timeout_returns_pending_after_one_pickup': not report['zero_timeout']['ok'] and 'pending' in report['zero_timeout'].get('error', '').lower() and report['zero_timeout']['counter_delta']['retrieval'] == 1,
    }
except Exception as exc:
    report['error'] = str(exc)
finally:
    try:
        patch(original)
    except Exception as exc:
        report['restore_error'] = str(exc)
    report['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report['passed'] = bool(report.get('checks')) and all(report['checks'].values()) and 'error' not in report and 'restore_error' not in report
    (ROOT / 'artifacts/auth-cache-research/pending.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps(report, indent=2))
sys.exit(0 if report['passed'] else 1)
