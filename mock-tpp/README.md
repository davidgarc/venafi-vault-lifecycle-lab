# TPP WebSDK contract simulator

This container accepts actual PKCS#10 CSRs through the same HTTPS WebSDK endpoints used by VCert v5.13.7 and signs real certificates with its persistent local CA. It is not Venafi TPP and does not emulate its policy engine, permissions, workflows, CA integrations, token refresh or service-generated private keys. No request is deduplicated: every accepted certificate request issues a fresh serial. Thus upstream issuance counters can prove whether Vault reuses certificates.

- HTTPS `8443`: `GET /vedsdk/Identity/Self`, `POST /vedsdk/certificates/checkpolicy`, `/request`, `/retrieve`, and `/vedsdk/metadata/get`.
- HTTP `8080`: read-only `/api/state`, `/ca.pem`, `/healthz`, and a simple auto-refreshing status page at `/`.
- WebSDK requires `Authorization: Bearer lab-token-cm` or `lab-token-agent`. Shared `lab-token` and `lab-token-exp1`/`lab-token-exp2` are also accepted. These are deliberately public local-lab credentials, not production secrets.
- Zones ending in `\cm` / `\agent` classify counters as `exp1` / `exp2`. Standard `exp1`/`exp2` CNs and zones also work. Shared-token authentication has no experiment identity and is counted as `shared`.
- `DATA_DIR=/data` retains CA key, CA certificate, public issuance records, counters and events. Do not commit runtime data. Mount a persistent volume to preserve state across container restarts.
- `CERT_LIFETIME_SECONDS=720` sets actual leaf lifetime, without backdating. This intentionally overrides the requested CA expiration attributes for an accelerated demo; requested attributes remain in public state. With cert-manager's requested one-hour duration and 50% renewal setting, actual twelve-minute leaves renew around six minutes.
- HTTPS certificate trusts the CA exposed at `/ca.pem` and includes `mock-tpp`, `localhost`, `mock-tpp.vault.svc`, `mock-tpp.vault.svc.cluster.local`, and `127.0.0.1`. Configure the plugin with `url=https://mock-tpp:8443`, its token, and the saved CA bundle path.
- The simulator stores no workload private keys. The plugin/cert-manager generated the key before submitting its CSR. Retrieval with `IncludePrivateKey` is explicitly rejected.

Build: `docker build -t lifecycle-mock-tpp:local .`

Run: `docker run --rm -p 127.0.0.1:18080:8080 -p 127.0.0.1:18443:8443 -v lifecycle-mock-data:/data lifecycle-mock-tpp:local`

Tests: `python -m pip install -r requirements.txt && python -m unittest -v`

Contract source: [VCert v5.13.7 TPP connector](https://github.com/Venafi/vcert/blob/v5.13.7/pkg/venafi/tpp/connector.go), [WebSDK request/response types](https://github.com/Venafi/vcert/blob/v5.13.7/pkg/venafi/tpp/tpp.go). API paths are case-insensitive, response field names follow these SDK structs, and certificate retrieval returns base64-encoded PEM with optional CA-chain ordering. Custom fields are echoed through metadata reads so the plugin's origin metadata is preserved.
