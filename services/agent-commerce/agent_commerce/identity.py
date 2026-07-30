from __future__ import annotations

import hashlib
import hmac
import ipaddress
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from .errors import IdentityConfigurationError


def _is_private_host(host: str) -> bool:
    lowered = host.lower().strip("[]")
    if lowered in {"localhost", "metadata.google.internal"}:
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified


def validate_public_endpoint(endpoint: str, *, production: bool) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise IdentityConfigurationError("agent endpoint must be an absolute HTTP(S) URL")
    if _is_private_host(parsed.hostname):
        raise IdentityConfigurationError("private, loopback, and metadata endpoints are forbidden")
    if production and parsed.scheme != "https":
        raise IdentityConfigurationError("production agent endpoints must use HTTPS")
    if parsed.username or parsed.password:
        raise IdentityConfigurationError("credentials must not be embedded in agent endpoints")
    return endpoint.rstrip("/")


class AgentProfileSigner:
    def __init__(self, signing_key: str):
        if len(signing_key.encode()) < 32:
            raise ValueError("profile signing key must be at least 32 bytes")
        self._key = signing_key.encode()

    def build_profile(
        self,
        *,
        npub: str,
        name: str,
        specialty: str,
        price_sats: int,
        endpoint: str,
        production: bool,
        ttl_seconds: int = 300,
        capabilities: list[str] | None = None,
    ) -> dict:
        if price_sats < 0:
            raise ValueError("price_sats cannot be negative")
        public_endpoint = validate_public_endpoint(endpoint, production=production)
        issued = datetime.now(timezone.utc)
        payload = {
            "schema": "empire1.agent-profile.v1",
            "issuer": "archisynapse-v2",
            "npub": npub,
            "name": name[:64],
            "specialty": specialty[:128],
            "price_sats": price_sats,
            "l402_endpoint": public_endpoint,
            "capabilities": sorted(set(capabilities or [])),
            "issued_at": issued.isoformat().replace("+00:00", "Z"),
            "expires_at": (issued + timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z"),
        }
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        digest = hashlib.sha256(canonical).hexdigest()
        signature = hmac.new(self._key, digest.encode(), hashlib.sha256).hexdigest()
        return {**payload, "profile_sha256": digest, "archisynapse_attestation": signature}

    def verify_profile(self, profile: dict, *, production: bool) -> bool:
        try:
            validate_public_endpoint(profile["l402_endpoint"], production=production)
            expires = datetime.fromisoformat(profile["expires_at"].replace("Z", "+00:00"))
            if expires <= datetime.now(timezone.utc):
                return False
            supplied_digest = profile["profile_sha256"]
            supplied_signature = profile["archisynapse_attestation"]
            payload = {
                k: v
                for k, v in profile.items()
                if k not in {"profile_sha256", "archisynapse_attestation"}
            }
            canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            digest = hashlib.sha256(canonical).hexdigest()
            signature = hmac.new(self._key, digest.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(digest, supplied_digest) and hmac.compare_digest(
                signature, supplied_signature
            )
        except (KeyError, TypeError, ValueError, IdentityConfigurationError):
            return False
