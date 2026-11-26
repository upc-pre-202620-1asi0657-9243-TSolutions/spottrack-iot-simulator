"""Pairing QR v2 of the SpotTrack backend (spottrack-platform docs/sensor-qr-pairing.md).

The QR text is the `payloadJson` returned by
POST /api/v1/monitoring/motion-sensors/{id}/pairing:

    {"v":2,"deviceId":"...","secret":"...","host":"...","port":8883,
     "caSha256":"<64 hex>","exp":1790000000,"profile":"default"}

The device validates it before connecting: known version, every field present,
not expired, and the CA it trusts must match `caSha256`. The secret is the MQTT
password: it is never logged nor shown in a repr.
"""

import hashlib
import hmac
import json
import re
import ssl
import time
from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Optional

from model.payload_profiles import SUPPORTED_PROFILES

SUPPORTED_VERSION = 2
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class PairingError(ValueError):
    """The QR cannot be used: the device must not connect."""


@dataclass(frozen=True)
class PairingPayload:
    device_id: str
    secret: str = field(repr=False)
    host: str
    port: int
    ca_sha256: str
    exp: int
    profile: str


def _require(data: dict, name: str, kind):
    value = data.get(name)
    # bool is an int in Python; a QR with "port": true is malformed
    if value is None or isinstance(value, bool) or not isinstance(value, kind):
        raise PairingError(f"invalid QR: field '{name}' is missing or not a {kind.__name__}")
    if isinstance(value, str) and not value.strip():
        raise PairingError(f"invalid QR: field '{name}' is blank")
    return value


def parse_pairing(text: str, now: Optional[float] = None) -> PairingPayload:
    """Validates the QR text (format v2). Raises PairingError with the reason."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError):
        raise PairingError("invalid QR: not JSON") from None
    if not isinstance(data, dict):
        raise PairingError("invalid QR: not a JSON object")

    version = data.get("v")
    if version != SUPPORTED_VERSION or isinstance(version, bool):
        raise PairingError(f"invalid QR: unsupported version {version!r} (expected {SUPPORTED_VERSION})")

    device_id = _require(data, "deviceId", str)
    secret = _require(data, "secret", str)
    host = _require(data, "host", str)
    port = _require(data, "port", int)
    ca_sha256 = _require(data, "caSha256", str)
    exp = _require(data, "exp", int)
    profile = _require(data, "profile", str)

    if not 1 <= port <= 65535:
        raise PairingError(f"invalid QR: port {port} out of range")
    if not _HEX64.match(ca_sha256):
        raise PairingError("invalid QR: caSha256 must be 64 lowercase hex characters")
    if profile not in SUPPORTED_PROFILES:
        raise PairingError(f"invalid QR: unsupported payload profile '{profile}' "
                           f"(supported: {', '.join(sorted(SUPPORTED_PROFILES))})")
    current = time.time() if now is None else now
    if exp <= current:
        raise PairingError("QR expired: ask for a new pairing")

    return PairingPayload(device_id=device_id, secret=secret, host=host, port=port,
                          ca_sha256=ca_sha256, exp=exp, profile=profile)


def ca_sha256_of(ca_cert_path: str) -> str:
    """SHA-256 (lowercase hex) of the first certificate of a PEM file, in DER.

    Same value as `openssl x509 -in ca.crt -outform DER | openssl dgst -sha256`.
    """
    with open(ca_cert_path, encoding="ascii") as f:
        pem = f.read()
    end = "-----END CERTIFICATE-----"
    if "-----BEGIN CERTIFICATE-----" not in pem or end not in pem:
        raise PairingError(f"{ca_cert_path} is not a PEM certificate")
    first = pem[:pem.index(end) + len(end)]
    try:
        der = ssl.PEM_cert_to_DER_cert(first[first.index("-----BEGIN CERTIFICATE-----"):])
    except ValueError as e:
        raise PairingError(f"{ca_cert_path} is not a PEM certificate: {e}") from None
    return hashlib.sha256(der).hexdigest()


def verify_ca(ca_cert_path: Optional[str], expected_sha256: str):
    """The CA the device trusts must be the one pinned in the QR; otherwise no credentials are sent."""
    if not ca_cert_path:
        raise PairingError("pairing needs the broker CA (--ca-cert / MQTT_CA_CERT) to check caSha256")
    actual = ca_sha256_of(ca_cert_path)
    if not hmac.compare_digest(actual, expected_sha256):
        raise PairingError(f"CA not recognised: {ca_cert_path} has SHA-256 {actual}, the QR expects {expected_sha256}")


@dataclass(frozen=True)
class PairedDevices:
    """Everything the publisher needs from one or more QR codes."""
    host: str
    port: int
    credentials: Dict[str, str] = field(repr=False)
    profiles: Dict[str, str]

    @property
    def device_ids(self) -> List[str]:
        return list(self.credentials)


def load_pairings(texts: Iterable[str], ca_cert_path: Optional[str], now: Optional[float] = None) -> PairedDevices:
    """Parses and validates every QR and the CA. All of them must point at the same brokers."""
    payloads = [parse_pairing(text, now=now) for text in texts]
    if not payloads:
        raise PairingError("no pairing given")
    first = payloads[0]
    for p in payloads[1:]:
        if (p.host, p.port, p.ca_sha256) != (first.host, first.port, first.ca_sha256):
            raise PairingError(f"pairing of {p.device_id} points at other brokers than {first.device_id}")
    ids = [p.device_id for p in payloads]
    if len(set(ids)) != len(ids):
        raise PairingError("the same deviceId appears in more than one pairing")
    verify_ca(ca_cert_path, first.ca_sha256)
    return PairedDevices(
        host=first.host,
        port=first.port,
        credentials={p.device_id: p.secret for p in payloads},
        profiles={p.device_id: p.profile for p in payloads},
    )
