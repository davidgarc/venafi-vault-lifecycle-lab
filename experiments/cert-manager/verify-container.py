#!/usr/bin/env python3
"""Restart only the demo app container and validate mounted public identity."""
import json
import subprocess
import sys
import time
from pathlib import Path

from observe import compare, kubectl, snapshot

output = Path(sys.argv[1] if len(sys.argv) > 1 else "artifacts/cm-container")
output.mkdir(parents=True, exist_ok=True)


def save(name, value):
    (output / name).write_text(json.dumps(value, indent=2) + "\n")


before = snapshot()
save("before.json", before)
assert len(before["pods"]) == 1, "Run with one active replica"
pod = before["pods"][0]["pod_name"]
status_before = json.loads(kubectl("get", "pod", pod, "-o", "json"))
container_before = next(c for c in status_before["status"]["containerStatuses"] if c["name"] == "app")
# Key bytes remain inside the container; only SHA-256 of its public key is returned.
validation = kubectl("exec", pod, "-c", "app", "--", "sh", "-ec", """
cert_hash=$(openssl x509 -in /certs/tls.crt -pubkey -noout | openssl pkey -pubin -outform DER | sha256sum | cut -d' ' -f1)
key_hash=$(openssl pkey -in /certs/tls.key -pubout -outform DER | sha256sum | cut -d' ' -f1)
test "$cert_hash" = "$key_hash"
openssl verify -CAfile /certs/ca.crt -verify_hostname demo.cm.test /certs/tls.crt
openssl x509 -in /certs/tls.crt -noout -checkend 0
printf 'matching_public_key_sha256=%s\\n' "$cert_hash"
""")
save("cryptographic-validation.json", {"passed": True, "checks": ["private key matches certificate public key", "chain validates against mounted CA", "hostname matches demo.cm.test", "certificate currently valid"], "public_output": validation})
# Linux namespace PID 1 ignores unhandled signals from the same namespace.
# Stop exactly this container through its dedicated kind node's CRI instead.
node = status_before["spec"]["nodeName"]
assert node == "venafi-lab-cm-control-plane", "Unexpected node: refusing runtime operation"
container_id = container_before["containerID"].removeprefix("containerd://")
assert container_id.isalnum(), "Unexpected CRI container ID"
subprocess.run(["docker", "exec", node, "crictl", "stop", "--timeout", "1", container_id], check=True, stdout=subprocess.DEVNULL)
for attempt in range(45):
    status_after = json.loads(kubectl("get", "pod", pod, "-o", "json"))
    container_after = next(c for c in status_after["status"]["containerStatuses"] if c["name"] == "app")
    if container_after["restartCount"] > container_before["restartCount"] and container_after["ready"]:
        break
    time.sleep(2)
else:
    raise AssertionError("Container did not restart and become Ready")
after = snapshot()
save("after.json", after)
result = compare(before, after, "reuse")
assert status_before["metadata"]["uid"] == status_after["metadata"]["uid"]
result.update({"pod_uid": status_after["metadata"]["uid"], "restart_count_before": container_before["restartCount"], "restart_count_after": container_after["restartCount"], "container_id_before": container_before["containerID"], "container_id_after": container_after["containerID"]})
save("result.json", result)
print(json.dumps(result, indent=2))
