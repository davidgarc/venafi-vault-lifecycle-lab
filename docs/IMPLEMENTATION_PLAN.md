# Implementation plan

## Objective and current evidence

Implement two independently runnable, browser-demonstrable experiments using HashiCorp Vault and the unmodified Venafi PKI plugin. This document is a plan, not a report of successful runtime validation.

Local verification: Docker is available on ARM64 with 10 CPUs and approximately 16 GB RAM. docker, kind, kubectl, helm, and gh are installed. An existing kind cluster named `kind` must be left untouched. GitHub authentication resolves to `davidgarc`, a personal account rather than an organization; use that explicitly requested namespace.

The current workspace is an existing Venafi plugin checkout at `1d98f04e22549fb7307908c3140fa359b4dd0d15`. Keep its origin/fork remotes unchanged. The lab has its own repository. Select and pin a verified upstream plugin commit, container digests and chart versions before implementation; do not use floating latest tags.

## Up-front decisions

The user confirmed shared application identities, a descriptive private repository name, and a short demo profile. The user explicitly rejected new lifecycle capabilities:

1. Replicas of one application share the same certificate AND private key, including after whole-pod replacement. Different applications and experiments have separate identities.
2. Use Agent + plugin alone. No renewal coordinator, custom lifecycle service, or plugin patches. Any unmet requirement is an experiment result, not permission to implement new capabilities.
3. Proposed repository: `davidgarc/venafi-vault-lifecycle-lab`, private.
4. Standard profile: request a one-hour certificate and renew at its actual validity midpoint. An optional accelerated profile uses a mock CA validity override, 12 minutes with a six-minute midpoint. cert-manager requests must still satisfy its one-hour duration minimum; the plugin converts TTL to whole hours. Label requested versus actual duration in the UI.
5. Cold start means fresh issuance and retrieval through the plugin, followed by reuse of that identity. Importing an existing TPP certificate with an existing private key is a separate scenario: certificate retrieval alone cannot recover a key that TPP never stored.

## Shared infrastructure

Use three small, single-node kind clusters: `venafi-lab-vault`, `venafi-lab-cm`, and `venafi-lab-agent`. The dedicated Vault cluster is shared; each experiment has its own workload cluster so it can be removed independently.

The Vault cluster contains a single HashiCorp Vault server with persistent storage across server-pod restarts, the registered real Venafi plugin binary, the TPP simulator, and a small demo status/control service. Separate plugin mounts (`venafi-cm/`, `venafi-agent/`), roles, policies, service accounts, mock policy zones, and certificate DNS names prevent cross-experiment reuse. Include experiment identifiers in all telemetry.

Use cluster-node Docker DNS plus explicit service ports for cross-cluster connectivity. Do not assume Kubernetes ClusterIP addresses or Kubernetes DNS names work across clusters. Verify routes from actual pods before installing the experiment components. Bind host demo ports to loopback and check for conflicts.

Use one Kubernetes auth mount per workload cluster, each configured against that cluster's API, CA, and TokenReview credentials. Bind roles to exact namespace/service-account identities. Automate reviewer credential provisioning and refresh for repeated demos. Never put root tokens in workloads or manifests. Validate a wrong-cluster and wrong-service-account login fails.

Keep generated local CA keys, Vault unseal material, credentials and kubeconfigs in ignored local state. Recreate a fresh lab with an explicit reset. The repo contains code and configuration templates, never generated credentials.

## Mock TPP design

Run a containerized HTTPS API simulator with a persistent local test CA and a certificate inventory. Use the real plugin's TPP connector with `fakemode=false`; register the simulator CA trust bundle in the plugin environment.

Derive the exact API contract from the pinned VCert version. Implement only the exercised authentication, policy lookup, certificate request, and retrieval endpoints, plus any search endpoints actually used by the selected path. Return protocol-shaped responses, not hardcoded certificates. Validate CSR signatures and preserve requested subject/SANs; issue a matching certificate and valid chain. Document every supported endpoint and unsupported TPP feature.

Expose counters separately for request/issuance, retrieval, search, policy and authentication. Include a sanitized event timeline, serial, fingerprint, identity, experiment and outcome. Never show private keys or tokens. Add bounded latency and temporary failure controls for reproducible recovery demos.

The plugin's built-in fake mode is useful for an early smoke test, but it bypasses the HTTP TPP boundary and cannot substantiate reduced upstream API traffic. The final demo uses the HTTPS simulator. Results remain simulator results until tested against real TPP.

## Parallel work and supervision

Default both experiment workers to Astra with medium reasoning. Run independent workers concurrently after publishing a shared contract for addresses, mount names, identities, images and telemetry. The coordinator owns common files and infrastructure; workers own separate experiment directories and avoid concurrent cluster/bootstrap edits.

| Owner | Scope | Completion evidence |
| --- | --- | --- |
| Coordinator | kind lifecycle, Vault/plugin image and bootstrap, mock TPP, shared dashboard, integration and private repository | clean up/down, isolated auth/mounts, real plugin-to-mock issuance, integrated report |
| Experiment 1 worker | `experiments/cert-manager/`, issuer/certificate manifests, BusyBox demo, lifecycle tests and walkthrough | initial issuance, restart reuse, natural midpoint renewal, updated Secret/app |
| Experiment 2 worker | `experiments/agent/`, injector configuration, shared identity, native lifecycle tests and walkthrough | no application TLS Secret, cold/warm cache, restarts, replicas, midpoint renewal |

Supervise at four gates: source compatibility; individual cold-start success; lifecycle/concurrency results; integrated reproducibility. Workers report concrete evidence and blockers at each gate. Coordinator reviews diffs, resolves shared-contract changes, returns failures to the owning worker, and reruns affected integration checks. Completion requires both acceptance matrices to be executed, with passing evidence or explicitly documented native limitations sufficient to compare the approaches.

## Experiment 1: cert-manager

Flow: Certificate in application namespace -> cert-manager Vault Issuer -> `venafi-cm/sign/<role>` -> mock TPP signing -> CertificateRequest response -> namespace TLS Secret -> mounted application files.

Use the plugin sign endpoint: cert-manager owns the CSR/private key. Do not use issue or try to return a cached certificate for a different CSR. The plugin's hash reuse path deliberately excludes sign requests. Restart reuse here comes from the persisted Kubernetes Secret.

Configure `renewBeforePercentage: 50`, explicit key rotation policy `Always`, namespace-scoped Issuer and least-privilege Vault access. Derive expected renewal from actual NotBefore/NotAfter, including any CA backdating. Deleting the TLS Secret is outside restart-reuse semantics and is expected to trigger recovery/issuance.

Use an Alpine-based image containing BusyBox httpd and OpenSSL. Mount the whole TLS Secret directory, not subPath. A CGI endpoint re-reads the certificate on every page request and shows subject, SANs, issuer, serial, fingerprint, validity and remaining time. Serve only public metadata. The app demonstrates consumption of mounted certificate data; this is not itself a proof of TLS-server hot reload.

## Experiment 2: Vault Agent Injector

Flow: injected Agent init/sidecar -> `venafi-agent/issue/<role>` -> plugin-managed Vault storage -> mock TPP on cache miss/renewal -> memory-backed emptyDir -> application.

Native baseline settings: `store_by=hash`, `store_pkey=true`, local storage enabled, fixed shared identity, `min_cert_time_left` equal to half the actual demo certificate lifetime. Use Agent `pkiCert` templating and `template_config.lease_renewal_threshold=0.5`; validate these settings against the pinned Agent version. A fresh pod loses emptyDir, but can recover the certificate/key from Vault.

Use a single rendered certificate/key/CA bundle so the app never reads a mixed pair during updates. Inject into `emptyDir.medium: Memory` with restrictive permissions. Only public certificate metadata is exposed over HTTP. No application certificate/private-key material may exist in any Kubernetes Secret or ConfigMap. Injector webhook TLS is infrastructure material and must be distinguished from the application identity in the inventory.

Important feasibility gates: plugin cache-hit responses and lease behavior must work with pkiCert; midpoint scheduling must remain based on original certificate lifetime after restarting a pod; concurrent cold starts and concurrent renewals must not duplicate issuance. Plugin-side request synchronization is present but is not proof of race-free operation. Also verify whether authentication/policy calls still reach TPP on a local certificate cache hit: measure every endpoint independently.

If native behavior fails a required gate, preserve the failing scenario and publish its evidence. Do not add a coordinator, patch the plugin, or hide failure with custom orchestration. The experiment is complete when it establishes what the existing tools can and cannot deliver.

The plugin constructs/authenticates its TPP client before checking its local certificate cache. Thus cache hits may still require upstream identity/authentication calls, and replacement pods may fail during a complete TPP outage even while existing pods retain usable certificates. Test both cases. With all application pods stopped, no Agent is present to drive renewal; the next startup should obtain a fresh certificate if the threshold has passed. Include that demand-driven behavior in the comparison.

Use requested TTL of one hour in both profiles because the plugin truncates durations to whole hours. For the accelerated profile, the simulator issues a 12-minute actual lifetime and the plugin's minimum remaining validity is six minutes. This leaves margin above cert-manager's five-minute minimum renewal lead time. Agent templating field syntax and cached-response lease handling must be checked against the exact pinned binary before publishing runnable configuration.

## Browser demo

Provide a landing page linking both experiment views, the mock API timeline, and Vault UI. Each view shows pods, auth/issuer readiness, current serial and public-key fingerprint, validity progress and midpoint, where key material lives, issuance/retrieval counts and recent test results.

Provide scoped actions for application restart, pod replacement, scale to multiple replicas, and temporary mock outage. Actions affect only lab-owned resources, use POST with origin protections, and display progress/result evidence. The main lifecycle demonstration waits for actual renewal; an optional manual renewal control must be visibly labeled as manual and never substitute for the midpoint test.

Display observed state rather than synthetic success badges. Show observation timestamp and polling delay. Link to each pod's certificate metadata, and refresh to show rotation. A Secrets inventory reports names/types and whether application TLS material exists without rendering values.

## Runtime acceptance matrix

| Scenario | Required observation |
| --- | --- |
| Clean startup | all three named clusters ready; real plugin registered; each experiment gets its expected certificate and matching key |
| Container restart before midpoint | serial/key fingerprint unchanged; no new issuance |
| Whole-pod replacement before midpoint | same certificate reused; experiment 2 emptyDir refilled from Vault; no new upstream certificate retrieval |
| Simultaneous replica startup | experiment 2 replicas converge on one shared identity; test cold and warm Vault cache separately; no duplicate issuance |
| Natural midpoint | new serial issued near actual midpoint; define and record scheduling/propagation tolerance; both apps display replacement without restart |
| Simultaneous renewal | one new shared identity for experiment 2; no issuance storm or repeated upstream retrieval |
| All pods stopped beyond midpoint | document demand-driven renewal on next startup, without claiming background rotation |
| Two renewal cycles | renewal remains stable after initial rotation and after a pod starts late in certificate lifetime |
| Secret audit | experiment 1 namespace has TLS Secret; experiment 2 has no application PEM/key in Secrets or ConfigMaps |
| Mock outage | existing pods keep usable files while valid; separately measure whether new pods can retrieve cached material despite upstream authentication; recovery eventually rotates |
| Vault restart | persisted plugin storage survives; warm cache can satisfy new pods after auth recovers |
| Isolation | experiment identities, auth, counters and storage remain separate; stop one without breaking the other |
| Teardown/recreate | remove only lab-owned resources; existing `kind` cluster untouched; fresh up reproducible |

Save sanitized JSON evidence with timestamps, actual NotBefore/NotAfter, midpoint, serials, public-key fingerprints, pod UIDs, Secret inventory and per-endpoint counters. Runtime evidence must distinguish certificate retrieval from issuance and Vault calls from mock TPP calls.

## Repository and intended operator commands

Proposed directories: `infra/`, `mock-tpp/`, `demo-ui/`, `experiments/cert-manager/`, `experiments/agent/`, `scripts/`, `tests/`, `docs/`, `artifacts/`. Centralize pinned versions and configurable ports/lifetimes.

Implement `make doctor`, `make up`, `make up-cm`, `make up-agent`, `make demo`, `make verify`, `make down-cm`, `make down-agent`, `make down`, and `make reset`. `up` must be idempotent; `down` semantics must clearly state what local state is retained; `reset` explicitly destroys lab state. Per-experiment down must preserve shared Vault while the other experiment uses it. All kubectl/Helm operations specify the lab context explicitly. Scripts reject unexpected names/contexts rather than modifying the user's existing cluster.

README must include prerequisites/resources, first run, exact browser URLs, scripted and UI walkthroughs, normal versus accelerated time profiles, storage/auth explanation, teardown/reset semantics, troubleshooting, and known mock/native limitations. Final delivery includes the private repo link, verified commands, runtime evidence, and what remains unverified against real TPP.

## Primary references

- [Venafi plugin source and configuration](https://github.com/Venafi/vault-pki-backend-venafi)
- [Vault Agent templating and renewal thresholds](https://developer.hashicorp.com/vault/docs/agent-and-proxy/agent/template)
- [Vault Agent Injector](https://developer.hashicorp.com/vault/docs/deploy/kubernetes/injector)
- [cert-manager Vault Issuer](https://cert-manager.io/docs/configuration/vault/)
- [cert-manager Certificate lifecycle](https://cert-manager.io/docs/usage/certificate/)

Source inspection establishes a plausible implementation, not a guarantee of runtime compatibility. Pin versions, run the gates, and update this plan with observed results during implementation.
