import base64
import tempfile
import unittest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.x509.oid import NameOID
from server import Store, experiment


class CertificateContractTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = Store(self.directory.name, 720)
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        csr = (x509.CertificateSigningRequestBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'demo.exp1.test')]))
               .add_extension(x509.SubjectAlternativeName([x509.DNSName('demo.exp1.test'), x509.DNSName('alias.exp1.test')]), False)
               .sign(self.key, hashes.SHA256()))
        self.body = {'PolicyDN': '\\VED\\Policy\\cm', 'PKCS10': csr.public_bytes(serialization.Encoding.PEM).decode()}

    def test_real_signature_key_sans_validity_and_no_dedup(self):
        first = self.store.issue(self.body)['CertificateDN']
        second = self.store.issue(self.body)['CertificateDN']
        self.assertNotEqual(first, second)
        cert = x509.load_pem_x509_certificate(self.store.state['certificates'][first]['pem'].encode())
        self.store.ca.public_key().verify(cert.signature, cert.tbs_certificate_bytes, padding.PKCS1v15(), cert.signature_hash_algorithm)
        self.assertEqual(cert.public_key().public_numbers(), self.key.public_key().public_numbers())
        self.assertEqual(cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value.get_values_for_type(x509.DNSName), ['demo.exp1.test', 'alias.exp1.test'])
        self.assertEqual((cert.not_valid_after_utc - cert.not_valid_before_utc).total_seconds(), 720)
        self.assertEqual(self.store.state['counters']['exp1']['issuance'], 2)

    def test_persistence_and_public_state(self):
        self.store.issue(self.body)
        reloaded = Store(self.directory.name, 720)
        self.assertEqual(reloaded.ca_pem, self.store.ca_pem)
        self.assertEqual(reloaded.state, self.store.state)
        self.assertNotIn('pem', reloaded.public_state()['certificates'][0])

    def test_bad_csr_rejected_without_issuance(self):
        with self.assertRaises(ValueError):
            self.store.issue({'PKCS10': 'bad CSR'})
        csr = x509.load_pem_x509_csr(self.body['PKCS10'].encode())
        der = bytearray(csr.public_bytes(serialization.Encoding.DER))
        der[-1] ^= 1
        broken = b'-----BEGIN CERTIFICATE REQUEST-----\n' + base64.encodebytes(der) + b'-----END CERTIFICATE REQUEST-----\n'
        with self.assertRaisesRegex(ValueError, 'Invalid CSR signature'):
            self.store.issue({'PKCS10': broken.decode()})
        self.assertEqual(self.store.state['certificates'], {})

    def test_classification(self):
        self.assertEqual(experiment('Bearer lab-token-cm'), 'exp1')
        self.assertEqual(experiment('\\VED\\Policy\\agent'), 'exp2')
        self.assertEqual(experiment('Bearer lab-token'), 'shared')

class HTTPSContractTest(CertificateContractTest):
    def test_https_websdk_round_trip_and_auth(self):
        import json
        import ssl
        import threading
        import urllib.request
        import urllib.error
        from http.server import ThreadingHTTPServer
        from server import handler_class
        from unittest.mock import patch
        with patch('socket.getfqdn', return_value='localhost'):
            server = ThreadingHTTPServer(('127.0.0.1', 0), handler_class(self.store))
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(self.store.directory / 'server.pem', self.store.directory / 'server.key')
        server.socket = context.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        client_context = ssl.create_default_context(cadata=self.store.ca_pem)
        base = 'https://127.0.0.1:' + str(server.server_port)

        def call(path, payload=None, token='lab-token-cm'):
            request = urllib.request.Request(base + path, data=json.dumps(payload).encode() if payload is not None else None,
                headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, context=client_context, timeout=5) as response:
                return json.load(response)

        with self.assertRaises(urllib.error.HTTPError) as rejected:
            call('/vedsdk/Identity/Self', token='bad-token')
        self.assertEqual(rejected.exception.code, 401)
        rejected.exception.close()
        self.assertTrue(call('/vedsdk/Identity/Self')['Identities'])
        self.assertTrue(call('/vedsdk/certificates/checkpolicy', {'PolicyDN': '\\VED\\Policy\\cm'})['Policy']['SubjAltNameDnsAllowed'])
        request = self.body | {'CustomFields': [{'Name': 'Origin', 'Values': ['vault']}]}
        dn = call('/vedsdk/certificates/request', request)['CertificateDN']
        metadata = call('/vedsdk/metadata/get', {'DN': dn})
        self.assertEqual(metadata['Data'][0]['Value'], ['vault'])
        result = call('/vedsdk/certificates/retrieve', {'CertificateDN': dn, 'IncludeChain': True})
        certs = x509.load_pem_x509_certificates(base64.b64decode(result['CertificateData']))
        self.assertEqual(len(certs), 2)
        self.assertEqual(certs[0].public_key().public_numbers(), self.key.public_key().public_numbers())
        state = call('/api/state')
        self.assertEqual(state['counters']['exp1'], {'issuance': 1, 'retrieval': 1, 'auth': 1, 'policy': 1})


if __name__ == '__main__':
    unittest.main()
