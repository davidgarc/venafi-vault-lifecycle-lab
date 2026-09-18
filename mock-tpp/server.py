"""Deliberately small TPP WebSDK contract simulator, not a TPP emulator."""
import base64
import ipaddress
import json
import os
from pathlib import Path
import ssl
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID


def experiment(value):
    value = value.lower()
    if any(s in value for s in ('exp1', 'experiment-one', 'experiment-1', 'cert-manager', 'lab-token-cm', '\\cm')):
        return 'exp1'
    if any(s in value for s in ('exp2', 'experiment-two', 'experiment-2', 'injector', 'lab-token-agent', '\\agent')):
        return 'exp2'
    return 'shared'


class Store:
    def __init__(self, directory, lifetime=720):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.lifetime = lifetime
        if lifetime < 1:
            raise ValueError('Certificate lifetime must be positive')
        self.state_path = self.directory / 'state.json'
        self.state = json.loads(self.state_path.read_text()) if self.state_path.exists() else {'counters': {}, 'events': [], 'certificates': {}}
        key_path, ca_path = self.directory / 'ca.key', self.directory / 'ca.pem'
        if key_path.exists() != ca_path.exists():
            raise ValueError('Incomplete CA state; restore both ca.key and ca.pem')
        if key_path.exists():
            self.key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
            self.ca = x509.load_pem_x509_certificate(ca_path.read_bytes())
        else:
            self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            now = datetime.now(timezone.utc)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'Lifecycle Lab Mock TPP CA')])
            self.ca = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self.key.public_key())
                       .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + timedelta(days=3650))
                       .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), False)
                       .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), False)
                       .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
                       .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), True)
                       .sign(self.key, hashes.SHA256()))
            self.write_key(key_path, self.key)
            ca_path.write_bytes(self.ca.public_bytes(serialization.Encoding.PEM))
        self.ca_pem = self.ca.public_bytes(serialization.Encoding.PEM).decode()
        self.create_server_certificate()

    @staticmethod
    def write_key(path, key):
        path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
        path.chmod(0o600)

    def create_server_certificate(self):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        names = ['mock-tpp', 'localhost', 'mock-tpp.vault.svc', 'mock-tpp.vault.svc.cluster.local']
        cert = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'mock-tpp')]))
                .issuer_name(self.ca.subject).public_key(key.public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now).not_valid_after(now + timedelta(days=365))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names] + [x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), False).sign(self.key, hashes.SHA256()))
        self.write_key(self.directory / 'server.key', key)
        (self.directory / 'server.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    def save(self):
        temp = self.state_path.with_suffix('.tmp')
        temp.write_text(json.dumps(self.state))
        temp.replace(self.state_path)

    def record(self, operation, owner='shared', **details):
        counters = self.state['counters'].setdefault(owner, {'issuance': 0, 'retrieval': 0, 'auth': 0, 'policy': 0})
        counters[operation] = counters.get(operation, 0) + 1
        self.state['events'].append({'time': datetime.now(timezone.utc).isoformat(), 'operation': operation, 'experiment': owner, **details})
        self.state['events'] = self.state['events'][-1000:]
        self.save()

    def issue(self, body):
        csr = x509.load_pem_x509_csr(body.get('PKCS10', '').encode())
        if not csr.is_signature_valid:
            raise ValueError('Invalid CSR signature')
        for extension in csr.extensions:
            if isinstance(extension.value, x509.BasicConstraints) and extension.value.ca:
                raise ValueError('CA requests are not supported')
        now = datetime.now(timezone.utc).replace(microsecond=0)
        builder = (x509.CertificateBuilder().subject_name(csr.subject).issuer_name(self.ca.subject).public_key(csr.public_key())
                   .serial_number(x509.random_serial_number()).not_valid_before(now).not_valid_after(now + timedelta(seconds=self.lifetime))
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), False)
                .add_extension(x509.BasicConstraints(ca=False, path_length=None), True)
                   .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]), False))
        try:
            san = csr.extensions.get_extension_for_class(x509.SubjectAlternativeName)
            builder = builder.add_extension(san.value, san.critical)
        except x509.ExtensionNotFound:
            pass
        cert = builder.sign(self.key, hashes.SHA256())
        cn = csr.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
        cn = cn[0].value if cn else ''
        dn = body.get('PolicyDN', '\\VED\\Policy\\Lab') + '\\' + format(cert.serial_number, 'x')
        owner = experiment(body.get('PolicyDN', '') + ' ' + cn)
        self.state['certificates'][dn] = {'pem': cert.public_bytes(serialization.Encoding.PEM).decode(), 'experiment': owner,
            'common_name': cn, 'serial': format(cert.serial_number, 'x'), 'not_before': now.isoformat(),
            'not_after': (now + timedelta(seconds=self.lifetime)).isoformat(), 'custom_fields': body.get('CustomFields', []),
            'requested_ca_attributes': body.get('CASpecificAttributes', [])}
        self.record('issuance', owner, certificate_dn=dn, common_name=cn, serial=format(cert.serial_number, 'x'))
        return {'CertificateDN': dn, 'Guid': format(cert.serial_number, 'x')}

    def public_state(self):
        certificates = [{k: v for k, v in cert.items() if k not in ('pem', 'custom_fields')} | {'certificate_dn': dn}
                        for dn, cert in self.state['certificates'].items()]
        return {'mode': 'TPP API contract simulator', 'actual_lifetime_seconds': self.lifetime,
                'counters': self.state['counters'], 'events': self.state['events'], 'certificates': certificates}


def handler_class(store):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def respond(self, code, payload, content_type='application/json'):
            data = json.dumps(payload).encode() if content_type == 'application/json' else payload.encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def dispatch(self):
            path = urlsplit(self.path).path.lower().rstrip('/')
            try:
                with store.lock:
                    if path == '/ca.pem':
                        return self.respond(200, store.ca_pem, 'application/x-pem-file')
                    if path in ('/healthz', '/health'):
                        return self.respond(200, {'status': 'ok'})
                    if path == '/api/state':
                        return self.respond(200, store.public_state())
                    if path == '':
                        return self.respond(200, '<!doctype html><title>Mock TPP</title><h1>Mock TPP certificate authority</h1><p>API contract simulator; real CSR signing, configurable accelerated certificate policy.</p><pre id="state"></pre><script>async function update(){document.getElementById("state").textContent=JSON.stringify(await(await fetch("/api/state")).json(),null,2)}update();setInterval(update,2000)</script>', 'text/html')
                    if not isinstance(self.connection, ssl.SSLSocket):
                        return self.respond(403, {'Error': 'WebSDK requires HTTPS'})
                    auth = self.headers.get('Authorization', '')
                    if auth not in ('Bearer lab-token', 'Bearer lab-token-exp1', 'Bearer lab-token-exp2', 'Bearer lab-token-cm', 'Bearer lab-token-agent'):
                        return self.respond(401, {'Error': 'Invalid lab token'})
                    length = int(self.headers.get('Content-Length', 0))
                    if length > 1024 * 1024:
                        return self.respond(413, {'Error': 'Request too large'})
                    body = json.loads(self.rfile.read(length)) if length else {}
                    if not isinstance(body, dict):
                        raise ValueError('Expected JSON object')
                    if path == '/vedsdk/identity/self':
                        store.record('auth', experiment(auth))
                        return self.respond(200, {'Identities': [{'Name': 'lab', 'FullName': 'Lifecycle Lab', 'Prefix': 'local', 'PrefixedName': 'local:lab', 'PrefixedUniversal': 'local:lab', 'Universal': 'lab', 'Type': 1}]})
                    if path == '/vedsdk/certificates/checkpolicy':
                        store.record('policy', experiment(body.get('PolicyDN', '')))
                        return self.respond(200, {'Policy': {'CsrGeneration': {'Value': 'UserProvided'}, 'ManagementType': {'Value': 'Enrollment'}, 'KeyPair': {'KeyAlgorithm': {'Value': 'RSA'}, 'KeySize': {'Value': 2048}}, 'PrivateKeyReuseAllowed': True, 'SubjAltNameDnsAllowed': True, 'SubjAltNameIpAllowed': True, 'SubjAltNameEmailAllowed': True, 'SubjAltNameUriAllowed': True, 'WildcardsAllowed': True}})
                    if path == '/vedsdk/certificates/request':
                        return self.respond(200, store.issue(body))
                    if path == '/vedsdk/certificates/retrieve':
                        dn = body.get('CertificateDN', '')
                        cert = store.state['certificates'].get(dn)
                        if cert is None:
                            return self.respond(404, {'Error': 'Unknown CertificateDN'})
                        if body.get('IncludePrivateKey'):
                            return self.respond(400, {'Error': 'Only caller-generated CSR/key mode supported'})
                        chain = store.ca_pem if body.get('IncludeChain') else ''
                        pem = chain + cert['pem'] if body.get('RootFirstOrder') else cert['pem'] + chain
                        store.record('retrieval', cert['experiment'], certificate_dn=dn, serial=cert['serial'])
                        return self.respond(200, {'CertificateData': base64.b64encode(pem.encode()).decode(), 'Format': 'base64', 'Status': 'Issued', 'Stage': 800})
                    if path == '/vedsdk/metadata/get':
                        cert = store.state['certificates'].get(body.get('DN', ''), {})
                        return self.respond(200, {'Data': [{'Key': {'Label': field['Name']}, 'Value': field['Values']} for field in cert.get('custom_fields', [])]})
                    if path == '/vedsdk/systemstatus/version':
                        return self.respond(200, {'Version': '25.1.0'})
                    return self.respond(404, {'Error': 'Unsupported mock endpoint', 'path': path})
            except (ValueError, TypeError, KeyError) as exc:
                self.respond(400, {'Error': str(exc)})
    return Handler


def main():
    store = Store(os.getenv('DATA_DIR', '/data'), int(os.getenv('CERT_LIFETIME_SECONDS', '720')))
    handler = handler_class(store)
    http = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('HTTP_PORT', '8080'))), handler)
    https = ThreadingHTTPServer(('0.0.0.0', int(os.getenv('HTTPS_PORT', '8443'))), handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(store.directory / 'server.pem', store.directory / 'server.key')
    https.socket = context.wrap_socket(https.socket, server_side=True)
    threading.Thread(target=http.serve_forever, daemon=True).start()
    https.serve_forever()


if __name__ == '__main__':
    main()
