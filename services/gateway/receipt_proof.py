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
