#!/usr/bin/env python3
"""Read-only evidence capture; never prints or persists Secret data/private keys."""
import argparse
import datetime
import json
import subprocess
from pathlib import Path

CONTEXT = "kind-venafi-lab-cm"


def kubectl(*args):
    return subprocess.check_output(["kubectl", "--context", CONTEXT, "-n", "demo", *args], text=True)


def snapshot():
    pods = json.loads(kubectl("get", "pods", "-l", "app=demo", "-o", "json"))
    observations = []
    for pod in pods["items"]:
        metadata = pod["metadata"]
        if metadata.get("deletionTimestamp"):
            continue
        observation = {"pod_name": metadata["name"], "pod_uid": metadata["uid"]}
        try:
            response = kubectl("exec", metadata["name"], "-c", "app", "--", "wget", "-qO-", "http://127.0.0.1:8080/cgi-bin/cert")
            observation["certificate"] = json.loads(response)
        except (subprocess.CalledProcessError, json.JSONDecodeError):
            observation["error"] = "Pod metadata endpoint unavailable"
        observations.append(observation)
    certificate = json.loads(kubectl("get", "certificate", "demo-tls", "-o", "json"))
    issuer = json.loads(kubectl("get", "issuer", "vault", "-o", "json"))
    # Server output contains only selected metadata; Secret values never leave kubectl.
    inventory = kubectl("get", "secrets", "-o", 'jsonpath={range .items[*]}{.metadata.name}{"\\t"}{.type}{"\\n"}{end}')
    return {
        "observed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "context": CONTEXT,
        "pods": observations,
        "certificate_status": certificate.get("status", {}),
        "issuer_status": issuer.get("status", {}),
        "secrets": [dict(zip(("name", "type"), line.split("\t"))) for line in inventory.splitlines()],
    }


def compare(before, after, mode):
    previous = [p["certificate"] for p in before["pods"] if "certificate" in p]
    current = [p["certificate"] for p in after["pods"] if "certificate" in p]
    assert previous and current, "Missing usable pod observations"
    assert len(current) == len(after["pods"]), "A current pod has no certificate observation"
    old = {(p["serial"], p["public_key_sha256"]) for p in previous}
    new = {(p["serial"], p["public_key_sha256"]) for p in current}
    assert len(new) == 1, "Replicas have not converged on one identity"
    assert all(p["valid"] for p in current), "An observed certificate is outside validity"
    if mode == "reuse":
        assert new == old, "Certificate/key changed during expected reuse"
    else:
        assert not ({p["serial"] for p in current} & {p["serial"] for p in previous}), "Serial did not rotate"
        assert not ({p["public_key_sha256"] for p in current} & {p["public_key_sha256"] for p in previous}), "Always rotation did not change key"
    return {"passed": True, "assertion": mode, "scope": "observed pod certificate and public-key identity only; upstream counters must be compared separately"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Save sanitized observation JSON")
    parser.add_argument("--before", type=Path)
    parser.add_argument("--expect", choices=("reuse", "rotation"))
    args = parser.parse_args()
    if bool(args.before) != bool(args.expect):
        parser.error("--before and --expect must be used together")
    result = snapshot()
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
    if args.before:
        result["comparison"] = compare(json.loads(args.before.read_text()), result, args.expect)
        if args.output:
            args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))
