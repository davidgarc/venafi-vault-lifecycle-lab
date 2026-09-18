#!/usr/bin/env bash
set -euo pipefail
here=$(cd "$(dirname "$0")" && pwd)
context=kind-venafi-lab-cm
vault_addr=${VAULT_ADDR:-http://venafi-lab-vault-control-plane:30200}
[[ "$vault_addr" =~ ^https?://[a-zA-Z0-9.:-]+$ ]] || { echo 'Invalid VAULT_ADDR' >&2; exit 1; }
kubectl --context "$context" get nodes >/dev/null
helm upgrade --install cert-manager oci://quay.io/jetstack/charts/cert-manager \
  --version v1.21.2 --kube-context "$context" --namespace cert-manager --create-namespace \
  --values "$here/values.yaml" --wait --timeout 5m
kubectl --context "$context" apply -f "$here/resources.yaml"
cat <<EOF | kubectl --context "$context" apply -f -
apiVersion: cert-manager.io/v1
kind: Issuer
metadata:
  name: vault
  namespace: demo
spec:
  vault:
    server: ${vault_addr}
    path: venafi-cm/sign/demo
    auth:
      kubernetes:
        mountPath: /v1/auth/kubernetes-cm
        role: demo
        serviceAccountRef:
          name: vault-issuer
EOF
kubectl --context "$context" -n demo wait --for=condition=Ready issuer/vault --timeout=120s
kubectl --context "$context" -n demo wait --for=condition=Ready certificate/demo-tls --timeout=180s
kubectl --context "$context" -n demo rollout status deployment/demo --timeout=180s
