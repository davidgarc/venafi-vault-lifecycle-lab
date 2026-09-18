# Experiment 1: cert-manager owns lifecycle

The namespaced `Issuer/vault` signs cert-manager's CSR through the unmodified plugin at `venafi-cm/sign/demo`. cert-manager stores its certificate and private key in `Secret/demo-tls`, mounted as a complete directory. Application restart reuse is Kubernetes Secret reuse; it is not the plugin's issue/cache path.

## Install contract

The root bootstrap creates `kind-venafi-lab-cm`, loads `venafi-lab/demo:local`, and configures Vault first. Run `VAULT_ADDR=http://<vault-node-ip>:30200 ./experiments/cert-manager/install.sh`. `KUBECONFIG` is inherited; every Kubernetes/Helm operation explicitly selects `kind-venafi-lab-cm`. Default Vault address is `http://venafi-lab-vault-control-plane:30200`, but pods commonly require the Docker-network node IP supplied by bootstrap.

Vault Kubernetes auth mount `kubernetes-cm`, role `demo`, must bind `demo/vault-issuer` with audience `vault://demo/vault` and allow `update` on `venafi-cm/sign/demo`. The cluster's TokenReview credential is configured by bootstrap. cert-manager's controller receives only namespaced permission to create tokens for that one service account. Application pods do not mount API tokens.

Helm chart is pinned to cert-manager **v1.21.2**, verified with `helm show chart`; OCI digest `sha256:634dce9c13b56677a2c05e2ab76c312d0be2664022d5dd05815da67e1fd5f610`. `values.yaml` pins controller, cainjector, webhook and startup API check images to verified multi-architecture digests; `helm template` confirmed the resulting references. See [supported releases](https://cert-manager.io/docs/releases/), [Vault Issuer](https://cert-manager.io/docs/configuration/vault/) and [Certificate lifecycle](https://cert-manager.io/docs/usage/certificate/).

## Observe and reproduce

The Service is `demo`, NodePort `30080`; the coordinator maps it to a loopback host port. `/` displays public metadata and `/cgi-bin/cert` provides JSON. Deployment name and label are `demo`, container name `app`.

Capture a baseline:

```sh
python3 experiments/cert-manager/observe.py --output artifacts/cm-before.json
kubectl --context kind-venafi-lab-cm -n demo rollout restart deployment/demo
kubectl --context kind-venafi-lab-cm -n demo rollout status deployment/demo
python3 experiments/cert-manager/observe.py --before artifacts/cm-before.json --expect reuse --output artifacts/cm-replacement.json
```

Scale to three replicas with `kubectl --context kind-venafi-lab-cm -n demo scale deployment/demo --replicas=3`, wait for rollout, and rerun the reuse comparison. Run before the midpoint; if renewal occurs during this scenario, repeat using the new baseline. Compare the simulator's issuance and retrieval counters separately: the helper does not claim unchanged upstream traffic.

`./experiments/cert-manager/verify-reuse.sh [output-directory]` automates the whole-pod replacement and three-replica cases and restores the original replica count. It requires at least three minutes until midpoint to reduce overlap with normal renewal, and asserts that replaced pods really have new UIDs. Its evidence covers mounted identities; the main lab verifier compares upstream counters.

`python3 experiments/cert-manager/verify-container.py [output-directory]` verifies private-key/public-key correspondence, chain trust, hostname and validity inside the pod, then stops only its app container through the dedicated kind node's CRI. It asserts the pod UID is unchanged, restart count increases, and certificate/key identity survives. Only public-key hashes and success output are persisted. This requires Docker access and one active replica. A signal sent to PID 1 from inside its own Linux namespace is insufficient for this test, because an unhandled signal can be ignored.

Verified locally on 2026-09-18 at 02:14–02:16 UTC: whole-pod replacement, three replicas and container-only restart all reused serial `61626D8EE81C1B1AC68E81209B9173E6F1D75FC9` and public-key hash `2a04c640074f298c2934eadc0dd1be1d7cb698522eea517e78c06ec9328f9b49`. Mock `exp1` issuance, retrieval, authentication and policy counters each remained at 1 (zero delta). Certificate/key match, chain, hostname and validity checks passed. See `artifacts/cm-reuse/` and `artifacts/cm-container/` for sanitized evidence. Natural midpoint renewal is a separate test, coordinated by the main lab verifier.

For **natural renewal**, keep pods running and wait until `Certificate.status.renewalTime`, then allow up to 120 seconds for controller scheduling plus Secret-volume propagation. Run the helper with `--expect rotation`. It asserts that serial and public-key hash both changed and replicas converged. Save separate observations for two natural cycles, and record actual timestamps/counters through the main lab verifier. The browser polls every five seconds and can alternate between replicas.

The requested duration is one hour, with `renewBeforePercentage: 50` and key `rotationPolicy: Always`. In the accelerated profile the simulator returns a 12-minute actual lifetime, so renewal is scheduled at its actual midpoint. The page labels both durations and reports NotBefore, NotAfter, midpoint, serial, certificate fingerprint and public-key hash. CA backdating changes the actual midpoint. Do not delete the TLS Secret to simulate a restart: that intentionally triggers recovery issuance.

The helper reads only public metadata from each pod, Certificate/Issuer status, pod UIDs, and Secret names/types. Private key values never enter its output. Kubernetes Secret storage and mounted-file consumption are demonstrated; TLS-server hot reload and real TPP interoperability are separate, unverified properties.

If issuance fails, inspect `kubectl --context kind-venafi-lab-cm -n demo describe issuer vault`, `describe certificate demo-tls`, and CertificateRequest events. A Ready Issuer alone does not establish successful certificate issuance. Cross-cluster API access and the Vault role audience must agree with the bootstrap configuration.
