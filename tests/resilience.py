#!/usr/bin/env python3
"""Scoped native restart/outage evidence; run after rotation with >=180s to midpoint."""
import datetime, json, pathlib, sys, time
ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
import lab
sys.path.insert(0, str(ROOT / 'experiments/agent'))
from observe import snapshot
from verify import counters, delta, identity, ready

MOCK = 'http://127.0.0.1:18201/api/state'
report = {'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'checks': {}, 'limitations': []}

def scoped(which, *args):
    return lab.k(which, *args, capture=True)

def safe_window(state):
    deadline = time.monotonic() + 780
    while True:
        certs = [p['certificate'] for p in state['pods'] if 'certificate' in p]
        if certs and len(certs) == len(state['pods']) and min([c['midpoint'] for c in certs]+[cm_public()['midpoint']]) - time.time() >= 180:
            return state
        if time.monotonic() >= deadline:
            raise RuntimeError('No >=180-second safe window observed after waiting for native rotation')
        # Observe only: never issue a request to force renewal or change certificate TTL.
        time.sleep(10)
        state = snapshot()

def unseal(timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = lab.vault('sys/seal-status')
            if state['sealed']:
                # Use only local ignored credential state, never include it in evidence.
                key = json.loads((lab.STATE / 'vault-init.json').read_text())['keys_base64'][0]
                lab.vault('sys/unseal', {'key': key})
            if not lab.vault('sys/seal-status')['sealed']:
                return
        except Exception:
            pass
        time.sleep(2)
    raise RuntimeError('Vault did not recover/unseal')

def cm_public():
    return lab.request('http://127.0.0.1:18081/cgi-bin/cert')

mutated = False
try:
    before = safe_window(snapshot())
    original_count = len(before['pods'])
    counts_before = counters(MOCK)
    cm_before = cm_public()
    report.update(before=before, cm_before=cm_before, counters_before=counts_before)
    mutated = True
    scoped('vault', '-n', 'lab', 'delete', 'pod', '-l', 'app=vault', '--wait=true', '--timeout=90s')
    # Wait for deletion/replacement so an old still-running pod cannot satisfy unseal.
    scoped('vault', '-n', 'lab', 'rollout', 'status', 'deployment/vault', '--timeout=120s')
    time.sleep(3)
    unseal()
    # Existing files survive Vault outage; a fresh emptyDir proves persisted cache.
    selected = before['pods'][0]['name']
    scoped('agent', '-n', 'demo', 'delete', 'pod', selected, '--wait=true', '--timeout=90s')
    after_vault = ready(original_count)
    counts_vault = counters(MOCK)
    report.update(after_vault_restart=after_vault, vault_restart_counter_delta=delta(counts_before, counts_vault))
    report['checks']['vault_restart_cache_identity_reused'] = identity(before) == identity(after_vault)
    for op in ('issuance', 'retrieval'):
        report['checks']['vault_restart_no_' + op] = report['vault_restart_counter_delta'].get('exp2', {}).get(op, 0) == 0
    report['checks']['cm_identity_survives_vault_restart'] = cm_before['serial'] == cm_public()['serial']
    after_vault = safe_window(after_vault)
    report["outage_baseline"] = after_vault
    cm_before = cm_public()

    # Take the complete simulator service offline, including upstream authentication.
    scoped('vault', '-n', 'lab', 'scale', 'deployment/mock-tpp', '--replicas=0')
    scoped('vault', '-n', 'lab', 'wait', '--for=delete', 'pod', '-l', 'app=mock-tpp', '--timeout=90s')
    during = snapshot()
    report['during_outage_existing'] = during
    report['checks']['existing_files_remain_usable_during_outage'] = identity(after_vault) == identity(during) and all(p.get('certificate_key_match') and p.get('certificate', {}).get('valid') for p in during['pods'])
    report['checks']['cm_existing_secret_usable_during_outage'] = cm_before['serial'] == cm_public()['serial']
    cm_pods=json.loads(scoped('cm','-n','demo','get','pods','-l','app=demo','-o','json'))['items']
    cm_old_uids={p['metadata']['uid'] for p in cm_pods}
    cm_selected=next(p['metadata']['name'] for p in cm_pods if not p['metadata'].get('deletionTimestamp'))
    scoped('cm','-n','demo','delete','pod',cm_selected,'--wait=true','--timeout=90s')
    scoped('cm','-n','demo','rollout','status','deployment/demo','--timeout=120s')
    cm_after_pods=json.loads(scoped('cm','-n','demo','get','pods','-l','app=demo','-o','json'))['items']
    cm_fresh=[p for p in cm_after_pods if p['metadata']['uid'] not in cm_old_uids and not p['metadata'].get('deletionTimestamp')]
    cm_observed=[{'uid':p['metadata']['uid'],'certificate':json.loads(scoped('cm','-n','demo','exec',p['metadata']['name'],'-c','app','--','wget','-qO-','http://127.0.0.1:8080/cgi-bin/cert'))} for p in cm_fresh]
    report['cm_replacement_observations']=cm_observed
    report['checks']['cm_replacement_reuses_secret_during_outage'] = bool(cm_observed) and all(p['certificate'].get('valid') and (p['certificate']['serial'],p['certificate']['public_key_sha256']) == (cm_before['serial'],cm_before['public_key_sha256']) for p in cm_observed)
    selected = during['pods'][0]['name']
    old_uids = {p['uid'] for p in during['pods']}
    scoped('agent', '-n', 'demo', 'delete', 'pod', selected, '--wait=true', '--timeout=90s')
    time.sleep(10)
    blocked = snapshot()
    report['during_outage_replacement'] = blocked
    fresh = [p for p in blocked['pods'] if p['uid'] not in old_uids]
    waiting = any(not p.get('certificate') for p in fresh)
    report['new_pod_blocked_during_upstream_outage'] = waiting
    if waiting:
        report['limitations'].append('New Agent pods could not recover the cached certificate while complete upstream TPP service, including authentication, was unavailable; existing files remained usable.')
    else:
        report['limitations'].append('Replacement was not observed blocked in this bounded outage window; this does not guarantee outage-independent cache retrieval.')
except Exception as exc:
    report['error'] = str(exc)
finally:
    if mutated:
        recovery_errors = []
        try:
            scoped('vault', '-n', 'lab', 'scale', 'deployment/mock-tpp', '--replicas=1')
            scoped('vault', '-n', 'lab', 'rollout', 'status', 'deployment/mock-tpp', '--timeout=180s')
            unseal()
            recovered = ready(original_count)
            report['recovered'] = recovered
            report['checks']['all_agent_pods_recovered'] = all(p.get('certificate_key_match') for p in recovered['pods'])
            report['checks']['cm_recovered'] = cm_public().get('ready', False)
            report['counters_after'] = counters(MOCK)
        except Exception as exc:
            recovery_errors.append(str(exc))
        report['recovery_errors'] = recovery_errors
    report['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    report['passed'] = bool(report['checks']) and all(report['checks'].values()) and not report.get('recovery_errors') and 'error' not in report
    print(json.dumps(report, indent=2), flush=True)

sys.exit(0 if report['passed'] else 1)
