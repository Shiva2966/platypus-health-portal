"""Application-level encryption at rest (HARD agent).

* Document file bytes (``document_blobs.data``) are stored as AES-256-GCM ciphertext when
  ``DATA_ENCRYPTION_KEY`` is set (32 random bytes, urlsafe-base64 or hex). The ``EncryptedBlob`` column type
  encrypts on write and decrypts on read, so ORM code keeps seeing plaintext bytes.
* Stored format, version 1:  MAGIC(4) | version(1) | key_id(4) | nonce(12) | ciphertext+tag
  The header is the AEAD associated data. Rows without MAGIC are legacy plaintext and are returned unchanged,
  so old rows keep working until ``scripts/encrypt_existing_blobs.py`` converts them.
* Key rotation: put retired keys in ``DATA_ENCRYPTION_KEYS_OLD`` (comma list); they are used for reading only.
* Backup files: ``encrypt_file`` / ``decrypt_file`` (chunked AES-256-GCM with a final-chunk flag, so truncation
  is detected) with ``BACKUP_ENCRYPTION_KEY`` or, if unset, ``DATA_ENCRYPTION_KEY``.

LOSING THE KEY MEANS LOSING EVERY ENCRYPTED FILE AND ENCRYPTED BACKUP. Keep a copy of .env's keys offline
(password manager / printed in a safe), separate from the backups.
"""
from __future__ import annotations

import base64
import binascii
import hashlib
import os
import secrets
import struct
from pathlib import Path

from sqlalchemy import LargeBinary
from sqlalchemy.types import TypeDecorator

MAGIC = b"\x00HPE"
VERSION = 1
_HDR_LEN = len(MAGIC) + 1 + 4 + 12

FILE_MAGIC = b"HPBK"
FILE_VERSION = 1
FILE_CHUNK = 1024 * 1024
FILE_SUFFIX = ".enc"


class KeyMaterialError(RuntimeError):
    """Bad or missing key material."""


class DecryptionError(RuntimeError):
    """Ciphertext could not be authenticated (wrong key, corruption or tampering)."""


def generate_key() -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(32)).decode().rstrip("=")


def parse_key(raw: str) -> bytes:
    raw = (raw or "").strip()
    if not raw:
        raise KeyMaterialError("empty key")
    if len(raw) == 64:
        try:
            return bytes.fromhex(raw)
        except ValueError:
            pass
    try:
        key = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (binascii.Error, ValueError) as e:
        raise KeyMaterialError("key is not valid base64/hex") from e
    if len(key) != 32:
        raise KeyMaterialError("key must be 32 bytes (AES-256)")
    return key


def key_id(key: bytes) -> bytes:
    return hashlib.sha256(b"hp-data-key-id:" + key).digest()[:4]


def _env_keys(primary: str, *, fallback: str | None = None, old: str | None = None) -> tuple[bytes | None, dict[bytes, bytes]]:
    raw = (os.environ.get(primary) or "").strip() or ((os.environ.get(fallback) or "").strip() if fallback else "")
    current = parse_key(raw) if raw else None
    keys: dict[bytes, bytes] = {}
    if current:
        keys[key_id(current)] = current
    for extra in ((os.environ.get(old) or "") if old else "").split(","):
        if extra.strip():
            k = parse_key(extra)
            keys.setdefault(key_id(k), k)
    if fallback and primary != fallback and (os.environ.get(fallback) or "").strip():
        k = parse_key(os.environ[fallback])
        keys.setdefault(key_id(k), k)
    return current, keys


def data_keys() -> tuple[bytes | None, dict[bytes, bytes]]:
    return _env_keys("DATA_ENCRYPTION_KEY", old="DATA_ENCRYPTION_KEYS_OLD")


def backup_keys() -> tuple[bytes | None, dict[bytes, bytes]]:
    return _env_keys("BACKUP_ENCRYPTION_KEY", fallback="DATA_ENCRYPTION_KEY", old="DATA_ENCRYPTION_KEYS_OLD")


def encryption_enabled() -> bool:
    return bool((os.environ.get("DATA_ENCRYPTION_KEY") or "").strip())


def _aesgcm(key: bytes):
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    return AESGCM(key)


# ------------------------------------------------------------------ blobs

def is_encrypted(stored: bytes | None) -> bool:
    return bool(stored) and bytes(stored[:4]) == MAGIC


def encrypt_blob(plain: bytes, key: bytes | None = None) -> bytes:
    """Encrypt with the current DATA_ENCRYPTION_KEY; returns the input unchanged when no key is configured."""
    if key is None:
        key, _ = data_keys()
    if key is None:
        return plain
    nonce = secrets.token_bytes(12)
    header = MAGIC + bytes([VERSION]) + key_id(key) + nonce
    return header + _aesgcm(key).encrypt(nonce, bytes(plain), header)


def decrypt_blob(stored: bytes, keys: dict[bytes, bytes] | None = None) -> bytes:
    stored = bytes(stored)
    if not is_encrypted(stored):
        return stored  # legacy plaintext row
    if len(stored) < _HDR_LEN + 16 or stored[4] != VERSION:
        raise DecryptionError("unsupported or truncated encrypted blob")
    header, body = stored[:_HDR_LEN], stored[_HDR_LEN:]
    kid, nonce = header[5:9], header[9:21]
    if keys is None:
        _, keys = data_keys()
    key = keys.get(kid)
    if key is None:
        raise DecryptionError("no DATA_ENCRYPTION_KEY matches this file (key missing or changed)")
    from cryptography.exceptions import InvalidTag

    try:
        return _aesgcm(key).decrypt(nonce, body, header)
    except InvalidTag as e:
        raise DecryptionError("encrypted file failed authentication") from e


class EncryptedBlob(TypeDecorator):
    """LargeBinary that is AES-256-GCM encrypted at rest when DATA_ENCRYPTION_KEY is set."""

    impl = LargeBinary
    cache_ok = True

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        value = bytes(value)
        return value if is_encrypted(value) else encrypt_blob(value)

    def process_result_value(self, value, dialect):
        return None if value is None else decrypt_blob(value)


# ------------------------------------------------------------------ files (backups)

def is_encrypted_file(path: str | Path) -> bool:
    with open(path, "rb") as f:
        return f.read(4) == FILE_MAGIC


def encrypt_file(src: str | Path, dst: str | Path, key: bytes | None = None) -> Path:
    if key is None:
        key, _ = backup_keys()
    if key is None:
        raise KeyMaterialError("set BACKUP_ENCRYPTION_KEY or DATA_ENCRYPTION_KEY to encrypt backups")
    aes = _aesgcm(key)
    base = secrets.token_bytes(8)
    header = FILE_MAGIC + bytes([FILE_VERSION]) + key_id(key) + base
    dst = Path(dst)
    tmp = dst.with_name(dst.name + ".partial")
    with open(src, "rb") as fi, open(tmp, "wb") as fo:
        fo.write(header)
        idx = 0
        chunk = fi.read(FILE_CHUNK)
        while True:
            nxt = fi.read(FILE_CHUNK)
            final = not nxt
            nonce = base + struct.pack(">I", idx)
            ct = aes.encrypt(nonce, chunk, header + struct.pack(">IB", idx, 1 if final else 0))
            fo.write(struct.pack(">IB", len(ct), 1 if final else 0) + ct)
            if final:
                break
            chunk, idx = nxt, idx + 1
    os.replace(tmp, dst)
    return dst


def decrypt_file(src: str | Path, dst: str | Path, keys: dict[bytes, bytes] | None = None) -> Path:
    from cryptography.exceptions import InvalidTag

    if keys is None:
        _, keys = backup_keys()
    dst = Path(dst)
    tmp = dst.with_name(dst.name + ".partial")
    with open(src, "rb") as fi:
        header = fi.read(4 + 1 + 4 + 8)
        if len(header) != 17 or header[:4] != FILE_MAGIC or header[4] != FILE_VERSION:
            raise DecryptionError("not an encrypted backup file")
        key = keys.get(header[5:9])
        if key is None:
            raise DecryptionError("no BACKUP_ENCRYPTION_KEY/DATA_ENCRYPTION_KEY matches this backup")
        aes, base = _aesgcm(key), header[9:17]
        idx, done = 0, False
        try:
            with open(tmp, "wb") as fo:
                while not done:
                    meta = fi.read(5)
                    if len(meta) != 5:
                        raise DecryptionError("backup file is truncated")
                    n, final = struct.unpack(">IB", meta)
                    ct = fi.read(n)
                    if len(ct) != n:
                        raise DecryptionError("backup file is truncated")
                    try:
                        fo.write(aes.decrypt(base + struct.pack(">I", idx), ct, header + struct.pack(">IB", idx, final)))
                    except InvalidTag as e:
                        raise DecryptionError("backup file failed authentication (wrong key or corrupted)") from e
                    done, idx = bool(final), idx + 1
                if fi.read(1):
                    raise DecryptionError("unexpected data after the final chunk")
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
    os.replace(tmp, dst)
    return dst
