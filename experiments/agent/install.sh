#!/usr/bin/env bash
set -euo pipefail
HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
CONTEXT=kind-venafi-lab-agent
: "${VAULT_ADDR:?Set VAULT_ADDR to the Vault node IP and port, e.g. http://172.18.0.2:30200}"
[[ "$VAULT_ADDR" == http://*:30200 ]] || { echo 'Expected isolated lab Vault HTTP NodePort 30200' >&2; exit 1; }
kubectl --context "$CONTEXT" get node >/dev/null
# Inherit KUBECONFIG. No switch of the caller's current context.
# Helm 4 SSA conflicts with the injector's managed webhook caBundle on repeat runs.
HELM_APPLY=()
if helm upgrade --help | grep -- '--server-side' >/dev/null; then
  HELM_APPLY+=(--server-side=false)
fi
helm upgrade --install vault-injector vault "${HELM_APPLY[@]}" \
  --repo https://helm.releases.hashicorp.com --version 0.30.1 \
  --kube-context "$CONTEXT" --namespace vault-injector --create-namespace \
  -f "$HERE/helm-values.yaml" --set-string "global.externalVaultAddr=$VAULT_ADDR" \
  --wait --timeout 180s
# Set replicas before first admission so cold-concurrency tests can start at zero.
AGENT_INITIAL_REPLICAS=${AGENT_INITIAL_REPLICAS:-1}
[[ "$AGENT_INITIAL_REPLICAS" =~ ^[0-9]+$ ]] || exit 1
kubectl --context "$CONTEXT" create --dry-run=client -f "$HERE/demo.yaml" -o json | \
  python3 "$HERE/prepare-manifests.py" "$AGENT_INITIAL_REPLICAS" | \
  kubectl --context "$CONTEXT" apply -f -
kubectl --context "$CONTEXT" -n demo rollout status deployment/demo --timeout=240s
