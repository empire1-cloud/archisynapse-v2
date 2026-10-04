"""Ed25519 proof envelopes for Archisynapse payment receipts."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Mapping

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)


class ReceiptProofConfigurationError(RuntimeError):
    pass


class ReceiptProofError(RuntimeError):
    pass


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except Exception as exc:
        raise ReceiptProofError("invalid base64 proof material") from exc


def canonical_receipt_bytes(receipt: Mapping[str, Any]) -> bytes:
    payload = {key: value for key, value in dict(receipt).items() if key != "_proof"}
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    ).encode("utf-8")


@dataclass(frozen=True)
class ReceiptProof:
    algorithm: str
    key_id: str
    payload_sha256: str
    signature_b64: str
    public_key_b64: str

    def as_dict(self) -> dict[str, str]:
        return {
            "algorithm": self.algorithm,
            "key_id": self.key_id,
            "payload_sha256": self.payload_sha256,
            "signature_b64": self.signature_b64,
            "public_key_b64": self.public_key_b64,
        }


class ReceiptSigner:
    algorithm = "Ed25519"

    def __init__(self, private_key: Ed25519PrivateKey, *, key_id: str) -> None:
        if not key_id.strip():
            raise ReceiptProofConfigurationError("receipt proof key id is required")
        self._private_key = private_key
        self.key_id = key_id.strip()
        self._public_key = private_key.public_key()

    @classmethod
    def from_base64_seed(cls, value: str, *, key_id: str) -> "ReceiptSigner":
        seed = _b64decode(value)
        if len(seed) != 32:
            raise ReceiptProofConfigurationError(
                "receipt signing private key must decode to a 32-byte Ed25519 seed"
            )
        return cls(Ed25519PrivateKey.from_private_bytes(seed), key_id=key_id)

    @classmethod
    def generate(cls, *, key_id: str = "receipt-test-v1") -> "ReceiptSigner":
        return cls(Ed25519PrivateKey.generate(), key_id=key_id)

    def private_seed_b64(self) -> str:
        return _b64encode(
            self._private_key.private_bytes(
                Encoding.Raw, PrivateFormat.Raw, NoEncryption()
            )
        )

    def public_key_b64(self) -> str:
        return _b64encode(
            self._public_key.public_bytes(Encoding.Raw, PublicFormat.Raw)
        )

    def sign(self, receipt: Mapping[str, Any]) -> ReceiptProof:
        payload = canonical_receipt_bytes(receipt)
        digest = hashlib.sha256(payload).hexdigest()
        signature = self._private_key.sign(payload)
        return ReceiptProof(
            algorithm=self.algorithm,
            key_id=self.key_id,
            payload_sha256=digest,
            signature_b64=_b64encode(signature),
            public_key_b64=self.public_key_b64(),
        )

    def attach(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        payload = {
            key: value for key, value in dict(receipt).items() if key != "_proof"
        }
        payload["_proof"] = self.sign(payload).as_dict()
        return payload


def verify_receipt(receipt: Mapping[str, Any]) -> tuple[bool, str]:
    proof_raw = receipt.get("_proof")
    if not isinstance(proof_raw, Mapping):
        return False, "receipt has no proof envelope"
    if proof_raw.get("algorithm") != ReceiptSigner.algorithm:
        return False, "unsupported proof algorithm"
    try:
        payload = canonical_receipt_bytes(receipt)
        digest = hashlib.sha256(payload).hexdigest()
        if digest != proof_raw.get("payload_sha256"):
            return False, "receipt payload hash does not match proof"
        public_key = Ed25519PublicKey.from_public_bytes(
            _b64decode(str(proof_raw.get("public_key_b64", "")))
        )
        public_key.verify(
            _b64decode(str(proof_raw.get("signature_b64", ""))), payload
        )
    except (ValueError, InvalidSignature, ReceiptProofError):
        return False, "receipt signature is invalid"
    return True, "receipt signature is valid"


def build_receipt_signer_from_env(
    environ: Mapping[str, str] | None = None,
) -> ReceiptSigner | None:
    env = environ or os.environ
    seed = env.get("ARCHISYNAPSE_RECEIPT_SIGNING_PRIVATE_KEY", "").strip()
    if not seed:
        return None
    key_id = env.get("ARCHISYNAPSE_RECEIPT_SIGNING_KEY_ID", "receipt-v1")
    return ReceiptSigner.from_base64_seed(seed, key_id=key_id)


# ---------------------------------------------------------------------------
# Trusted key ring and rotation
#
# `verify_receipt` above checks that a receipt is internally consistent: the
# signature matches the public key embedded in the receipt. It does not check
# WHO signed it -- anyone can generate a key, sign a receipt, and embed their
# own public key. The key ring below adds authenticity: a receipt is accepted
# only if its key_id is one Archisynapse issued, and the signature verifies
# against that trusted public key (the embedded key is ignored for trust and
# must match if present).
#
# Rotation: the active signer signs new receipts; retired public keys stay in
# the ring so every receipt signed before the rotation still verifies.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TrustedReceiptKey:
    key_id: str
    public_key_b64: str
    status: str  # "active" or "retired"


class ReceiptKeyRing:
    """Active signer plus every public key whose receipts are still trusted."""

    def __init__(
        self,
        active: ReceiptSigner,
        retired_public_keys: Mapping[str, str] | None = None,
    ) -> None:
        self.active = active
        self._keys: dict[str, TrustedReceiptKey] = {}
        for key_id, public_key_b64 in (retired_public_keys or {}).items():
            key_id = key_id.strip()
            if not key_id:
                raise ReceiptProofConfigurationError("retired key id is required")
            if key_id == active.key_id:
                raise ReceiptProofConfigurationError(
                    f"key id {key_id!r} is both active and retired; rotate to a new key id"
                )
            try:
                raw = _b64decode(public_key_b64.strip())
            except ReceiptProofError as exc:
                raise ReceiptProofConfigurationError(
                    f"retired key {key_id!r} is not valid base64"
                ) from exc
            if len(raw) != 32:
                raise ReceiptProofConfigurationError(
                    f"retired key {key_id!r} must decode to a 32-byte Ed25519 public key"
                )
            self._keys[key_id] = TrustedReceiptKey(key_id, public_key_b64.strip(), "retired")
        self._keys[active.key_id] = TrustedReceiptKey(
            active.key_id, active.public_key_b64(), "active"
        )

    def attach(self, receipt: Mapping[str, Any]) -> dict[str, Any]:
        return self.active.attach(receipt)

    def keys(self) -> list[TrustedReceiptKey]:
        return sorted(self._keys.values(), key=lambda k: (k.status != "active", k.key_id))

    def get(self, key_id: str) -> TrustedReceiptKey | None:
        return self._keys.get(key_id)

    def rotate(self, new_active: ReceiptSigner) -> "ReceiptKeyRing":
        """Return a new ring signing with `new_active`; the old active key is retired."""
        retired = {k.key_id: k.public_key_b64 for k in self._keys.values()}
        return ReceiptKeyRing(new_active, retired)


def verify_receipt_trusted(
    receipt: Mapping[str, Any],
    trusted_keys: Mapping[str, str],
) -> tuple[bool, str]:
    """Verify a receipt against trusted public keys (key_id -> base64 public key).

    Works offline: `trusted_keys` can come from GET /v1/proof/keys saved once.
    """
    proof_raw = receipt.get("_proof")
    if not isinstance(proof_raw, Mapping):
        return False, "receipt has no proof envelope"
    if proof_raw.get("algorithm") != ReceiptSigner.algorithm:
        return False, "unsupported proof algorithm"
    key_id = str(proof_raw.get("key_id", ""))
    trusted_public_key_b64 = trusted_keys.get(key_id)
    if trusted_public_key_b64 is None:
        return False, "receipt was signed by an unknown key"
    embedded = proof_raw.get("public_key_b64")
    if embedded is not None and embedded != trusted_public_key_b64:
        return False, "receipt public key does not match the trusted key"
    try:
        payload = canonical_receipt_bytes(receipt)
        digest = hashlib.sha256(payload).hexdigest()
        if digest != proof_raw.get("payload_sha256"):
            return False, "receipt payload hash does not match proof"
        Ed25519PublicKey.from_public_bytes(_b64decode(trusted_public_key_b64)).verify(
            _b64decode(str(proof_raw.get("signature_b64", ""))), payload
        )
    except (ValueError, InvalidSignature, ReceiptProofError):
        return False, "receipt signature is invalid"
    return True, "receipt signature is valid and the key is trusted"


def keyring_trusted_keys(keyring: ReceiptKeyRing) -> dict[str, str]:
    return {k.key_id: k.public_key_b64 for k in keyring.keys()}


def build_receipt_keyring_from_env(
    environ: Mapping[str, str] | None = None,
) -> ReceiptKeyRing | None:
    """Active key from the existing variables; retired keys from
    ARCHISYNAPSE_RECEIPT_RETIRED_PUBLIC_KEYS, a JSON object {key_id: public_key_b64}.
    """
    env = environ or os.environ
    active = build_receipt_signer_from_env(env)
    if active is None:
        return None
    raw = env.get("ARCHISYNAPSE_RECEIPT_RETIRED_PUBLIC_KEYS", "").strip()
    retired: dict[str, str] = {}
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReceiptProofConfigurationError(
                "ARCHISYNAPSE_RECEIPT_RETIRED_PUBLIC_KEYS must be a JSON object"
            ) from exc
        if not isinstance(parsed, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()
        ):
            raise ReceiptProofConfigurationError(
                "ARCHISYNAPSE_RECEIPT_RETIRED_PUBLIC_KEYS must map key ids to base64 public keys"
            )
        retired = parsed
    return ReceiptKeyRing(active, retired)
