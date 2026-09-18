#!/usr/bin/env python3
"""Validate scoped teardown and fresh startup; finishes with the lab shut down."""
import datetime
import hashlib
import json
import pathlib
import socket
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import lab

report = {'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'checks': {}}

def containers():
    return dict(line.split(' ', 1) for line in lab.run('docker', 'ps', '-a', '--no-trunc', '--format', '{{.Names}} {{.ID}}', capture=True).splitlines())

def kubeconfig_hash():
    path = pathlib.Path.home() / '.kube/config'
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None

def clean_checks(label):
    current = containers()
    report['checks'][label + '_no_lab_containers'] = not any(name in current for name in ['venafi-lab-ui'] + [n + '-control-plane' for n in lab.NAMES.values()])
    report['checks'][label + '_credentials_removed'] = not any(p.is_file() for p in lab.STATE.iterdir())
    closed = {}
    for port in (18080, 18081, 18082, 18200, 18201):
        with socket.socket() as connection:
            connection.settimeout(1)
            closed[str(port)] = connection.connect_ex(('127.0.0.1', port)) != 0
    report[label + '_ports_closed'] = closed
    report['checks'][label + '_ports_closed'] = all(closed.values())
    report['checks'][label + '_existing_kind_preserved'] = 'kind-control-plane' not in baseline or current.get('kind-control-plane') == baseline['kind-control-plane']
    report['checks'][label + '_default_kubeconfig_preserved'] = kubeconfig_hash() == config_before

baseline = containers()
config_before = kubeconfig_hash()
report['containers_before'] = baseline
try:
    vault_before = baseline.get('venafi-lab-vault-control-plane')
    agent_node_before = baseline.get('venafi-lab-agent-control-plane')
    lab.down(['cm'])
    agent_after = lab.request('http://127.0.0.1:18082/cgi-bin/cert')
    report['checks']['partial_shutdown_preserves_vault'] = containers().get('venafi-lab-vault-control-plane') == vault_before and not lab.vault('sys/seal-status')['sealed']
    report['partial_shutdown_agent_certificate'] = agent_after
    report['checks']['partial_shutdown_preserves_agent'] = containers().get('venafi-lab-agent-control-plane') == agent_node_before and agent_after['valid']
    lab.down(['cm', 'agent', 'vault'])
    clean_checks('first_shutdown')
    if not all(report['checks'].values()):
        raise RuntimeError('Initial cleanup or isolation check failed')
    lab.run(sys.executable, ROOT / 'scripts/lab.py', 'up')
    state = lab.request('http://127.0.0.1:18080/api/state')
    report['fresh_start'] = state
    for name in ('cm', 'agent'):
        pods = state['experiments'][name].get('pods', [])
        report['checks']['fresh_' + name + '_ready'] = len(pods) == 1 and all(p.get('certificate', {}).get('valid') for p in pods)
    report['checks']['fresh_vault_container'] = containers().get('venafi-lab-vault-control-plane') != vault_before
except Exception as exc:
    report['error'] = str(exc)
finally:
    try:
        lab.down(['cm', 'agent', 'vault'])
        clean_checks('final_shutdown')
        report['containers_after'] = containers()
    except Exception as exc:
        report['cleanup_error'] = str(exc)
    report['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report['passed'] = bool(report['checks']) and all(report['checks'].values()) and 'error' not in report and 'cleanup_error' not in report
    (ROOT / 'artifacts/cleanup-cycle.json').write_text(json.dumps(report, indent=2) + '\n')

print(json.dumps({'passed': report['passed'], 'checks': report['checks'], 'error': report.get('error'), 'cleanup_error': report.get('cleanup_error')}))
sys.exit(0 if report['passed'] else 1)
