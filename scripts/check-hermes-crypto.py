"""Check the managed Hermes cryptography wheel and its TLS interoperability."""

import datetime
import ctypes
import pathlib
import re
import ssl
import tempfile

import cryptography
import brotlicffi
from brotlicffi import _brotlicffi
from cryptography import x509
from cryptography.hazmat.backends.openssl.backend import backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID


def version(text):
    return tuple(map(int, re.search(r"(\d+)\.(\d+)\.(\d+)", text).groups()))


assert version(cryptography.__version__) >= (50, 0, 2), cryptography.__version__
openssl = backend.openssl_version_text()
assert version(openssl) >= (4, 0, 3), openssl

brotli = ctypes.CDLL(_brotlicffi.__file__).BrotliDecoderVersion
brotli.restype = ctypes.c_uint
assert brotli() >= 0x1002001, hex(brotli())
compressed = brotlicffi.compress(b"Hermes Brotli security check")
assert brotlicffi.decompress(compressed) == b"Hermes Brotli security check"
assert brotlicffi.Decompressor().process(compressed) == b"Hermes Brotli security check"
try:
    brotlicffi.decompress(compressed + b"trailing bytes")
except brotlicffi.error:
    pass
else:
    raise AssertionError("Brotli accepted trailing bytes")

cipher = AESGCM(AESGCM.generate_key(bit_length=128))
nonce = b"securitytest"
message = b"Hermes runtime security check"
assert cipher.decrypt(nonce, cipher.encrypt(nonce, message, None), None) == message

key = ec.generate_private_key(ec.SECP256R1())
name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
now = datetime.datetime.now(datetime.timezone.utc)
cert = (
    x509.CertificateBuilder()
    .subject_name(name)
    .issuer_name(name)
    .public_key(key.public_key())
    .serial_number(1)
    .not_valid_before(now - datetime.timedelta(days=1))
    .not_valid_after(now + datetime.timedelta(days=1))
    .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost")]), critical=False)
    .sign(key, hashes.SHA256())
)
cert_pem = cert.public_bytes(serialization.Encoding.PEM)
key_pem = key.private_bytes(
    serialization.Encoding.PEM,
    serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption(),
)

with tempfile.TemporaryDirectory() as directory:
    cert_path = pathlib.Path(directory) / "cert.pem"
    key_path = pathlib.Path(directory) / "key.pem"
    cert_path.write_bytes(cert_pem)
    key_path.write_bytes(key_pem)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    client_context = ssl.create_default_context(cadata=cert_pem.decode())
    client_in, client_out, server_in, server_out = [ssl.MemoryBIO() for _ in range(4)]
    client = client_context.wrap_bio(client_in, client_out, server_hostname="localhost")
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    complete = set()
    for _ in range(20):
        for connection in (client, server):
            try:
                connection.do_handshake()
                complete.add(connection)
            except ssl.SSLWantReadError:
                pass
        server_in.write(client_out.read())
        client_in.write(server_out.read())
        if len(complete) == 2:
            break
    assert len(complete) == 2, "TLS handshake did not complete"
    client.write(message)
    server_in.write(client_out.read())
    assert server.read(len(message)) == message

print(f"cryptography {cryptography.__version__}: {openssl}; AES-GCM, X.509 and TLS checks passed")
print(f"brotlicffi {brotlicffi.__version__}: native Brotli {hex(brotli())}; decoder checks passed")
