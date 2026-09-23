# Cached certificate delivery without TPP authentication

Follow-up research: September 23, 2026. Plugin commit `1d98f04e22549fb7307908c3140fa359b4dd0d15`, VCert `v5.13.7`, Vault/Agent `1.21.4`. The plugin and SDK were not patched.

## Conclusion and correction

**A configuration-only workaround exists for the tested version.** With access-token authentication, `server_timeout=0`, and the TPP CA trusted through the Vault container's system trust store instead of the plugin's `trust_bundle_file`, warm requests returned cached certificates without any TPP calls. Three fresh Agent-injected pods started successfully while the entire mock TPP service was stopped.

The earlier outage result remains correct for the default positive timeout and explicit trust bundle. It must not be generalized into an unavoidable limitation of Vault Agent Injector or every plugin configuration. The default lab configuration remains unchanged so its original results stay reproducible.

This is a side effect of client initialization, not a documented `skip_auth_on_cache_hit` feature. Upstream documents `server_timeout` as an enrollment timeout. Maintainer confirmation of intended support and a test against the actual TPP/CA workflow remain necessary before choosing it for production.

## Tested matrix

| Configuration / request | Result |
| --- | --- |
| Positive timeout, explicit trust bundle, warm cache, TPP online | Same certificate/key; one Identity/Self call, no issuance/retrieval |
| Positive timeout, warm cache, TPP offline | Request failed before cache return |
| Zero timeout + explicit `trust_bundle_file` | HTTP 500 / plugin RPC termination; source has a nil-client dereference |
| Zero timeout + system trust + access token, warm cache, TPP online | Same certificate/key; zero auth, policy, issuance and retrieval calls |
| Same zero-timeout configuration, TPP offline | Three direct warm requests returned the same certificate/key |
| Same zero-timeout configuration, three fresh injected pods, TPP offline | All three started with the cached identity, valid certificates and matching keys; no application TLS Secret |
| Zero timeout, cold issuance and threshold-triggered reissuance, instant-signing mock | Both succeeded with verified TLS; issuance/retrieval occurred, no Identity/Self call |
| Zero timeout, deliberately invalid upstream access token, warm cache | Cache return succeeded; upstream credential validity was not checked |
| Same invalid token, cache miss | Rejected with upstream HTTP 401 |

The reissuance check explicitly requested a minimum remaining lifetime larger than the cached certificate's remaining lifetime. It tests the native reissuance branch; it is not another natural midpoint-renewal test and does not resolve the previously observed Agent replica-update anomaly.

Evidence: [results.json](../artifacts/auth-cache-research/results.json). The report contains serials and hashes, not certificate private keys or real tokens. Its assertion note explains a correction to the expected-error check: Vault exposed RPC termination rather than a panic stack. The offline-delivery observations were unchanged.

## Configuration conditions

All of these are required for the tested path:

1. Use the TPP **access-token** credential branch, not username/password.
2. Set role `server_timeout` to `0s`; preserve the role's other settings. Read the role back and confirm the stored value.
3. Omit/clear the Venafi credential's `trust_bundle_file` and install the CA chain signing the TPP HTTPS server certificate in the Vault/plugin container's system trust store. TLS certificate verification stays enabled. Our disposable test appended the CA to `/etc/ssl/certs/ca-certificates.crt`; a persistent deployment must provision trust reproducibly.
4. Retain `store_by=hash`, `store_pkey=true`, `no_store=false`, `ignore_local_storage=false`, and an appropriate `min_cert_time_left`.
5. Request the same cached identity before it crosses the minimum-remaining-lifetime threshold.

Our empirical test used an access token without paired refresh tokens. If `refresh_token` and `refresh_token_2` are configured, a due token refresh can still require TPP **before** cache lookup. Removing token refresh has an operational cost: usable credentials are still needed for subsequent real issuance. No lifecycle service was added to compensate.

There is no blanket guarantee for other credential modes, versions, endpoints, or configurations. Vault authentication and authorization still apply to the request even when the TPP identity call is skipped.

## Why the settings change behavior

- [Plugin client initialization](https://github.com/Venafi/vault-pki-backend-venafi/blob/1d98f04e22549fb7307908c3140fa359b4dd0d15/plugin/pki/vcert.go#L149-L168): a positive timeout creates a custom HTTP client. Zero leaves it nil. An explicit trust bundle then dereferences that nil client, explaining the failing combination.
- [VCert authentication](https://github.com/Venafi/vcert/blob/v5.13.7/pkg/venafi/tpp/connector.go#L173-L182): the access token is assigned, but Identity/Self is called only when the client is non-nil.
- [Plugin cache lookup](https://github.com/Venafi/vault-pki-backend-venafi/blob/1d98f04e22549fb7307908c3140fa359b4dd0d15/plugin/pki/path_venafi_cert_enroll.go#L195-L257): client initialization precedes cache lookup. Skipping the identity network call lets this request reach local storage offline.
- [Lazy SDK HTTP client](https://github.com/Venafi/vcert/blob/v5.13.7/pkg/venafi/tpp/tpp.go#L532-L565): a later cache miss creates a verified HTTP client with a 30-second timeout. Zero therefore does not mean every subsequent network request waits forever.
- [Token-refresh ordering](https://github.com/Venafi/vault-pki-backend-venafi/blob/1d98f04e22549fb7307908c3140fa359b4dd0d15/plugin/pki/path_venafi_cert_enroll.go#L204-L215): paired refresh credentials can add a separate upstream dependency when refresh is due.

## Certificate pickup tradeoff

The plugin also passes `server_timeout` into the certificate pickup request ([source](https://github.com/Venafi/vault-pki-backend-venafi/blob/1d98f04e22549fb7307908c3140fa359b4dd0d15/plugin/pki/path_venafi_cert_enroll.go#L590-L607)). VCert returns a pending error after the first empty retrieval when that timeout is zero, rather than polling ([source](https://github.com/Venafi/vcert/blob/v5.13.7/pkg/venafi/tpp/connector.go#L1428-L1448)).

The live pending-response test reproduced this difference: positive timeout succeeded after two retrievals (about 2.1 seconds); zero timeout returned a pending error after one retrieval (about 0.1 seconds). The fixture and measured results are in [pending.json](../artifacts/auth-cache-research/pending.json). It simulates an initial HTTP 200 with empty certificate data followed by an available certificate. It probes the real unmodified client behavior; it does not reproduce every TPP/CA workflow or establish eventual retry/duplicate-issuance behavior. Instant mock issuance alone cannot establish production suitability for asynchronous CAs.

## Reproduce

Use a disposable lab; these tests change demo roles, replace application pods, stop TPP, and install a test CA into the Vault container. They preserve the plugin binary and restore the default demo role, mock deployment command and replica count. Full teardown removes the additional container trust and research roles.

```sh
make up-agent
python3 tests/auth-cache-config.py
python3 tests/auth-cache-pending.py
make down
```

The scripts write only to `artifacts/auth-cache-research/`; earlier experiment reports are preserved. A failed assertion stays visible. The checked-in research report explicitly identifies its one assertion correction.

## Executive wording

> The default configuration requires Venafi availability to deliver cached certificates to new workloads. A configuration workaround enables offline cache reuse, but introduces issuance tradeoffs that require production validation.

Do not present this as an inherent Vault Agent Injector limitation or as a fully validated production fix.
