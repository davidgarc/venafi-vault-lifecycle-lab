# Shared BusyBox certificate viewer

Build with `docker build -t venafi-lab/demo:local demo-app`. Alpine 3.22.1's base manifest digest is pinned. BusyBox `httpd` serves on port 8080; OpenSSL reads the first public certificate on each CGI request. `jq` escapes every JSON string; GNU `date` parses OpenSSL validity timestamps.

Environment: `CERT_FILE` defaults to `/certs/tls.crt`; Agent uses `/vault/secrets/bundle.pem`. `POD_NAME`, `EXPERIMENT`, and `CERT_STORAGE` label observations. `/cgi-bin/cert` returns public metadata only. The private key is never parsed or serialized by the CGI, even when its input is a combined bundle. Certificates are outside the `/www` web root. No external fonts, scripts, telemetry or network services are required by the page.

The application runs as UID/GID 1000 and supports a read-only root filesystem. Its input must be readable by that identity (cert-manager fsGroup 1000; Agent injector file/user configuration must match). Request failures return 503 rather than inventing metadata. This is an HTTP demonstration of mounted-file updates, not a TLS server.

Run `python3 demo-app/smoke-test.py` after building. It creates temporary test certificate/key files, starts the container with a read-only filesystem on a random loopback port, checks the HTML and public JSON, confirms the bundle is inaccessible over HTTP, and removes the container/files. This smoke test passed locally. Alpine packages `httpd` in `busybox-extras`; the image explicitly installs and invokes that binary.
