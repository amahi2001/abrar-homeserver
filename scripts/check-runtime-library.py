#!/usr/bin/env python3
"""Check real patched library versions and small, offline public API operations."""

import ctypes as c
import hashlib
from pathlib import Path
import sys


kind, directory, version = sys.argv[1:]


def load(soname):
    return c.CDLL(str(Path(directory) / soname))


if kind == "brotli":
    encoder = load("libbrotlienc.so.1")
    decoder = load("libbrotlidec.so.1")
    expected = tuple(map(int, version.split(".")))
    decoder.BrotliDecoderVersion.restype = c.c_uint
    assert decoder.BrotliDecoderVersion() == (expected[0] << 24 | expected[1] << 12 | expected[2])
    encoder.BrotliEncoderCompress.argtypes = [c.c_int, c.c_int, c.c_int, c.c_size_t,
                                             c.c_void_p, c.POINTER(c.c_size_t), c.c_void_p]
    decoder.BrotliDecoderDecompress.argtypes = [c.c_size_t, c.c_void_p, c.POINTER(c.c_size_t), c.c_void_p]
    payload = b"Brotli security regression round trip" * 128
    compressed = c.create_string_buffer(len(payload) * 2)
    size = c.c_size_t(len(compressed))
    assert encoder.BrotliEncoderCompress(5, 22, 0, len(payload), payload, c.byref(size), compressed) == 1
    restored = c.create_string_buffer(len(payload))
    restored_size = c.c_size_t(len(restored))
    assert decoder.BrotliDecoderDecompress(size.value, compressed, c.byref(restored_size), restored) == 1
    assert restored.raw[:restored_size.value] == payload
elif kind == "c-ares":
    library = load("libcares.so.2")
    library.ares_version.restype = c.c_char_p
    number = c.c_int()
    assert library.ares_version(c.byref(number)).decode() == version
    assert library.ares_library_init(3) == 0
    library.ares_library_cleanup()
elif kind == "sqlite":
    library = load("libsqlite3.so.0")
    library.sqlite3_libversion.restype = c.c_char_p
    assert library.sqlite3_libversion().decode() == version
    database = c.c_void_p()
    assert library.sqlite3_open(b":memory:", c.byref(database)) == 0
    library.sqlite3_exec.argtypes = [c.c_void_p, c.c_char_p, c.c_void_p, c.c_void_p, c.c_void_p]
    library.sqlite3_close.argtypes = [c.c_void_p]
    try:
        assert library.sqlite3_exec(database, b"CREATE TABLE test (value INTEGER); INSERT INTO test VALUES (42);", None, None, None) == 0
        result = []
        callback_type = c.CFUNCTYPE(c.c_int, c.c_void_p, c.c_int, c.POINTER(c.c_char_p), c.POINTER(c.c_char_p))
        callback = callback_type(lambda _, count, values, names: result.append(values[0].decode()) or 0)
        assert library.sqlite3_exec(database, b"SELECT value FROM test", c.cast(callback, c.c_void_p), None, None) == 0
        assert result == ["42"]
    finally:
        assert library.sqlite3_close(database) == 0
elif kind == "openssl":
    crypto = load("libcrypto.so.3")
    crypto.OpenSSL_version.restype = c.c_char_p
    assert crypto.OpenSSL_version(0).decode().split()[1] == version
    crypto.SHA256.argtypes = [c.c_void_p, c.c_size_t, c.c_void_p]
    digest = c.create_string_buffer(32)
    payload = b"OpenSSL security API check"
    crypto.SHA256(payload, len(payload), digest)
    assert digest.raw == hashlib.sha256(payload).digest()
    ssl = load("libssl.so.3")
    ssl.TLS_method.restype = c.c_void_p
    ssl.SSL_CTX_new.argtypes = [c.c_void_p]
    ssl.SSL_CTX_new.restype = c.c_void_p
    ssl.SSL_CTX_free.argtypes = [c.c_void_p]
    context = ssl.SSL_CTX_new(ssl.TLS_method())
    assert context
    ssl.SSL_CTX_free(context)
else:
    raise SystemExit(f"Unknown library: {kind}")
print(f"{kind} {version}: real library and public API checks passed")
