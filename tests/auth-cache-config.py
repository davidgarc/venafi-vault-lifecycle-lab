#!/usr/bin/env python3
"""Opt-in research: compare auth/cache configuration on a disposable running Agent lab.

Mutates the demo role, installs the mock CA in the Vault container system trust,
stops TPP temporarily, and replaces demo pods. Does not patch plugin or SDK code.
Restores the default role, mock availability and original replica count at exit.
Run on a disposable lab and use make down afterward to discard the added CA.
"""
import datetime
import hashlib
import json
import pathlib
import sys
import time
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import lab
sys.path.insert(0, str(ROOT / 'experiments/agent'))
from observe import snapshot
from verify import ready, counters, delta

OUT = ROOT / 'artifacts/auth-cache-research'
OUT.mkdir(exist_ok=True)
report = {'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'cases': {}, 'checks': {}}
MOUNT = 'venafi-agent'
MOCK = 'http://127.0.0.1:18201/api/state'

def configure(name, timeout, secret='mock'):
    role = {'venafi_secret': secret, 'ttl': '1h', 'max_ttl': '1h', 'generate_lease': False,
            'store_by': 'hash', 'store_pkey': True, 'no_store': False,
            'min_cert_time_left': '360s', 'server_timeout': timeout}
    lab.vault(MOUNT + '/roles/' + name, role)
    stored = lab.vault(MOUNT + '/roles/' + name)['data']
    return {k: stored.get(k) for k in role}

def issue(role, cn, **extra):
    start = time.monotonic()
    try:
        response = lab.vault(MOUNT + '/issue/' + role, {'common_name': cn, 'ttl': '1h', **extra})['data']
        # Keep only non-secret identity evidence. Compare private-key bytes by digest.
        result = {'ok': True, 'serial': response['serial_number'],
                  'certificate_sha256': hashlib.sha256(response['certificate'].encode()).hexdigest(),
                  'private_key_sha256': hashlib.sha256(response['private_key'].encode()).hexdigest()}
    except urllib.error.HTTPError as exc:
        result = {'ok': False, 'http_status': exc.code, 'error': exc.read().decode()[:1500]}
    except Exception as exc:
        result = {'ok': False, 'error_type': type(exc).__name__, 'error': str(exc)[:500]}
    result['seconds'] = round(time.monotonic() - start, 3)
    return result

def same(a, b):
    return a.get('ok') and b.get('ok') and all(a[k] == b[k] for k in ('serial', 'certificate_sha256', 'private_key_sha256'))

def case(name, role, cn, **extra):
    before = counters(MOCK)
    result = issue(role, cn, **extra)
    result['counter_delta'] = delta(before, counters(MOCK))
    report['cases'][name] = result
    print(name, json.dumps(result), flush=True)
    return result

deployment = json.loads(lab.k('agent', '-n', 'demo', 'get', 'deployment/demo', '-o', 'json', capture=True))
replicas = deployment['spec']['replicas']
original_role = lab.vault(MOUNT + '/roles/demo')['data']
mutated = False
try:
    baseline = ready(replicas)
    report['initial_pods'] = baseline
    mutated = True
    lab.k('agent', '-n', 'demo', 'scale', 'deployment/demo', '--replicas=0', capture=True)
    lab.k('agent', '-n', 'demo', 'wait', '--for=delete', 'pod', '-l', 'app=demo', '--timeout=120s', capture=True)
    report['roles'] = {'control': configure('auth-control', '3s'), 'zero_bundle': configure('auth-zero-bundle', '0s')}
    # Seed through the ordinary authenticated request path with normal TLS verification.
    run_id = str(int(time.time()))
    cn = 'auth-cache-' + run_id + '.agent.test'
    seed = case('seed_control', 'auth-control', cn)
    assert seed['ok'], 'Control issuance failed'
    positive = case('warm_positive_timeout', 'auth-control', cn)
    report['checks']['control_cache_hit_still_authenticates'] = same(seed, positive) and positive['counter_delta']['exp2']['auth'] == 1
    zero_bundle = case('zero_with_explicit_bundle', 'auth-zero-bundle', cn)
    panic_logs = lab.k('vault', '-n', 'lab', 'logs', 'deployment/vault', '--tail=120', capture=True)
    zero_bundle['nil_pointer_panic_observed'] = 'panic: runtime error: invalid memory address or nil pointer dereference' in panic_logs
    report['checks']['zero_bundle_reproduces_failure'] = not zero_bundle['ok'] and zero_bundle.get('http_status') == 500 and ('rpc error' in zero_bundle.get('error', '') or 'plugin is shut down' in zero_bundle.get('error', ''))
    # TLS verification remains enabled; use the standard OS trust-store installation.
    lab.k('vault', '-n', 'lab', 'exec', 'deployment/vault', '--', 'sh', '-c',
          'set -eu; test -f /etc/ssl/certs/ca-certificates.crt; cat /vault/lab-config/ca.pem >> /etc/ssl/certs/ca-certificates.crt', capture=True)
    lab.vault(MOUNT + '/venafi/mock-system', {'url': 'https://mock-tpp:8443', 'zone': '\\VED\\Policy\\agent', 'access_token': 'lab-token-agent'})
    report['roles']['zero_system'] = configure('auth-zero-system', '0s', 'mock-system')
    zero_warm = case('warm_zero_system_trust', 'auth-zero-system', cn)
    report['checks']['zero_warm_has_no_upstream_calls'] = same(seed, zero_warm) and all(v == 0 for v in zero_warm['counter_delta'].get('exp2', {}).values())
    cold = case('cold_zero_system_trust', 'auth-zero-system', 'cold-auth-' + run_id + '.agent.test')
    report['checks']['zero_cold_issues_with_verified_tls'] = cold['ok'] and cold['counter_delta']['exp2']['issuance'] == 1 and cold['counter_delta']['exp2']['auth'] == 0
    renewed = case('threshold_reissue_zero_system_trust', 'auth-zero-system', cn, min_cert_time_left='1h')
    report['checks']['zero_threshold_can_reissue'] = renewed['ok'] and renewed['serial'] != seed['serial'] and renewed['counter_delta']['exp2']['issuance'] == 1
    # Confirm disabling the eager lookup does not make upstream issuance accept a bad token.
    lab.vault(MOUNT + '/venafi/mock-invalid', {'url': 'https://mock-tpp:8443', 'zone': '\\VED\\Policy\\agent', 'access_token': 'deliberately-invalid-research-token'})
    configure('auth-zero-invalid', '0s', 'mock-invalid')
    invalid_warm = case('invalid_upstream_token_warm_cache', 'auth-zero-invalid', cn)
    invalid_cold = case('invalid_upstream_token_cold', 'auth-zero-invalid', 'invalid-cold-' + run_id + '.agent.test')
    report['checks']['cached_return_does_not_validate_upstream_token'] = same(renewed, invalid_warm)
    report['checks']['cold_still_requires_valid_upstream_token'] = not invalid_cold['ok'] and ('401' in invalid_cold.get('error', '') or 'unauthorized' in invalid_cold.get('error', '').lower())
    # Change only the native role/credential selection used by the existing Injector manifest.
    configure('demo', '0s', 'mock-system')
    demo_seed = issue('demo', 'demo.agent.test')
    assert demo_seed['ok']
    report['demo_cached_identity'] = demo_seed
    before_outage = counters(MOCK)
    lab.k('vault', '-n', 'lab', 'scale', 'deployment/mock-tpp', '--replicas=0', capture=True)
    lab.k('vault', '-n', 'lab', 'wait', '--for=delete', 'pod', '-l', 'app=mock-tpp', '--timeout=90s', capture=True)
    report['mock_offline_desired_replicas'] = json.loads(lab.k('vault', '-n', 'lab', 'get', 'deployment/mock-tpp', '-o', 'json', capture=True))['spec']['replicas']
    report['cases']['offline_positive_timeout'] = issue('auth-control', cn)
    offline = [issue('auth-zero-system', cn) for _ in range(3)]
    report['cases']['offline_zero_system_trust'] = offline
    report['checks']['positive_timeout_fails_offline'] = not report['cases']['offline_positive_timeout']['ok']
    report['checks']['zero_warm_succeeds_offline_three_times'] = all(same(renewed, r) for r in offline)
    lab.k('agent', '-n', 'demo', 'scale', 'deployment/demo', '--replicas=3', capture=True)
    pods = ready(3)
    report['offline_fresh_injected_pods'] = pods
    report['checks']['three_fresh_pods_start_offline_with_cached_identity'] = len(pods['pods']) == 3 and all(p['certificate_key_match'] and p['certificate']['valid'] and p['certificate']['serial'].lower().replace(':', '').lstrip('0') == demo_seed['serial'].lower().replace(':', '').lstrip('0') for p in pods['pods'])
    report['checks']['no_application_tls_secret'] = not any(x.get('pem_fields') or x.get('type') == 'kubernetes.io/tls' for x in pods['inventory'] if x['kind'] == 'secrets')
    lab.k('vault', '-n', 'lab', 'scale', 'deployment/mock-tpp', '--replicas=1', capture=True)
    lab.k('vault', '-n', 'lab', 'rollout', 'status', 'deployment/mock-tpp', '--timeout=120s', capture=True)
    report['offline_counter_delta_after_restore'] = delta(before_outage, counters(MOCK))
    report['checks']['no_upstream_operations_during_offline_pod_start'] = all(v == 0 for v in report['offline_counter_delta_after_restore'].get('exp2', {}).values())
except Exception as exc:
    report['error'] = str(exc)
finally:
    if mutated:
        errors = []
        actions = [
            lambda: lab.k('vault', '-n', 'lab', 'scale', 'deployment/mock-tpp', '--replicas=1', capture=True),
            lambda: lab.k('vault', '-n', 'lab', 'rollout', 'status', 'deployment/mock-tpp', '--timeout=120s', capture=True),
            lambda: lab.vault(MOUNT + '/roles/demo', original_role),
            lambda: lab.k('agent', '-n', 'demo', 'scale', 'deployment/demo', '--replicas=' + str(replicas), capture=True),
            lambda: ready(replicas),
        ]
        for action in actions:
            try:
                action()
            except Exception as exc:
                errors.append(str(exc))
        if errors:
            report['restore_error'] = errors
    report['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report['passed'] = bool(report['checks']) and all(report['checks'].values()) and 'error' not in report and 'restore_error' not in report
    (OUT / 'results.json').write_text(json.dumps(report, indent=2) + '\n')
print(json.dumps({'passed': report['passed'], 'checks': report['checks'], 'error': report.get('error')}))
sys.exit(0 if report['passed'] else 1)
