# -*- coding: utf-8 -*-
"""Tests for b2b_ai.features.declaraciones.fiel_signer — FIEL digital signing."""
import base64
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from b2b_ai.features.declaraciones.fiel_signer import (
    FIELSigner,
    FIELSignerError,
    CertificateInfo,
    validate_rfc_with_digit,
)


# --- Fixtures ---

@pytest.fixture()
def rsa_key():
    """Generate a test RSA private key."""
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture()
def self_signed_cert(rsa_key):
    """Generate a self-signed X.509 certificate for testing."""
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Test FIEL"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Test Org"),
        x509.NameAttribute(NameOID.SERIAL_NUMBER, "XEXX010101000"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(rsa_key.public_key())
        .serial_number(12345678901234567890)
        .not_valid_before(datetime.utcnow())
        .not_valid_after(datetime.utcnow() + timedelta(days=365))
        .sign(rsa_key, hashes.SHA256())
    )
    return cert


@pytest.fixture()
def expired_cert(rsa_key):
    """Generate an expired certificate."""
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, "Expired FIEL"),
    ])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(rsa_key.public_key())
        .serial_number(99999999999999999999)
        .not_valid_before(datetime.utcnow() - timedelta(days=730))
        .not_valid_after(datetime.utcnow() - timedelta(days=1))
        .sign(rsa_key, hashes.SHA256())
    )
    return cert


@pytest.fixture()
def signer(rsa_key, self_signed_cert):
    """Create a FIELSigner with test key and cert."""
    info = CertificateInfo(
        serial_number=str(self_signed_cert.serial_number),
        no_certificado=str(self_signed_cert.serial_number)[-20:],
        issuer=self_signed_cert.issuer.rfc4514_string(),
        subject=self_signed_cert.subject.rfc4514_string(),
        not_valid_before=self_signed_cert.not_valid_before,
        not_valid_after=self_signed_cert.not_valid_after,
        is_expired=False,
        days_until_expiry=365,
    )
    return FIELSigner(
        certificate=self_signed_cert.public_bytes(serialization.Encoding.DER),
        private_key=rsa_key,
        certificate_info=info,
    )


@pytest.fixture()
def expired_signer(rsa_key, expired_cert):
    """Create a FIELSigner with an expired cert."""
    info = CertificateInfo(
        serial_number=str(expired_cert.serial_number),
        no_certificado=str(expired_cert.serial_number)[-20:],
        issuer=expired_cert.issuer.rfc4514_string(),
        subject=expired_cert.subject.rfc4514_string(),
        not_valid_before=expired_cert.not_valid_before,
        not_valid_after=expired_cert.not_valid_after,
        is_expired=True,
        days_until_expiry=0,
    )
    return FIELSigner(
        certificate=expired_cert.public_bytes(serialization.Encoding.DER),
        private_key=rsa_key,
        certificate_info=info,
    )


# --- Tests ---

class TestFIELSignerSigning:
    def test_sign_returns_bytes(self, signer):
        """sign() should return bytes."""
        sig = signer.sign(b"test data to sign")
        assert isinstance(sig, bytes)
        assert len(sig) > 0

    def test_sign_b64_returns_base64(self, signer):
        """sign_b64() should return valid base64 string."""
        sig_b64 = signer.sign_b64(b"test data")
        decoded = base64.b64decode(sig_b64)
        assert len(decoded) > 0

    def test_sign_deterministic(self, signer):
        """Same input should produce same signature (RSA PKCS1v1.5 is deterministic)."""
        data = b"deterministic test"
        sig1 = signer.sign(data)
        sig2 = signer.sign(data)
        assert sig1 == sig2

    def test_sign_different_data_different_signature(self, signer):
        """Different inputs should produce different signatures."""
        sig1 = signer.sign(b"data1")
        sig2 = signer.sign(b"data2")
        assert sig1 != sig2


class TestFIELSignerStamping:
    def test_stamp_xml_adds_attributes(self, signer):
        """stamp_xml() should add Sello, Certificado, NoCertificado."""
        xml = b'<declaracion version="1.0"/>'
        sello = signer.sign_b64(b"test")
        stamped = signer.stamp_xml(xml, sello)
        assert b'Sello=' in stamped
        assert b'Certificado=' in stamped
        assert b'NoCertificado=' in stamped

    def test_stamp_xml_invalid_raises(self, signer):
        """stamp_xml() with malformed XML should raise FIELSignerError."""
        with pytest.raises(FIELSignerError, match="inv.lido"):
            signer.stamp_xml(b"not xml><", "fake_sello")

    def test_stamp_xml_preserves_content(self, signer):
        """stamp_xml() should preserve original element content."""
        xml = b'<declaracion><campo>valor</campo></declaracion>'
        sello = signer.sign_b64(b"test")
        stamped = signer.stamp_xml(xml, sello)
        assert b"<campo>valor</campo>" in stamped


class TestFIELSignerValidity:
    def test_valid_cert_check(self, signer):
        """Valid cert should return (True, message)."""
        is_valid, msg = signer.check_validity()
        assert is_valid is True
        assert "vigente" in msg.lower() or "advertencia" in msg.lower()

    def test_expired_cert_check(self, expired_signer):
        """Expired cert should return (False, message)."""
        is_valid, msg = expired_signer.check_validity()
        assert is_valid is False
        assert "expirado" in msg.lower()

    def test_expired_signer_rejects_sign_declaration(self, expired_signer):
        """sign_declaration() should reject expired certificates."""
        xml = b'<declaracion/>'
        with pytest.raises(FIELSignerError, match="expirado"):
            expired_signer.sign_declaration(xml)

    def test_certificate_b64(self, signer):
        """certificate_b64 should return valid base64."""
        cert_b64 = signer.certificate_b64
        decoded = base64.b64decode(cert_b64)
        assert len(decoded) > 0

    def test_no_certificado_length(self, signer):
        """no_certificado should be last 20 digits of serial."""
        nc = signer.no_certificado
        assert len(nc) == 20
        assert nc.isdigit()


class TestFIELSignerFromFile:
    def test_missing_cer_file(self, tmp_path):
        """Missing .cer file should raise FIELSignerError."""
        key_file = tmp_path / "test.key"
        key_file.write_bytes(b"dummy")
        with pytest.raises(FIELSignerError, match="no encontrado"):
            FIELSigner.from_files(
                str(tmp_path / "missing.cer"),
                str(key_file),
                "password",
            )

    def test_missing_key_file(self, tmp_path, self_signed_cert):
        """Missing .key file should raise FIELSignerError."""
        cer_file = tmp_path / "test.cer"
        cer_file.write_bytes(
            self_signed_cert.public_bytes(serialization.Encoding.DER)
        )
        with pytest.raises(FIELSignerError, match="no encontrada"):
            FIELSigner.from_files(
                str(cer_file),
                str(tmp_path / "missing.key"),
                "password",
            )


class TestSignDeclaration:
    def test_sign_declaration_full_workflow(self, signer):
        """sign_declaration() should check validity, sign, and stamp."""
        xml = b'<declaracion version="1.0"><campo>valor</campo></declaracion>'
        result = signer.sign_declaration(xml)
        assert b'Sello=' in result
        assert b'Certificado=' in result
        assert b'NoCertificado=' in result
        assert b'<campo>valor</campo>' in result


# --- validate_rfc_with_digit ---

class TestValidateRFC:
    def test_valid_moral(self):
        assert validate_rfc_with_digit("ABC123456T10") is True

    def test_valid_fisica(self):
        assert validate_rfc_with_digit("GAPA850101ABC") is True

    def test_empty_rfc(self):
        assert validate_rfc_with_digit("") is False

    def test_short_rfc(self):
        assert validate_rfc_with_digit("ABC") is False

    def test_none_rfc(self):
        assert validate_rfc_with_digit(None) is False

    def test_lowercase_converted(self):
        """Lowercase RFC should be validated (auto-uppercased)."""
        assert validate_rfc_with_digit("gapa850101abc") is True
