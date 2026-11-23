import base64
import hashlib
import ipaddress
import json
import secrets
import socket
import threading
import time
from datetime import datetime, timedelta, timezone

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from cryptography.x509.oid import NameOID

from remote_mouse.config import CERT_FILE, DATA_DIR, KEY_FILE, TRUSTED_FILE, logger

TRUSTED_LOCK = threading.Lock()
_PIN_LOCK = threading.Lock()
_MIGRATION_LOCK = threading.Lock()

_pairing_pin = f"{secrets.randbelow(1_000_000):06d}"
_failed_pin_attempts = 0
_pin_lockout_until = 0.0

# In-memory migration tokens: token -> {"device_id": str, "name": str, "expires_at": float}
_migration_tokens: dict[str, dict] = {}


def create_migration_token(device_id: str, device_name: str = "Device") -> str:
    """Generate a single-use 120-second token allowing a paired USB client to hand off to Wi-Fi."""
    with _MIGRATION_LOCK:
        now = time.monotonic()
        # Clean expired tokens
        expired = [k for k, v in _migration_tokens.items() if v["expires_at"] <= now]
        for k in expired:
            del _migration_tokens[k]

        token = secrets.token_urlsafe(24)
        _migration_tokens[token] = {
            "device_id": device_id,
            "name": device_name,
            "expires_at": now + 120.0,
        }
        return token


def consume_migration_token(token: str) -> dict | None:
    """Consume and invalidate a migration token if valid and not expired."""
    with _MIGRATION_LOCK:
        now = time.monotonic()
        data = _migration_tokens.pop(token, None)
        if data and data["expires_at"] > now:
            return data
        return None


def get_pairing_pin() -> str:
    with _PIN_LOCK:
        return _pairing_pin


def rotate_pairing_pin() -> str:
    global _pairing_pin
    with _PIN_LOCK:
        _pairing_pin = f"{secrets.randbelow(1_000_000):06d}"
        return _pairing_pin


def verify_pairing_pin(candidate: str) -> tuple[bool, int, str]:
    """Validate a submitted pairing PIN with 5-attempt lockout protection.

    Returns (ok, close_code, reason).
    """
    global _failed_pin_attempts, _pin_lockout_until, _pairing_pin
    with _PIN_LOCK:
        now = time.monotonic()
        if now < _pin_lockout_until:
            wait_s = int(_pin_lockout_until - now) + 1
            return False, 4002, f"Locked out after failed PINs. Wait {wait_s}s."

        if not secrets.compare_digest(candidate, _pairing_pin):
            _failed_pin_attempts += 1
            if _failed_pin_attempts >= 5:
                _failed_pin_attempts = 0
                _pin_lockout_until = time.monotonic() + 30.0
                _pairing_pin = f"{secrets.randbelow(1_000_000):06d}"
                logger.warning("5 failed PIN attempts. Rotated Pairing PIN.")
                return False, 4002, "Too many failed PINs. Check PC for new PIN."
            return False, 4001, "Incorrect Pairing PIN."

        _failed_pin_attempts = 0
        return True, 0, ""


def load_trusted_devices() -> dict:
    if not TRUSTED_FILE.exists():
        return {}
    try:
        return json.loads(TRUSTED_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_trusted_devices(devices: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_file = TRUSTED_FILE.with_suffix(".tmp")
    tmp_file.write_text(json.dumps(devices, indent=2), encoding="utf-8")
    tmp_file.replace(TRUSTED_FILE)


def ensure_tls_cert(local_ip: str) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)

    if CERT_FILE.exists() and KEY_FILE.exists():
        try:
            existing = x509.load_pem_x509_certificate(CERT_FILE.read_bytes())
            san = existing.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            san_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
            
            # expiry = getattr(existing, "not_valid_after_utc", existing.not_valid_after.replace(tzinfo=timezone.utc))
            if hasattr(existing, "not_valid_after_utc"):
                expiry = existing.not_valid_after_utc
            else:
                expiry = existing.not_valid_after.replace(tzinfo=timezone.utc)
                
            if (local_ip == "127.0.0.1" or local_ip in san_ips) and expiry > now + timedelta(days=7):
                return
        except Exception:
            pass

    private_key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"RemoteMouse ({local_ip})")])
    san_entries = [
        x509.DNSName("localhost"),
        x509.DNSName(f"{socket.gethostname().lower()}.local"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    if local_ip != "127.0.0.1":
        san_entries.append(x509.IPAddress(ipaddress.ip_address(local_ip)))

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=5))
        .not_valid_after(now + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(private_key, hashes.SHA256())
    )

    KEY_FILE.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    CERT_FILE.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    logger.info("Generated self-signed ECDSA TLS certificate for %s", local_ip)


def verify_p256_signature(public_key_b64: str, nonce_bytes: bytes, signature_b64: str) -> tuple[bool, str]:
    try:
        pub_der = base64.b64decode(public_key_b64, validate=True)
        raw_sig = base64.b64decode(signature_b64, validate=True)
        if len(raw_sig) != 64:
            return False, ""

        pub_key = serialization.load_der_public_key(pub_der)
        if not isinstance(pub_key, ec.EllipticCurvePublicKey) or not isinstance(pub_key.curve, ec.SECP256R1):
            return False, ""

        r = int.from_bytes(raw_sig[:32], "big")
        s = int.from_bytes(raw_sig[32:], "big")
        der_sig = encode_dss_signature(r, s)

        pub_key.verify(der_sig, nonce_bytes, ec.ECDSA(hashes.SHA256()))
        fingerprint = hashlib.sha256(pub_der).hexdigest()[:16]
        return True, fingerprint
    except (InvalidSignature, ValueError, TypeError):
        return False, ""