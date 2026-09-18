#!/usr/bin/env python3
"""Read-only, sanitized pod/certificate/Secret evidence. Never emits PEM or tokens."""
import argparse, base64, datetime, json, subprocess, time
CONTEXT = 'kind-venafi-lab-agent'
def k(*args):
    return subprocess.check_output(['kubectl', '--context', CONTEXT, '-n', 'demo', *args], text=True)
def snapshot():
    pods = json.loads(k('get', 'pods', '-l', 'app=demo', '-o', 'json'))['items']
    observations = []
    for pod in pods:
        name = pod['metadata']['name']
        entry = {'name': name, 'uid': pod['metadata']['uid'], 'phase': pod['status']['phase'],
                 'containers': [{key: item.get(key) for key in ('name','ready','restartCount')} for item in pod['status'].get('containerStatuses', [])],
                 'volumes': [{'name': v['name'], 'emptyDir': v.get('emptyDir')} for v in pod['spec'].get('volumes', []) if 'emptyDir' in v]}
        if pod['metadata'].get('deletionTimestamp'):
            entry['terminating_since'] = pod['metadata']['deletionTimestamp']
            observations.append(entry)
            continue
        try:
            entry['certificate'] = json.loads(k('exec', name, '-c', 'app', '--', 'wget', '-qO-', 'http://127.0.0.1:8080/cgi-bin/cert'))
            # Check certificate/key consistency inside pod; return only a boolean.
            match = k('exec', name, '-c', 'app', '--', 'sh', '-c',
                      'set -eu; set -o pipefail; c=$(openssl x509 -in /vault/secrets/bundle.pem -pubkey -noout | openssl pkey -pubin -outform DER | openssl dgst -sha256); '
                      'p=$(openssl pkey -in /vault/secrets/bundle.pem -pubout -outform DER | openssl dgst -sha256); '
                      '[ "$c" = "$p" ] && echo true')
            entry['certificate_key_match'] = match.strip() == 'true'
        except (subprocess.CalledProcessError, json.JSONDecodeError) as exc:
            entry['observation_error'] = str(exc)
        observations.append(entry)
    inventory = []
    for kind in ('secrets', 'configmaps'):
        for obj in json.loads(k('get', kind, '-o', 'json'))['items']:
            values = obj.get('data', {})
            material = []
            for key, value in values.items():
                raw = base64.b64decode(value) if kind == 'secrets' else value.encode()
                if b'-----BEGIN ' in raw and (b'PRIVATE KEY-----' in raw or b'CERTIFICATE-----' in raw):
                    material.append(key)
            inventory.append({'kind': kind, 'name': obj['metadata']['name'], 'type': obj.get('type'), 'pem_fields': material})
    return {'observed_at': datetime.datetime.now(datetime.timezone.utc).isoformat(), 'context': CONTEXT, 'pods': observations, 'inventory': inventory}
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=0, help='Observation duration; zero makes one snapshot')
    parser.add_argument('--interval', type=int, default=10)
    args = parser.parse_args()
    deadline = time.monotonic() + args.seconds
    while True:
        print(json.dumps(snapshot()), flush=True)
        if time.monotonic() >= deadline:
            break
        time.sleep(max(1, args.interval))
