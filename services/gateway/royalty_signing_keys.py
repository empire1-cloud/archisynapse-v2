"""Signing-key resolution for the reference Lyrica outbox.

Production rows persist only an opaque signing_key_ref. A Vault/KMS-backed
adapter must resolve that reference at delivery time. The default adapter
fails closed; the in-memory adapter exists only for tests and examples.
"""

from typing import Protocol


class SigningKeyUnavailable(RuntimeError):
    """The referenced signing key cannot currently be resolved."""


class SigningKeyProvider(Protocol):
    async def resolve_private_key(self, signing_key_ref: str) -> str:
        ...


class FailClosedSigningKeyProvider:
    async def resolve_private_key(self, signing_key_ref: str) -> str:
        raise SigningKeyUnavailable(
            f"no production signing-key provider configured for {signing_key_ref}"
        )


class InMemorySigningKeyProvider:
    """TEST-ONLY key provider. Raw keys never enter the outbox table."""

    def __init__(self, keys: dict[str, str]):
        self._keys = dict(keys)

    async def resolve_private_key(self, signing_key_ref: str) -> str:
        try:
            return self._keys[signing_key_ref]
        except KeyError as exc:
            raise SigningKeyUnavailable(
                f"test signing key reference not found: {signing_key_ref}"
            ) from exc
