#!/usr/bin/env python3
"""Stop only one app CRI container; prove pod/file identity survives restart."""
import json, subprocess, sys, time
from observe import k, snapshot
from verify import counters, delta, identity
report = {}
try:
    before = snapshot()
    counts_before = counters('http://127.0.0.1:18201/api/state')
    selected = before['pods'][0]
    if selected['certificate']['midpoint'] - time.time() < 45:
        raise RuntimeError('Too near midpoint for unambiguous restart test')
    pod = selected['name']
    status = json.loads(k('get', 'pod', pod, '-o', 'json'))
    container = next(c for c in status['status']['containerStatuses'] if c['name'] == 'app')
    node = status['spec']['nodeName']
    assert node == 'venafi-lab-agent-control-plane'
    cid = container['containerID'].removeprefix('containerd://')
    assert cid.isalnum()
    subprocess.run(['docker', 'exec', node, 'crictl', 'stop', '--timeout', '1', cid], check=True, stdout=subprocess.DEVNULL)
    for attempt in range(45):
        current = json.loads(k('get', 'pod', pod, '-o', 'json'))
        app = next(c for c in current['status']['containerStatuses'] if c['name'] == 'app')
        if app['ready'] and app['restartCount'] > container['restartCount']:
            break
        time.sleep(2)
    else:
        raise RuntimeError('App container did not recover')
    after = snapshot()
    changes = delta(counts_before, counters('http://127.0.0.1:18201/api/state'))
    checks = {'same_pod_uid': current['metadata']['uid'] == status['metadata']['uid'],
              'restart_count_increased': app['restartCount'] > container['restartCount'],
              'same_identity': identity(before) == identity(after),
              'all_pairs_match': all(p.get('certificate_key_match') for p in after['pods']),
              'no_issuance': changes['exp2']['issuance'] == 0,
              'no_retrieval': changes['exp2']['retrieval'] == 0}
    report = {'passed': all(checks.values()), 'checks': checks, 'before': before, 'after': after,
              'counter_delta': changes, 'restart_count_before': container['restartCount'], 'restart_count_after': app['restartCount']}
except Exception as exc:
    report.update(passed=False,error=str(exc))
print(json.dumps(report,indent=2))
sys.exit(0 if report['passed'] else 1)
