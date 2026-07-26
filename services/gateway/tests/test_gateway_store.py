from __future__ import annotations

import base64
import os
import unittest
from decimal import Decimal

from gateway_store import (
    AuthenticationError,
    CredentialCipher,
    canonical_request_hash,
    generate_merchant_api_key,
    hash_api_key,
    parse_merchant_api_key,
    verify_api_key,
)


class GatewayStoreUtilityTests(unittest.TestCase):
    def test_api_key_round_trip(self) -> None:
        key_id, api_key, prefix = generate_merchant_api_key("test")
        environment, parsed_key_id = parse_merchant_api_key(api_key)
        self.assertEqual(environment, "test")
        self.assertEqual(parsed_key_id, key_id)
        self.assertTrue(prefix.endswith(key_id))
        hashed = hash_api_key(api_key)
        self.assertTrue(verify_api_key(hashed, api_key))
        self.assertFalse(verify_api_key(hashed, api_key + "x"))

    def test_invalid_api_key_is_rejected(self) -> None:
        with self.assertRaises(AuthenticationError):
            parse_merchant_api_key("not-a-key")

    def test_credentials_are_bound_to_merchant(self) -> None:
        encoded = base64.urlsafe_b64encode(os.urandom(32)).decode("ascii").rstrip("=")
        cipher = CredentialCipher.from_base64(encoded)
        encrypted = cipher.encrypt("mer_one", {"fraud_api_key": "secret"})
        self.assertEqual(
            cipher.decrypt("mer_one", encrypted), {"fraud_api_key": "secret"}
        )
        with self.assertRaises(Exception):
            cipher.decrypt("mer_two", encrypted)

    def test_request_hash_is_stable(self) -> None:
        left = {
            "amount": Decimal("10.00"),
            "currency": "USD",
            "metadata": {"b": 2, "a": 1},
        }
        right = {
            "metadata": {"a": 1, "b": 2},
            "currency": "USD",
            "amount": Decimal("10.00"),
        }
        self.assertEqual(canonical_request_hash(left), canonical_request_hash(right))

    def test_different_requests_have_different_hashes(self) -> None:
        self.assertNotEqual(
            canonical_request_hash({"amount": Decimal("10.00")}),
            canonical_request_hash({"amount": Decimal("11.00")}),
        )


if __name__ == "__main__":
    unittest.main()
