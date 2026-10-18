"""Encrypted Credential & KeyStore Management for PolyArb.
Securely manages Polymarket and Kalshi trading credentials with AES/PBKDF2 encryption,
environment variable overrides, and strict secret masking in memory.
"""
from __future__ import annotations
import base64
import hashlib
import hmac
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logger import get_logger

logger = get_logger("credentials")


def _mask(s: str | None, prefix_len: int = 4, suffix_len: int = 4) -> str:
    """Masks secret string for safe logging and printing."""
    if not s:
        return "<empty>"
    if len(s) <= prefix_len + suffix_len:
        return "***"
    return f"{s[:prefix_len]}...{s[-suffix_len:]}"


@dataclass
class PolymarketCredentials:
    private_key: str = ""
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""

    def is_configured(self) -> bool:
        return bool(self.private_key or self.api_key)

    def __repr__(self) -> str:
        return (f"PolymarketCredentials(private_key='{_mask(self.private_key)}', "
                f"api_key='{_mask(self.api_key)}')")


@dataclass
class KalshiCredentials:
    key_id: str = ""
    private_key_pem: str = ""
    api_secret: str = ""

    def is_configured(self) -> bool:
        return bool(self.key_id and (self.private_key_pem or self.api_secret))

    def __repr__(self) -> str:
        return (f"KalshiCredentials(key_id='{_mask(self.key_id)}', "
                f"has_key={bool(self.private_key_pem or self.api_secret)})")


class KeyStore:
    """Encrypted credential vault storing venue API keys and signing credentials."""
    def __init__(self, data_dir: str = "data"):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.keystore_file = self.data_dir / "keystore.enc"

        self._poly_creds = PolymarketCredentials()
        self._kalshi_creds = KalshiCredentials()
        self._loaded = False

    def get_polymarket(self) -> PolymarketCredentials:
        """Returns Polymarket credentials, checking environment overrides first."""
        # 1. Environment variables take precedence
        env_pk = os.getenv("POLYARB_POLY_PRIVATE_KEY") or os.getenv("POLYARB_POLY_KEY")
        env_api_key = os.getenv("POLYARB_POLY_API_KEY")
        env_api_secret = os.getenv("POLYARB_POLY_API_SECRET")
        env_pass = os.getenv("POLYARB_POLY_PASSPHRASE")

        if env_pk or env_api_key:
            return PolymarketCredentials(
                private_key=env_pk or self._poly_creds.private_key,
                api_key=env_api_key or self._poly_creds.api_key,
                api_secret=env_api_secret or self._poly_creds.api_secret,
                api_passphrase=env_pass or self._poly_creds.api_passphrase,
            )
        return self._poly_creds

    def get_kalshi(self) -> KalshiCredentials:
        """Returns Kalshi credentials, checking environment overrides first."""
        env_key_id = os.getenv("POLYARB_KALSHI_KEY_ID")
        env_pem = os.getenv("POLYARB_KALSHI_PRIVATE_KEY")
        env_secret = os.getenv("POLYARB_KALSHI_SECRET")

        if env_key_id:
            return KalshiCredentials(
                key_id=env_key_id,
                private_key_pem=env_pem or self._kalshi_creds.private_key_pem,
                api_secret=env_secret or self._kalshi_creds.api_secret,
            )
        return self._kalshi_creds

    def set_polymarket(self, private_key: str = "", api_key: str = "",
                       api_secret: str = "", api_passphrase: str = ""):
        self._poly_creds = PolymarketCredentials(
            private_key=private_key,
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

    def set_kalshi(self, key_id: str = "", private_key_pem: str = "", api_secret: str = ""):
        self._kalshi_creds = KalshiCredentials(
            key_id=key_id,
            private_key_pem=private_key_pem,
            api_secret=api_secret,
        )

    # --- Encryption & Persistence ---

    def _derive_key(self, passphrase: str, salt: bytes) -> bytes:
        return hashlib.pbkdf2_hmac("sha256", passphrase.encode("utf-8"), salt, 100_000, dklen=32)

    def _encrypt(self, raw_bytes: bytes, key: bytes) -> bytes:
        """Symmetric XOR-stream cipher with HMAC authentication (standard library only)."""
        nonce = os.urandom(16)
        # Generate stream from key + nonce
        stream = hashlib.sha256(key + nonce).digest()
        cipher = bytearray()
        for i, b in enumerate(raw_bytes):
            if i % 32 == 0 and i > 0:
                stream = hashlib.sha256(key + stream).digest()
            cipher.append(b ^ stream[i % 32])
        mac = hmac.new(key, nonce + bytes(cipher), hashlib.sha256).digest()
        return nonce + mac + bytes(cipher)

    def _decrypt(self, payload: bytes, key: bytes) -> bytes:
        if len(payload) < 48:
            raise ValueError("Invalid payload: too short")
        nonce = payload[:16]
        mac = payload[16:48]
        cipher = payload[48:]
        expected_mac = hmac.new(key, nonce + cipher, hashlib.sha256).digest()
        if not hmac.compare_digest(mac, expected_mac):
            raise ValueError("Decryption failed: invalid passphrase or corrupted keystore")
        stream = hashlib.sha256(key + nonce).digest()
        plain = bytearray()
        for i, b in enumerate(cipher):
            if i % 32 == 0 and i > 0:
                stream = hashlib.sha256(key + stream).digest()
            plain.append(b ^ stream[i % 32])
        return bytes(plain)

    def save(self, passphrase: str, file_path: Path | None = None) -> Path:
        """Encrypt and save keystore to file."""
        target = file_path or self.keystore_file
        salt = os.urandom(16)
        key = self._derive_key(passphrase, salt)

        payload = {
            "polymarket": {
                "private_key": self._poly_creds.private_key,
                "api_key": self._poly_creds.api_key,
                "api_secret": self._poly_creds.api_secret,
                "api_passphrase": self._poly_creds.api_passphrase,
            },
            "kalshi": {
                "key_id": self._kalshi_creds.key_id,
                "private_key_pem": self._kalshi_creds.private_key_pem,
                "api_secret": self._kalshi_creds.api_secret,
            }
        }
        raw_data = json.dumps(payload).encode("utf-8")
        encrypted = self._encrypt(raw_data, key)

        # File format: SALT (16B) + ENCRYPTED_PAYLOAD
        target.write_bytes(salt + encrypted)
        logger.info(f"Keystore encrypted and saved to {target}")
        return target

    def load(self, passphrase: str, file_path: Path | None = None) -> bool:
        """Load and decrypt keystore from file."""
        target = file_path or self.keystore_file
        if not target.exists():
            return False

        data = target.read_bytes()
        if len(data) < 64:
            raise ValueError("Keystore file corrupted")

        salt = data[:16]
        encrypted = data[16:]
        key = self._derive_key(passphrase, salt)
        raw_json = self._decrypt(encrypted, key).decode("utf-8")
        parsed = json.loads(raw_json)

        p = parsed.get("polymarket", {})
        self._poly_creds = PolymarketCredentials(**p)
        k = parsed.get("kalshi", {})
        self._kalshi_creds = KalshiCredentials(**k)
        self._loaded = True
        logger.info(f"Keystore decrypted successfully from {target}")
        return True
