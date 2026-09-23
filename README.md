# Certificate lifecycle lab

Compare **cert-manager** and **HashiCorp Vault Agent Injector** using the unmodified [Venafi PKI plugin](https://github.com/Venafi/vault-pki-backend-venafi), a real CSR-signing mock TPP API, and three isolated kind clusters.

**Measured result:** cert-manager passed both natural midpoint renewals. Agent cache/restart reuse passed, but one of three Agent replicas skipped the second midpoint update and recovered at the next boundary. Experiment 2 therefore does not fully meet the requested lifecycle behavior with these pinned tools. Both experiments also passed clean recreation and final cleanup; the validation environment is stopped. See [results](docs/RESULTS.md).

**September 23 clarification:** the default configuration's TPP dependency for cached delivery is avoidable under specific settings. A tested zero-timeout/system-trust workaround started three fresh Agent pods with TPP offline, but also changed pending-certificate pickup behavior. See [configuration research and tradeoffs](docs/AUTH_CACHE_CONFIGURATION.md).

No custom renewal controller or patched plugin is involved. The simulator, UI, setup scripts and tests make existing tool behavior visible. This lab does not certify compatibility with a deployed Venafi TPP server.

## Start and view

Prerequisites: running Docker with approximately 10 CPUs / 16 GB memory available, kind, kubectl, Helm, Git and Python 3.10+. The implementation was run on an Apple Silicon Mac. Linux builds are supported by the pinned multiarch images but are not yet independently verified.

```sh
gh repo clone davidgarc/venafi-vault-lifecycle-lab
cd venafi-vault-lifecycle-lab
make doctor
make up
```

First startup downloads/builds images and creates three clusters. Subsequent startup reuses downloaded images. The default profile requests one-hour certificates, while the mock CA deliberately issues **12-minute certificates**, producing natural renewal around six minutes. This is a mock CA policy override, not a plugin modification or forced renewal.

| Page | URL |
| --- | --- |
| Combined dashboard and controls | http://127.0.0.1:18080 |
| BusyBox app using cert-manager Secret | http://127.0.0.1:18081 |
| BusyBox app using Agent memory volume | http://127.0.0.1:18082 |
| HashiCorp Vault UI | http://127.0.0.1:18200 |
| Sanitized mock counters and events | http://127.0.0.1:18201/api/state |

The dashboard uses namespace-scoped service accounts and a narrow mock-deployment control role. It does not receive the Vault root token. Vault UI login is optional: the disposable local root token is in ignored `.state/vault-init.json`; do not share or commit it. Demo identities and the mock CA are disposable test material.

For a standard one-hour actual lifetime / 30-minute midpoint, start a fresh lab with `CERT_LIFETIME_SECONDS=3600 make up`. Do not change profiles on a running cache. The profile is fixed for a run because plugin `min_cert_time_left` is a duration, not a percentage.

## Browser walkthrough

1. Open the combined dashboard. Both experiments show actual pod names, serials, public-key fingerprints and validity progress. The mock records actual calls from the plugin.
2. Before the midpoint, select **Replace pods** and **3 replicas**. The certificate/key identity should stay the same. Observe issuance and retrieval counters independently from authentication.
3. Wait for the lifetime gauge to cross its midpoint. A new serial should appear and replicas should converge without restarting the application. Certificate projection in Kubernetes can lag issuance.
4. Compare namespace Secrets: cert-manager has `demo-tls`; the Agent app has no TLS Secret. Agent uses a memory-backed emptyDir and retrieves the reusable keypair from plugin storage in Vault.
5. Use **Pause mock TPP** / **Resume mock TPP** to observe outage behavior. Existing files remain available until expiration. A new Agent pod may fail to fetch the cached identity because plugin authentication happens before the cache lookup. Restore the mock before continuing a renewal demonstration.
6. **Stop app** scales the selected deployment to zero. With no running Agents, there is no Agent driving background renewal. Starting it later causes on-demand evaluation of the cached certificate.

The BusyBox CGI reads certificate files on every request. This proves mounted-file delivery and updates; it is not a demonstration of TLS-server hot reload. Private keys and tokens never appear in the webpages or public evidence.

## Validation

```sh
make verify
```

Validation takes real time (typically 20–30 minutes): it watches two natural renewal cycles, tests restart/outage recovery and stops all Agent pods past a midpoint. It writes sanitized results under `artifacts/`. A native lifecycle failure remains a failed gate; the runner continues with recovery checks and exits nonzero rather than hiding the limitation. Exact scripts and experiment-specific checks are documented under [cert-manager](experiments/cert-manager/README.md) and [Agent](experiments/agent/README.md). Tests distinguish application-container restart from full pod replacement.

For a concurrent **cold-cache** Agent test, start a fresh lab with zero Agent pods:

```sh
AGENT_INITIAL_REPLICAS=0 make up
export KUBECONFIG="$PWD/.state/kubeconfig"
python3 experiments/agent/verify.py --phase cold > artifacts/agent-cold.json
python3 experiments/agent/verify.py --phase warm > artifacts/agent-warm.json
make verify
```

Cold tests never delete or bypass the native plugin cache to manufacture a result. A cold cache issues and retrieves a fresh certificate; this does not import an arbitrary pre-existing TPP private key. The evidence records both successful behavior and native limitations.

## Shutdown and cleanup

```sh
make down
```

This removes the UI container and **only** `venafi-lab-cm`, `venafi-lab-agent`, and `venafi-lab-vault`, including their node-local Vault/mock data. It removes local bootstrap credentials and kubeconfig when the entire lab is gone. Downloaded images, build caches, source and sanitized evidence remain for quick reuse. `make reset` is an alias for this complete disposable-lab teardown.

`make down-cm` and `make down-agent` remove only that workload cluster; shared Vault remains. `make up-cm` and `make up-agent` recreate the selected experiment and refresh the dashboard. A full teardown is intentionally different from a workload restart: recreating Vault starts a new inventory.

For an optional full destructive test of this disposable lab, `python3 tests/cleanup-cycle.py` checks partial shutdown, complete cleanup, fresh startup of both experiments, then leaves everything shut down. Run it after lifecycle observation, since it destroys the lab inventory.

The runner uses `.state/kubeconfig`, explicit contexts, a recorded ownership list and exact node-container identities. It leaves the user's default kubeconfig and existing `kind` cluster untouched. Do not manually delete `.state` while the lab is running: its ownership records make cleanup safe.

## Architecture and boundaries

- Dedicated `venafi-lab-vault`: persistent single-node Vault file storage, plugin mounts `venafi-cm` and `venafi-agent`, HTTPS TPP simulator with local CA.
- `venafi-lab-cm`: cert-manager Vault Issuer uses `venafi-cm/sign/demo`; cert-manager owns the CSR/key and namespace TLS Secret.
- `venafi-lab-agent`: Injector uses `pkiCert`, threshold `0.5`, `venafi-agent/issue/demo`; plugin uses `store_by=hash`, `store_pkey=true`, and half-lifetime `min_cert_time_left`.
- Separate Kubernetes authentication mounts, API CA/reviewer credentials and service-account bindings for each workload cluster. UI/reviewer tokens last 24 hours; rerun `make up`/`make demo` to refresh as appropriate.
- Cross-cluster traffic uses explicit kind node IPs / NodePorts. Host pages bind to loopback. Vault HTTP is a local POC simplification inside Docker; mock TPP HTTPS is verified with its CA. Do not expose this configuration as a production service.
- Vault storage persists across its pod restart. Vault needs unsealing after restart; rerun `make up` or use the local lifecycle bootstrap to unseal it. No production auto-unseal is claimed.

Version/digest pins live in [infra/versions.json](infra/versions.json), Dockerfiles and Helm values. The plugin is built from immutable upstream commit `1d98f04e22549fb7307908c3140fa359b4dd0d15` without source changes.

See [implementation plan](docs/IMPLEMENTATION_PLAN.md) for design and acceptance criteria, [mock API contract](mock-tpp/README.md) for simulator coverage, and `artifacts/` for measured results.

## Troubleshooting

- `make doctor`: check tools, Docker connectivity and existing cluster names. Ports 18080–18082 and 18200–18201 must be available.
- Pods pending: check Docker memory/CPU; this lab runs three Kubernetes control planes.
- Agent init failure: check Vault seal status, Kubernetes role audience, mock availability and trust. The dashboard may show no certificate while init is waiting.
- Existing cluster refused: do not remove unrelated clusters; ownership must match `.state`. A cluster recreated outside the runner intentionally fails the cleanup identity check.
- Midpoint test fails: keep the evidence. Look for mock auth dependency, duplicate issuance, mismatched actual lifetime/minimum remaining lifetime, or certificate projection delay. Do not replace native behavior with custom renewal code.
