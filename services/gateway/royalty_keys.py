"""
Key management for the Lyrica royalty receipt loop — see
spec/SPEC-royalty-loop-v1.md §2 (transport/signing) and §6 (signed receipts).

Two independent keysets:
  - Tenant keys: ed25519 public keys registered per tenant (e.g. "lyrica"),
    used to verify INCOMING event signatures. Registered via
    POST /admin/tenants/{tenant_id}/keys — no private key material ever
    touches the gateway for these.
  - Gateway receipt key: one ed25519 keypair the gateway owns, used to
    SIGN outgoing receipts so Lyrica (and creators) can verify them
    independently. Generated on first use and persisted under
    services/gateway/.runtime/ (gitignored — same category as
    merchant_credentials.json; belongs in a real secrets manager
    long-term, not a JSON file on one machine).
"""

import base64
import json
import os
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

_RUNTIME_DIR = os.path.join(os.path.dirname(__file__), ".runtime")
TENANT_KEYS_FILE = os.path.join(_RUNTIME_DIR, "royalty_tenant_keys.json")
RECEIPT_KEY_FILE = os.path.join(_RUNTIME_DIR, "royalty_receipt_signing_key.json")
GATEWAY_KEY_ID = "arch-rcpt-k1"


def _ensure_runtime_dir() -> None:
    os.makedirs(_RUNTIME_DIR, exist_ok=True)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(data: str) -> bytes:
    return base64.b64decode(data)


class TenantKeyRegistry:
    """tenant_id -> {key_id -> ed25519 public key (base64, raw 32 bytes)}."""

    def __init__(self, path: str = TENANT_KEYS_FILE):
        self._path = path
        self._data: dict[str, dict[str, str]] = self._load()

    def _load(self) -> dict[str, dict[str, str]]:
        if os.path.exists(self._path):
            with open(self._path) as f:
                return json.load(f)
        return {}

    def _save(self) -> None:
        _ensure_runtime_dir()
        with open(self._path, "w") as f:
            json.dump(self._data, f, indent=2)

    def register(self, tenant_id: str, key_id: str, public_key_b64: str) -> None:
        self._data.setdefault(tenant_id, {})[key_id] = public_key_b64
        self._save()

    def get(self, tenant_id: str, key_id: str) -> Optional[str]:
        return self._data.get(tenant_id, {}).get(key_id)

    def tenant_owns_key(self, tenant_id: str, key_id: str) -> bool:
        return key_id in self._data.get(tenant_id, {})

    def key_registered_to_any_tenant(self, key_id: str) -> bool:
        return any(key_id in keys for keys in self._data.values())


def verify_event_signature(
    raw_body: bytes, signature_header: str, public_key_b64: str
) -> bool:
    """
    signature_header looks like 'ed25519=<base64(sig)>' per spec §2.
    Returns False (never raises) on any malformed input or bad signature —
    callers must treat False as "reject, no financial objects created".
    """
    try:
        alg, _, sig_b64 = signature_header.partition("=")
        if alg != "ed25519" or not sig_b64:
            return False
        signature = _unb64(sig_b64)
        public_key = Ed25519PublicKey.from_public_bytes(_unb64(public_key_b64))
        public_key.verify(signature, raw_body)
        return True
    except (InvalidSignature, ValueError, Exception):
        return False


def sign_with_private_key(private_key_b64: str, payload: bytes) -> str:
    """Used by test fixtures / Lyrica-side simulators to produce X-Empire1-Signature."""
    private_key = Ed25519PrivateKey.from_private_bytes(_unb64(private_key_b64))
    signature = private_key.sign(payload)
    return f"ed25519={_b64(signature)}"


def generate_tenant_keypair() -> tuple[str, str]:
    """Returns (private_key_b64, public_key_b64) — convenience for fixtures/tests."""
    private_key = Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    public_bytes = public_key.public_bytes(
        encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
    )
    return _b64(private_bytes), _b64(public_bytes)


class GatewayReceiptSigner:
    """The gateway's own keypair for signing outgoing receipts (spec §6)."""

    def __init__(self, path: str = RECEIPT_KEY_FILE, key_id: str = GATEWAY_KEY_ID):
        self._path = path
        self.key_id = key_id
        self._private_key, self.public_key_b64 = self._load_or_generate()

    def _load_or_generate(self) -> tuple[Ed25519PrivateKey, str]:
        if os.path.exists(self._path):
            with open(self._path) as f:
                data = json.load(f)
            private_key = Ed25519PrivateKey.from_private_bytes(_unb64(data["private_key"]))
            return private_key, data["public_key"]

        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()
        private_b64 = _b64(
            private_key.private_bytes(
                encoding=serialization.Encoding.Raw,
                format=serialization.PrivateFormat.Raw,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )
        public_b64 = _b64(
            public_key.public_bytes(
                encoding=serialization.Encoding.Raw, format=serialization.PublicFormat.Raw
            )
        )
        _ensure_runtime_dir()
        with open(self._path, "w") as f:
            json.dump({"private_key": private_b64, "public_key": public_b64}, f, indent=2)
        return private_key, public_b64

    def sign(self, payload: bytes) -> dict:
        signature = self._private_key.sign(payload)
        return {"alg": "ed25519", "key_id": self.key_id, "value": _b64(signature)}

    def verify(self, payload: bytes, signature_b64: str) -> bool:
        try:
            self._private_key.public_key().verify(_unb64(signature_b64), payload)
            return True
        except InvalidSignature:
            return False


tenant_key_registry = TenantKeyRegistry()
gateway_receipt_signer = GatewayReceiptSigner()
