# Runtime findings

Validation platform: Apple Silicon Mac, Docker Desktop 29.8.0, kind v0.31.0, Kubernetes v1.35.0, Vault/Agent v1.21.4, cert-manager v1.21.2, unmodified Venafi plugin commit `1d98f04e22549fb7307908c3140fa359b4dd0d15`.

Validation began September 18, 2026 UTC (September 17 local US Central). Evidence contains exact timestamps. This report distinguishes successful gates from an observed native renewal failure; it is not an all-green result.

## Natural renewal result

**cert-manager passed. Agent Injector did not fully satisfy the 50% update requirement.**

For cert-manager, two new certificates were signed exactly at the actual midpoint (zero-second issuance offset). All three application replicas converged after approximately 75 and 83 seconds, within the 120-second projection allowance.

For Agent, the first native renewal converged across three replicas about nine seconds after the midpoint. At the second midpoint (02:27:22 UTC), the plugin issued exactly one replacement; two replicas rendered it and one did not. The affected pod still had its previous certificate at 02:33:19 and was observed updated by 02:33:28, around the 02:33:22 next midpoint and old certificate's expiry boundary. It recovered automatically to the following generation, without a pod or Agent restart. No additional custom renewal logic was added.

This is a missed certificate generation, not an upstream duplicate-issuance problem. The observation interval does not establish or rule out a brief expired-service interval; it does establish failure to replace that replica's certificate at 50% lifetime.

The stale bundle was verified in both app and Agent containers with the same inode/mtime, ruling out webpage caching. Source inspection suggests consul-template's second-resolution response index can discard changed data fetched in the same second; this is a **hypothesis**, not a traced root cause. Official latest Vault 2.1.1 still bundles consul-template 0.41.1; standalone 0.43.0 retains the relevant implementation. Neither newer version was runtime-tested here, and no verified released fix was identified.

## Measured common behavior

- Both experiments issue through the real plugin and HTTPS mock TPP API, with actual cryptographic CSR signing.
- App-container restarts, pod replacements and three replicas preserve the certificate/public-key identity before renewal.
- The accelerated mock issues 720-second certificates from one-hour requests. The renewal target is the actual 360-second midpoint.
- Auth isolation rejects wrong-cluster and wrong-service-account tokens.
- Browser pages show actual mounted certificate metadata; control actions change only lab demo deployments.

## Comparison

| Property | cert-manager + Vault Issuer | Vault Agent Injector + plugin |
| --- | --- | --- |
| Key generation | cert-manager | Venafi plugin using local CSR/key generation |
| Durable certificate/key storage | Kubernetes TLS Secret | Native Venafi plugin storage in Vault |
| Workload delivery | projected Secret volume | Agent-rendered memory-backed emptyDir |
| Shared identity across replicas | all mount the same Secret | fixed app identity + plugin `store_by=hash` |
| Renewal owner | cert-manager Certificate controller | running Agent `pkiCert` templates |
| 50% configuration | `renewBeforePercentage: 50` | Agent threshold `0.5` plus fixed half-life `min_cert_time_left` |
| New Agent pod warm-cache traffic | no Vault/TPP call for ordinary app restart | no certificate issue/retrieval, but one TPP authentication call per new pod in this test |
| No application pods | cert-manager controller remains available | no Agent remains to drive renewal; next pod evaluates cache on demand |

## Native Agent limitations

**Scope clarification (September 23):** the authentication dependency below describes the original positive-timeout configuration. Subsequent testing verified a configuration-only bypass using zero timeout, system trust and access-token authentication, including fresh-pod startup with TPP offline. It has issuance tradeoffs; see [the configuration research](AUTH_CACHE_CONFIGURATION.md).

The warm-cache test recorded zero issuance/retrieval/policy calls and three authentication calls for three new pods. Vault-local certificate caching does **not** mean the entire request is independent of TPP availability.

`min_cert_time_left` is a duration. Setting it to six minutes produces the desired midpoint for a fixed twelve-minute issuer policy. This is not a universal percentage-based cache policy for arbitrary variable lifetimes.

Cold startup means issuance plus retrieval into Vault. This does not import an arbitrary pre-existing certificate and private key from TPP.

Concurrency results apply to the tested three replicas and pinned versions; they do not prove the absence of every possible race. A source-level concurrency concern remains a reason to test realistic load before selecting production behavior.

## Restart and upstream outage

Vault was restarted and unsealed using local bootstrap state. A replacement Agent pod received the same certificate/key from persisted plugin storage: zero new issuance, zero retrieval, one TPP authentication call. cert-manager retained its existing Secret identity.

With the entire mock TPP deployment stopped, existing cert-manager and Agent files remained valid and readable. A new cert-manager pod reused the same Secret. A new Agent pod stayed in initialization until TPP returned, despite the Vault cache. All Agent pods recovered after restoration. This confirms the upstream authentication dependency in the tested configuration.

The outage harness originally ran with one cert-manager replica; a supplemental check read the replacement pod directly and confirmed its changed pod identity, unchanged certificate/public key, and zero desired TPP replicas. The final reusable harness identifies and inspects replacement pods directly even when multiple replicas exist.

## No running Agents

All Agent pods were stopped before the current certificate midpoint and left stopped until fifteen seconds after it. No background issuance occurred. Starting three pods then produced exactly one issuance and one retrieval, and all three received the same new valid certificate/key. This is demand-driven renewal; Vault's stored certificate does not renew itself when there are no running Agents.

## Cleanup and clean recreation

The final lifecycle test passed all fifteen checks. Removing only the cert-manager cluster left the shared Vault container and Agent workload available. Full shutdown removed the three owned kind clusters, UI container, bootstrap credential files and all five lab listening ports. The pre-existing `kind` cluster container and default kubeconfig were preserved.

A fresh `make up` then recreated all three clusters and both experiments issued valid certificates. A second full shutdown passed the same cleanup checks. The lab was left **stopped** at 02:48 UTC on September 18, 2026. Downloaded images and build caches remain intentionally for reuse; no running lab resources or local bootstrap credentials remain.

## Evidence map

- `agent-cold.json`: simultaneous cold starts, shared serial/key and one issuance.
- `agent-warm.json`: simultaneous warm starts and rolling whole-pod replacement, including per-operation upstream deltas.
- `agent-container.json` and `cm-container/`: same-pod container restart and public-key/chain checks.
- `cm-reuse/`: whole-pod replacement and scaling, unchanged upstream counts.
- `auth-isolation.json`: rejected cross-cluster and wrong-SA logins.
- `renewal-observations.jsonl` and `renewal-report.json`: actual natural-renewal timeline.
- `dashboard.jpg`: browser capture; no private-key material.
- `code-review.json`: final structured Astra-medium review, no actionable findings.
- `resilience.json`: Vault restart, complete upstream outage and recovery.
- `demand-driven.json`: zero replicas past midpoint, then one shared replacement on demand.
- `cleanup-cycle.json`: partial shutdown isolation, clean recreation and final cleanup.
- `cm-outage-direct.json`: direct replacement-pod check while TPP was stopped.
- `ui-validation.json` and `mock-outage-dashboard.jpg`: verified browser state and outage counters.
- `agent-second-renewal-divergence.json`, `agent-native-renewal-anomaly.json`, `agent-native-followup.jsonl`: failed update, source investigation and spontaneous recovery.
- `renewal-strict-supplement.json`: independent recheck of replica validity/convergence and per-cycle issuance counts.

All paths above are under `artifacts/`. Public certificate metadata, internal lab node names and disposable serials are included. Private keys and authentication credentials are not.
