import copy
import ssl
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from aiohttp import web

from cfoptimizer.config import DEFAULTS
from cfoptimizer.models import Endpoint, Geo, Node
from cfoptimizer.probe import verify
from cfoptimizer.trace import TRACE_PATH

try:
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
except ImportError:
    x509 = None


@unittest.skipIf(x509 is None, "Install requirements-dev.txt to test TLS/SNI")
class TLSTests(unittest.IsolatedAsyncioTestCase):
    async def test_candidate_tls_uses_domain_sni_and_certificate_checks(self):
        with tempfile.TemporaryDirectory() as folder:
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "zone.invalid")])
            now = datetime.now(timezone.utc)
            certificate = (
                x509.CertificateBuilder()
                .subject_name(name)
                .issuer_name(name)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(now - timedelta(minutes=1))
                .not_valid_after(now + timedelta(days=1))
                .add_extension(x509.SubjectAlternativeName([x509.DNSName("zone.invalid")]), critical=False)
                .sign(key, hashes.SHA256())
            )
            cert_file, key_file = Path(folder) / "cert.pem", Path(folder) / "key.pem"
            cert_file.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_file.write_bytes(
                key.private_bytes(
                    serialization.Encoding.PEM,
                    serialization.PrivateFormat.PKCS8,
                    serialization.NoEncryption(),
                )
            )
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_file, key_file)
            sni_values, hosts = [], []
            context.set_servername_callback(lambda socket, name, context: sni_values.append(name))
            app = web.Application()

            async def handler(request):
                hosts.append(request.host)
                tls = request.transport.get_extra_info("ssl_object").version()
                return web.Response(
                    text=(
                        f"h={request.host}\nip={request.remote}\nuag={request.headers['User-Agent']}\n"
                        f"visit_scheme=https\ntls={tls}\ncolo=SIN\n"
                    ),
                    headers={"Server": "cloudflare", "CF-Ray": "test-SIN"},
                )

            app.router.add_get(TRACE_PATH, handler)
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, "127.0.0.1", 0, ssl_context=context)
            await site.start()
            port = site._server.sockets[0].getsockname()[1]
            node = Node(Endpoint("127.0.0.1", port), Geo("SG", "新加坡", 0))
            cfg = copy.deepcopy(DEFAULTS["zone"])
            cfg.update(domain="zone.invalid", rounds=1, verify_tls=False)
            try:
                result, reason = await verify(node, cfg)
                self.assertIsNotNone(result, reason)
                self.assertEqual(sni_values[0], "zone.invalid")
                self.assertEqual(hosts, [f"zone.invalid:{port}"])
                cfg["verify_tls"] = True
                result, reason = await verify(node, cfg)
                self.assertIsNone(result)
                self.assertIn("Certificate", reason)
            finally:
                await runner.cleanup()
