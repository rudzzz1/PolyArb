"""Tests for Credential Management and Encrypted KeyStore (Phase 1.2)."""
from __future__ import annotations
import os
import tempfile
from pathlib import Path
from polyarb.credentials import KeyStore, PolymarketCredentials, KalshiCredentials, _mask


def test_credential_masking():
    # Verify raw secret strings are never exposed in plaintext
    pk = "0x4f3edf983ac636a65a842ce7c78d9aa706d3b113bce9c46f30d7d21715b23b1d"
    masked = _mask(pk)
    assert masked.startswith("0x4f")
    assert masked.endswith("3b1d")
    assert "..." in masked
    assert pk not in repr(PolymarketCredentials(private_key=pk))

    kalshi_key = "KALSHI-KEY-894172-PRODUCTION"
    assert "..." in _mask(kalshi_key)
    assert kalshi_key not in repr(KalshiCredentials(key_id=kalshi_key))


def test_keystore_encrypt_decrypt_roundtrip():
    with tempfile.TemporaryDirectory() as tmp:
        ks = KeyStore(data_dir=tmp)
        ks.set_polymarket(
            private_key="0xabc1234567890abcdef1234567890abcdef123456",
            api_key="poly-api-key-100",
            api_secret="poly-secret-xyz",
            api_passphrase="poly-passphrase",
        )
        ks.set_kalshi(
            key_id="kalshi-key-id-999",
            private_key_pem="-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQ...\n-----END RSA PRIVATE KEY-----",
            api_secret="kalshi-secret-abc",
        )

        keystore_file = Path(tmp) / "test_keystore.enc"
        ks.save(passphrase="correct-battery-staple", file_path=keystore_file)
        assert keystore_file.exists()
        assert keystore_file.stat().st_size > 64

        # Read back with matching passphrase
        ks2 = KeyStore(data_dir=tmp)
        success = ks2.load(passphrase="correct-battery-staple", file_path=keystore_file)
        assert success is True

        poly2 = ks2.get_polymarket()
        assert poly2.private_key == "0xabc1234567890abcdef1234567890abcdef123456"
        assert poly2.api_key == "poly-api-key-100"

        kalshi2 = ks2.get_kalshi()
        assert kalshi2.key_id == "kalshi-key-id-999"
        assert "RSA PRIVATE KEY" in kalshi2.private_key_pem


def test_keystore_wrong_passphrase_fails():
    with tempfile.TemporaryDirectory() as tmp:
        ks = KeyStore(data_dir=tmp)
        ks.set_polymarket(private_key="0xsecret123")
        f = Path(tmp) / "keystore.enc"
        ks.save(passphrase="secret-password", file_path=f)

        ks2 = KeyStore(data_dir=tmp)
        try:
            ks2.load(passphrase="WRONG-PASSWORD", file_path=f)
            assert False, "Should have raised ValueError on wrong passphrase"
        except ValueError as e:
            assert "Decryption failed" in str(e)


def test_environment_variable_overrides():
    ks = KeyStore()
    os.environ["POLYARB_POLY_PRIVATE_KEY"] = "0xenv_override_key"
    os.environ["POLYARB_KALSHI_KEY_ID"] = "env_kalshi_key"

    try:
        p = ks.get_polymarket()
        assert p.private_key == "0xenv_override_key"

        k = ks.get_kalshi()
        assert k.key_id == "env_kalshi_key"
    finally:
        del os.environ["POLYARB_POLY_PRIVATE_KEY"]
        del os.environ["POLYARB_KALSHI_KEY_ID"]


if __name__ == "__main__":
    test_credential_masking()
    print("ok test_credential_masking")
    test_keystore_encrypt_decrypt_roundtrip()
    print("ok test_keystore_encrypt_decrypt_roundtrip")
    test_keystore_wrong_passphrase_fails()
    print("ok test_keystore_wrong_passphrase_fails")
    test_environment_variable_overrides()
    print("ok test_environment_variable_overrides")
