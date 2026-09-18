#!/usr/bin/env python3
"""Explicit native cold/warm concurrency test; no issuance or renewal orchestration."""
import argparse, datetime, json, subprocess, sys, time, urllib.request
from observe import k, snapshot

def counters(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)['counters']

def ready(replicas):
    k('rollout', 'status', 'deployment/demo', '--timeout=240s')
    end = time.monotonic() + 120
    while time.monotonic() < end:
        state = snapshot()
        if len(state['pods']) == replicas and all(p.get('certificate', {}).get('ready') and p.get('certificate', {}).get('valid') and p.get('certificate_key_match') for p in state['pods']):
            return state
        time.sleep(3)
    raise RuntimeError('Application replicas did not all become ready with matching keys')

def delta(before, after):
    return {owner: {op: after.get(owner, {}).get(op, 0) - before.get(owner, {}).get(op, 0)
                    for op in set(before.get(owner, {})) | set(after.get(owner, {}))}
            for owner in set(before) | set(after)}

def identity(state):
    return {(p['certificate']['serial'], p['certificate']['public_key_sha256']) for p in state['pods'] if 'certificate' in p}

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--phase', choices=('cold', 'warm'), required=True)
    parser.add_argument('--replicas', type=int, default=3)
    parser.add_argument('--mock-state-url', default='http://127.0.0.1:18201/api/state')
    args = parser.parse_args()
    if not 2 <= args.replicas <= 5:
        parser.error('replicas must be between two and five')
    report = {'started_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'phase': args.phase, 'checks': {}}
    try:
        before = snapshot()
        counts_before = counters(args.mock_state_url)
        report.update(before=before, counters_before=counts_before)
        if args.phase == 'cold':
            # Never destroy cache to fabricate a cold result. A fresh lab is required.
            if before['pods'] or counts_before.get('exp2', {}).get('issuance', 0):
                raise RuntimeError('Cold test requires zero application pods and a fresh exp2 inventory; install with AGENT_INITIAL_REPLICAS=0 on a fresh lab')
        else:
            if len(identity(before)) != 1 or not before['pods']:
                raise RuntimeError('Warm test requires an existing shared identity')
            cert = before['pods'][0]['certificate']
            if cert['midpoint'] - time.time() < 90:
                raise RuntimeError('Warm test too close to/past midpoint; run after natural rotation to avoid conflating renewal with reuse')
        if args.phase == 'warm':
            # Start all replicas together against the retained warm plugin cache.
            k('scale', 'deployment/demo', '--replicas=0')
            k('wait', '--for=delete', 'pod', '-l', 'app=demo', '--timeout=120s')
        k('scale', 'deployment/demo', '--replicas=' + str(args.replicas))
        after = ready(args.replicas)
        counts_after = counters(args.mock_state_url)
        changes = delta(counts_before, counts_after)
        report.update(after=after, counters_after=counts_after, counter_delta=changes)
        report['checks']['replicas_share_certificate_and_key'] = len(identity(after)) == 1
        report['checks']['all_pairs_match'] = all(p.get('certificate_key_match') for p in after['pods'])
        report['checks']['issuance_count'] = changes.get('exp2', {}).get('issuance', 0) == (1 if args.phase == 'cold' else 0)
        if args.phase == 'warm':
            report['checks']['original_identity_reused'] = identity(before) == identity(after)
            report['checks']['no_certificate_retrieval'] = changes.get('exp2', {}).get('retrieval', 0) == 0
            # All fresh emptyDirs now exercise the persisted plugin cache.
            k('rollout', 'restart', 'deployment/demo')
            replaced = ready(args.replicas)
            replaced_counts = counters(args.mock_state_url)
            report.update(replaced=replaced, replacement_counter_delta=delta(counts_after, replaced_counts))
            report['checks']['replacement_identity_reused'] = identity(after) == identity(replaced)
            report['checks']['pod_uids_replaced'] = not ({p['uid'] for p in after['pods']} & {p['uid'] for p in replaced['pods']})
            for op in ('issuance', 'retrieval'):
                report['checks']['replacement_no_' + op] = report['replacement_counter_delta'].get('exp2', {}).get(op, 0) == 0
        report['passed'] = all(report['checks'].values())
    except Exception as exc:
        report.update(passed=False, error=str(exc))
    report['finished_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    print(json.dumps(report, indent=2))
    sys.exit(0 if report['passed'] else 1)
