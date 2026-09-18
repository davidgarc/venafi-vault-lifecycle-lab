# Native Vault Agent experiment

This experiment installs HashiCorp's injector into `kind-venafi-lab-agent` and injects the unmodified Vault Agent into Deployment `demo` in namespace `demo`. The shared infrastructure must provision `auth/kubernetes-agent`, bind role `demo` to service account `demo` in namespace `demo`, and authorize `update` on `venafi-agent/issue/demo`. The plugin mount must use `store_by=hash`, `store_pkey=true`, local storage, and `min_cert_time_left=6m` for the 12-minute mock profile.

```sh
# The common setup supplies the real Vault node address and inherited KUBECONFIG.
VAULT_ADDR=http://172.18.0.2:30200 ./experiments/agent/install.sh
./experiments/agent/observe.py > artifacts/agent-before.jsonl
./experiments/agent/observe.py --seconds 1500 > artifacts/agent-two-cycles.jsonl
```

The IP above is illustrative; use the address returned by the lab setup. Every Kubernetes/Helm command uses the explicit experiment context. Installation does not switch the current context. The app exposes public metadata on `/cgi-bin/cert`, and HTTP NodePort 30080 is mapped to the host by the common kind configuration.

## Material and native scheduling

The injector creates a memory-backed emptyDir. One template atomically replaces `/vault/secrets/bundle.pem` with certificate, private key, and CA chain. Mode `0440`, Agent UID 100/GID 1000, and pod fsGroup 1000 restrict file access. There is no application TLS Secret or ConfigMap. The app uses the bundle to inspect public metadata, not to terminate TLS. Kubernetes service-account tokens and injector webhook TLS are separate infrastructure credentials.

The actual native annotation is `vault.hashicorp.com/template-lease-renewal-threshold: '0.5'`. `pkiCert` exposes `.Cert`, `.Key`, and `.CAChain`; these are not the ordinary `secret` function's response fields. Agent requests `ttl=1h` because the plugin truncates TTL to whole hours. The mock's 12-minute actual validity drives scheduling, not the request TTL.

Pinned Vault Agent 1.21.4 includes consul-template 0.41.1. That implementation reads the destination bundle first, scans PEM values from the Vault response on a miss, and determines renewal time from the certificate's original NotBefore/NotAfter and threshold. It does not rely on `lease_duration` or `renewable`; a plugin cache-hit response with no lease can therefore supply the bundle. The init Agent retrieves the bundle; the sidecar can reuse the resulting file. An emptyDir survives application/sidecar container restarts and disappears with the pod. A replacement pod retrieves through the plugin again.

These are source findings, not a claim that the full runtime matrix passed. The source's scheduling jitter expression contains integer division that rounds its small adjustment to zero in this pinned version. Target the actual midpoint with a 30-second observation/propagation tolerance, and record late retries separately.

## Reproducible scenarios

Use `observe.py` before/after each operation and record mock per-endpoint counters alongside it. The helper emits metadata, pod UIDs, key-match booleans, memory volume details, and Secret/ConfigMap PEM field names; it never emits private keys or tokens. `kube-root-ca.crt` is an expected public CA ConfigMap, not the application identity.

```sh
# Warm simultaneous replicas, then whole-pod replacement.
kubectl --context kind-venafi-lab-agent -n demo scale deployment/demo --replicas=3
kubectl --context kind-venafi-lab-agent -n demo rollout status deployment/demo
./experiments/agent/observe.py
kubectl --context kind-venafi-lab-agent -n demo rollout restart deployment/demo
kubectl --context kind-venafi-lab-agent -n demo rollout status deployment/demo
./experiments/agent/observe.py

# Natural midpoint: observation only, no manual rotation request.
./experiments/agent/observe.py --seconds 1500 --interval 10

# Demand-driven recovery: leave all Agents stopped beyond the midpoint.
kubectl --context kind-venafi-lab-agent -n demo scale deployment/demo --replicas=0
# Wait until the recorded midpoint has passed, then:
kubectl --context kind-venafi-lab-agent -n demo scale deployment/demo --replicas=3
```

For cold concurrency, start a clean lab/plugin cache with Deployment replicas set to three **before** the first Agent pod starts; warm scaling is not a cold-concurrency test. Do not delete existing plugin state or certificates as part of a warm test. For an application-container restart, restart only `app`, record unchanged pod UID, and compare the new restart count; rolling restart tests whole pods instead. For outages, separately test existing pods with usable files and replacement pods needing to call the plugin. Capture failures as results, then restore the mock and observe recovery.

## Native limitations to measure

* The plugin constructs/authenticates its TPP client before cache lookup; warm retrieval may still require upstream authentication and can fail during total upstream outage.
* Agent is a per-pod consumer, not a cluster-wide renewal coordinator. Plugin caching/synchronization determines whether cold starts and simultaneous midpoint requests converge without duplicates; do not assume that locking guarantees it.
* With every pod stopped, no Agent requests renewal. Rotation resumes on demand.
* An Agent renewal request slightly before the plugin cache cutoff can receive the old certificate and retry. Boundary behavior and issuance counts are runtime evidence, not hidden by a custom coordinator.
* Memory-only files are lost on pod deletion; durable shared identity depends on persisted Vault plugin storage. The stored private key is intentional for this shared-identity experiment.

## Exact source references

* [Vault 1.21.4 dependencies](https://github.com/hashicorp/vault/blob/v1.21.4/go.mod)
* [consul-template 0.41.1 pkiCert response parsing and scheduling](https://github.com/hashicorp/consul-template/blob/v0.41.1/dependency/vault_pki.go)
* [consul-template 0.41.1 atomic renderer](https://github.com/hashicorp/consul-template/blob/v0.41.1/renderer/renderer.go)
* [vault-k8s 1.7.0 annotations](https://github.com/hashicorp/vault-k8s/blob/v1.7.0/agent-inject/agent/annotations.go)
* [vault-k8s 1.7.0 memory volumes](https://github.com/hashicorp/vault-k8s/blob/v1.7.0/agent-inject/agent/container_volume.go)
* [vault Helm chart 0.30.1 values](https://github.com/hashicorp/vault-helm/blob/v0.30.1/values.yaml)

## Concurrency driver

On a fresh lab, set `AGENT_INITIAL_REPLICAS=0` when running the common `up-agent` command (the environment propagates to `install.sh`), then run:

```sh
KUBECONFIG="$PWD/.state/kubeconfig" ./experiments/agent/verify.py --phase cold > artifacts/agent-cold.json
KUBECONFIG="$PWD/.state/kubeconfig" ./experiments/agent/verify.py --phase warm > artifacts/agent-warm.json
```

The cold phase refuses an already-issued exp2 inventory or existing app pods. It scales zero to three and measures convergence and exactly one upstream issuance. The warm phase requires an existing shared identity with at least 90 seconds before midpoint, stops all pods, simultaneously starts three against the warm cache, and then replaces all pods; it checks identity reuse and zero additional issuance/retrieval while preserving authentication/policy counter deltas. Tests leave three replicas running for the natural two-cycle observation. A failed native assertion returns exit code 1 and records the evidence; it never patches the plugin, wipes the cache, or forces renewal. Use the common lab teardown after collecting the results.

## Recorded runtime gate (2026-09-18 UTC)

`artifacts/agent-cold.json` records three simultaneous initial replicas converging on one certificate/key with exactly one issuance, one retrieval, one policy lookup, and three upstream authentication calls. `artifacts/agent-warm.json` records simultaneous startup from zero against retained Vault storage and a subsequent full rolling replacement: each operation added three authentication calls and zero issuance, retrieval, or policy calls. All pairs matched and the original serial/public-key fingerprint persisted. The namespace contained no Secrets; the only PEM ConfigMap was Kubernetes' public root CA. Kubernetes login succeeded using the configured audience. These gates passed; natural renewal, failure recovery, and later integrated scenarios are recorded separately by the coordinator.

Two installation compatibility fixes were validated during this run: kubectl emits concatenated JSON for the multi-document input, so `prepare-manifests.py` normalizes it into a List; Helm 4 upgrades use client-side apply to avoid conflicting with the injector's managed webhook CA bundle. Helm 3 omits that unsupported option.
