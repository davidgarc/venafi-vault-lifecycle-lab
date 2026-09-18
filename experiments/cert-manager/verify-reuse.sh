#!/usr/bin/env bash
# Mutates only the lab Deployment; preserves original replica count on exit.
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
output=${1:-"$here/../../artifacts/cm-reuse"}
context=kind-venafi-lab-cm
mkdir -p "$output"
original=$(kubectl --context "$context" -n demo get deployment demo -o jsonpath='{.spec.replicas}')
restore() { kubectl --context "$context" -n demo scale deployment/demo --replicas="$original" >/dev/null; }
trap restore EXIT
kubectl --context "$context" -n demo rollout status deployment/demo --timeout=120s
python3 "$here/observe.py" --output "$output/before.json" > /dev/null
# Require enough time to distinguish restart reuse from normal scheduled renewal.
python3 - "$output/before.json" <<'PY'
import json, sys, time
evidence = json.load(open(sys.argv[1]))
assert evidence['pods'], 'No pods to test'
for pod in evidence['pods']:
    assert pod['certificate']['midpoint'] - time.time() > 180, 'Run reuse test with a fresh certificate, at least 3 minutes before midpoint'
PY
kubectl --context "$context" -n demo rollout restart deployment/demo
kubectl --context "$context" -n demo rollout status deployment/demo --timeout=120s
python3 "$here/observe.py" --before "$output/before.json" --expect reuse --output "$output/replacement.json" > /dev/null
kubectl --context "$context" -n demo scale deployment/demo --replicas=3
kubectl --context "$context" -n demo rollout status deployment/demo --timeout=120s
python3 "$here/observe.py" --before "$output/before.json" --expect reuse --output "$output/three-replicas.json" > /dev/null
python3 - "$output/before.json" "$output/replacement.json" "$output/three-replicas.json" <<'PY'
import json, sys
before, replaced, scaled = [json.load(open(p)) for p in sys.argv[1:]]
old = {p['pod_uid'] for p in before['pods']}
new = {p['pod_uid'] for p in replaced['pods']}
assert old.isdisjoint(new), 'Expected whole-pod replacement did not occur'
assert len(scaled['pods']) == 3, 'Expected exactly three replicas'
print('PASS: whole-pod replacement and three replicas reuse the certificate/key. Compare mock API counters separately.')
PY
