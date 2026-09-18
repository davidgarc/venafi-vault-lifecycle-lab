#!/usr/bin/env python3
"""Reproducible full validation from a running lab. Leaves it running for review."""
import json, os, pathlib, subprocess, sys, time, urllib.request
ROOT=pathlib.Path(__file__).resolve().parents[1]
os.chdir(ROOT);os.environ['KUBECONFIG']=str(ROOT/'.state/kubeconfig')
def run(*args):subprocess.run(args,check=True)
# Start bounded mutation tests with a comfortable window before actual renewal.
for attempt in range(100):
    with urllib.request.urlopen('http://127.0.0.1:18080/api/state',timeout=40) as r:s=json.load(r)
    certs=[p.get('certificate',{}) for w in ('cm','agent') for p in s['experiments'][w].get('pods',[])]
    if len(certs)>=2 and all(c.get('valid') and c['midpoint']-time.time()>180 for c in certs):break
    time.sleep(10)
else:raise RuntimeError('No ready pre-midpoint validation window')
run('kubectl','--context','kind-venafi-lab-cm','-n','demo','scale','deployment/demo','--replicas=1')
run('kubectl','--context','kind-venafi-lab-cm','-n','demo','rollout','status','deployment/demo','--timeout=120s')
run('bash','experiments/cert-manager/verify-reuse.sh')
run(sys.executable,'experiments/cert-manager/verify-container.py')
with open('artifacts/agent-warm.json','w') as f:
    subprocess.run([sys.executable,'experiments/agent/verify.py','--phase','warm'],stdout=f,check=True)
with open('artifacts/agent-container.json','w') as f:
    subprocess.run([sys.executable,'experiments/agent/verify-container.py'],stdout=f,check=True)
run(sys.executable,'tests/auth-isolation.py')
run('kubectl','--context','kind-venafi-lab-cm','-n','demo','scale','deployment/demo','--replicas=3')
run('kubectl','--context','kind-venafi-lab-cm','-n','demo','rollout','status','deployment/demo','--timeout=120s')
natural = subprocess.run([sys.executable,'tests/verify.py']).returncode
if natural:
    print('Native renewal gate failed; preserving evidence and continuing recovery tests. No lifecycle workaround applied.',flush=True)
with open('artifacts/resilience.json','w') as f:
    subprocess.run([sys.executable,'tests/resilience.py'],stdout=f,check=True)
run(sys.executable,'tests/demand.py')
print('Validation finished. Evidence is in artifacts/. Run make down to remove the lab.')
sys.exit(natural)
