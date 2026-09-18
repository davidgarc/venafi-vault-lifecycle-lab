#!/usr/bin/env python3
"""Isolated container smoke test; creates temporary keys, removes its container."""
import json
import pathlib
import subprocess
import tempfile
import time
import urllib.error
import urllib.request


def run(*args):
    return subprocess.check_output(args, text=True).strip()


with tempfile.TemporaryDirectory(prefix="venafi-demo-smoke-") as directory:
    root = pathlib.Path(directory)
    root.chmod(0o755)
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(root / "key.pem"), "-out", str(root / "cert.pem"), "-days", "1", "-subj", "/CN=smoke.lab.test", "-addext", "subjectAltName=DNS:smoke.lab.test"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    (root / "bundle.pem").write_bytes((root / "key.pem").read_bytes() + (root / "cert.pem").read_bytes())
    (root / "bundle.pem").chmod(0o644)
    container = run("docker", "run", "--rm", "-d", "--read-only", "-p", "127.0.0.1::8080", "-v", f"{root}:/certs:ro", "-e", "CERT_FILE=/certs/bundle.pem", "-e", "EXPERIMENT=smoke", "venafi-lab/demo:local")
    try:
        for attempt in range(30):
            published = json.loads(run("docker", "inspect", container))[0]["NetworkSettings"]["Ports"].get("8080/tcp")
            if published:
                address = f'{published[0]["HostIp"]}:{published[0]["HostPort"]}'
                break
            time.sleep(0.2)
        else:
            raise RuntimeError("Docker did not publish the loopback port")
        url = f"http://{address}"
        for attempt in range(30):
            try:
                with urllib.request.urlopen(url + "/cgi-bin/cert", timeout=2) as response:
                    raw = response.read().decode()
                break
            except (urllib.error.URLError, TimeoutError):
                time.sleep(0.2)
        else:
            raise RuntimeError("Demo CGI did not become ready")
        data = json.loads(raw)
        assert data["ready"] and data["valid"]
        assert data["subject"] == "CN=smoke.lab.test"
        assert data["sans"] == "DNS:smoke.lab.test"
        assert len(data["public_key_sha256"]) == 64
        assert data["actual_duration_seconds"] == 86400
        assert "PRIVATE KEY" not in raw and "BEGIN CERTIFICATE" not in raw
        with urllib.request.urlopen(url, timeout=2) as response:
            assert "Certificate at the application" in response.read().decode()
        try:
            urllib.request.urlopen(url + "/certs/bundle.pem", timeout=2)
        except urllib.error.HTTPError as error:
            assert error.code == 404
        else:
            raise AssertionError("Certificate bundle was exposed over HTTP")
        print(json.dumps({"passed": True, "assertions": ["read-only nonroot server", "combined key/certificate input", "public metadata JSON", "HTML page", "bundle inaccessible via HTTP"], "metadata": data}, indent=2))
    finally:
        subprocess.run(["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL)
