"""Key rotation and trusted verification for signed receipts.

Proves:
  * a receipt signed before a rotation still verifies after it (retired key);
  * receipts after a rotation are signed with the new key;
  * a receipt signed with a key Archisynapse never issued is rejected, even
    though the legacy self-consistency check accepts it;
  * key-id spoofing and tampering are rejected;
  * configuration mistakes fail loudly at startup;
  * the offline verifier gives the same answers from saved key material.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from receipt_proof import (
    ReceiptKeyRing,
    ReceiptProofConfigurationError,
    ReceiptSigner,
    build_receipt_keyring_from_env,
    keyring_trusted_keys,
    verify_receipt,
    verify_receipt_trusted,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
OFFLINE_VERIFIER = REPO_ROOT / "tools" / "verify-receipt" / "verify_receipt.py"


def receipt(n: int) -> dict:
    return {"event_id": f"evt_{n}", "merchant_id": "mer_1", "amount": "12.50", "currency": "USD"}


class ReceiptKeyRotationTests(unittest.TestCase):
    def setUp(self):
        self.v1 = ReceiptSigner.generate(key_id="receipt-v1")
        self.ring_v1 = ReceiptKeyRing(self.v1)
        self.old_receipt = self.ring_v1.attach(receipt(1))
        self.v2 = ReceiptSigner.generate(key_id="receipt-v2")
        self.ring_v2 = self.ring_v1.rotate(self.v2)

    def test_old_receipt_still_verifies_after_rotation(self):
        valid, message = verify_receipt_trusted(self.old_receipt, keyring_trusted_keys(self.ring_v2))
        self.assertTrue(valid, message)
        self.assertEqual(self.ring_v2.get("receipt-v1").status, "retired")

    def test_new_receipts_sign_with_the_new_key(self):
        new_receipt = self.ring_v2.attach(receipt(2))
        self.assertEqual(new_receipt["_proof"]["key_id"], "receipt-v2")
        self.assertEqual(self.ring_v2.get("receipt-v2").status, "active")
        self.assertTrue(verify_receipt_trusted(new_receipt, keyring_trusted_keys(self.ring_v2))[0])

    def test_rotation_keeps_every_earlier_key(self):
        v3 = ReceiptSigner.generate(key_id="receipt-v3")
        ring_v3 = self.ring_v2.rotate(v3)
        self.assertEqual(
            sorted(k.key_id for k in ring_v3.keys()), ["receipt-v1", "receipt-v2", "receipt-v3"]
        )
        self.assertTrue(verify_receipt_trusted(self.old_receipt, keyring_trusted_keys(ring_v3))[0])

    def test_receipt_from_an_unissued_key_is_rejected(self):
        forged = ReceiptSigner.generate(key_id="attacker").attach(receipt(3))
        self.assertTrue(verify_receipt(forged)[0], "legacy check only proves self-consistency")
        valid, message = verify_receipt_trusted(forged, keyring_trusted_keys(self.ring_v2))
        self.assertFalse(valid)
        self.assertEqual(message, "receipt was signed by an unknown key")

    def test_spoofed_key_id_with_attacker_public_key_is_rejected(self):
        forged = ReceiptSigner.generate(key_id="receipt-v2").attach(receipt(4))
        valid, message = verify_receipt_trusted(forged, keyring_trusted_keys(self.ring_v2))
        self.assertFalse(valid)
        self.assertEqual(message, "receipt public key does not match the trusted key")

    def test_spoofed_key_id_with_copied_public_key_is_rejected(self):
        forged = ReceiptSigner.generate(key_id="receipt-v2").attach(receipt(5))
        forged["_proof"]["public_key_b64"] = self.v2.public_key_b64()
        valid, message = verify_receipt_trusted(forged, keyring_trusted_keys(self.ring_v2))
        self.assertFalse(valid)
        self.assertEqual(message, "receipt signature is invalid")

    def test_tampering_after_rotation_is_rejected(self):
        tampered = dict(self.old_receipt)
        tampered["amount"] = "99.00"
        self.assertFalse(verify_receipt_trusted(tampered, keyring_trusted_keys(self.ring_v2))[0])

    def test_retiring_a_key_removes_nothing_from_the_record(self):
        valid_before = verify_receipt_trusted(self.old_receipt, keyring_trusted_keys(self.ring_v1))[0]
        valid_after = verify_receipt_trusted(self.old_receipt, keyring_trusted_keys(self.ring_v2))[0]
        self.assertEqual((valid_before, valid_after), (True, True))


class ReceiptKeyRingConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.v1 = ReceiptSigner.generate(key_id="receipt-v1")
        self.v2 = ReceiptSigner.generate(key_id="receipt-v2")

    def env(self, retired: str | None) -> dict:
        env = {
            "ARCHISYNAPSE_RECEIPT_SIGNING_PRIVATE_KEY": self.v2.private_seed_b64(),
            "ARCHISYNAPSE_RECEIPT_SIGNING_KEY_ID": "receipt-v2",
        }
        if retired is not None:
            env["ARCHISYNAPSE_RECEIPT_RETIRED_PUBLIC_KEYS"] = retired
        return env

    def test_environment_rotation_verifies_old_receipts(self):
        old = ReceiptKeyRing(self.v1).attach(receipt(6))
        ring = build_receipt_keyring_from_env(
            self.env(json.dumps({"receipt-v1": self.v1.public_key_b64()}))
        )
        self.assertIsNotNone(ring)
        assert ring is not None
        self.assertEqual(ring.active.key_id, "receipt-v2")
        self.assertTrue(verify_receipt_trusted(old, keyring_trusted_keys(ring))[0])

    def test_no_signing_key_means_no_ring(self):
        self.assertIsNone(build_receipt_keyring_from_env({}))

    def test_without_retired_keys_only_the_active_key_is_trusted(self):
        ring = build_receipt_keyring_from_env(self.env(None))
        assert ring is not None
        self.assertEqual([k.key_id for k in ring.keys()], ["receipt-v2"])

    def test_bad_configuration_fails_loudly(self):
        bad_values = [
            "not json",
            json.dumps(["receipt-v1"]),
            json.dumps({"receipt-v1": "too-short"}),
            json.dumps({"receipt-v2": self.v1.public_key_b64()}),
        ]
        for value in bad_values:
            with self.subTest(value=value):
                with self.assertRaises(ReceiptProofConfigurationError):
                    build_receipt_keyring_from_env(self.env(value))


class OfflineVerifierTests(unittest.TestCase):
    def run_verifier(self, signed: dict, ring: ReceiptKeyRing) -> tuple[int, dict]:
        keys_doc = {
            "algorithm": "Ed25519",
            "active_key_id": ring.active.key_id,
            "keys": [
                {"key_id": k.key_id, "public_key_b64": k.public_key_b64, "status": k.status}
                for k in ring.keys()
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            receipt_path = Path(tmp) / "receipt.json"
            keys_path = Path(tmp) / "keys.json"
            receipt_path.write_text(json.dumps(signed))
            keys_path.write_text(json.dumps(keys_doc))
            result = subprocess.run(
                [sys.executable, str(OFFLINE_VERIFIER), str(receipt_path), str(keys_path)],
                capture_output=True,
                text=True,
                check=False,
            )
        return result.returncode, json.loads(result.stdout)

    def test_offline_verifier_accepts_old_receipt_after_rotation(self):
        v1 = ReceiptSigner.generate(key_id="receipt-v1")
        ring_v1 = ReceiptKeyRing(v1)
        old = ring_v1.attach(receipt(7))
        ring_v2 = ring_v1.rotate(ReceiptSigner.generate(key_id="receipt-v2"))
        code, output = self.run_verifier(old, ring_v2)
        self.assertEqual(code, 0)
        self.assertTrue(output["valid"])

    def test_offline_verifier_rejects_forgery(self):
        ring = ReceiptKeyRing(ReceiptSigner.generate(key_id="receipt-v1"))
        forged = ReceiptSigner.generate(key_id="attacker").attach(receipt(8))
        code, output = self.run_verifier(forged, ring)
        self.assertEqual(code, 1)
        self.assertFalse(output["valid"])


class GatewayVerifyEndpointTests(unittest.IsolatedAsyncioTestCase):
    """The gateway's /verify, /evidence and /v1/proof/keys use the key ring."""

    async def asyncSetUp(self):
        import production_main

        self.gateway = production_main
        self.saved = production_main._receipt_keyring
        v1 = ReceiptSigner.generate(key_id="receipt-v1")
        self.old = ReceiptKeyRing(v1).attach(receipt(9))
        production_main._receipt_keyring = ReceiptKeyRing(v1).rotate(
            ReceiptSigner.generate(key_id="receipt-v2")
        )

    async def asyncTearDown(self):
        self.gateway._receipt_keyring = self.saved

    async def test_retired_key_receipt_reports_valid_and_retired(self):
        self.assertEqual(
            self.gateway._verify_stored_receipt(self.old),
            (True, "receipt signature is valid and the key is trusted", "retired"),
        )

    async def test_unissued_key_receipt_reports_invalid_and_unknown(self):
        forged = ReceiptSigner.generate(key_id="attacker").attach(receipt(10))
        valid, _, status = self.gateway._verify_stored_receipt(forged)
        self.assertEqual((valid, status), (False, "unknown"))

    async def test_keys_endpoint_lists_active_and_retired(self):
        document = await self.gateway.receipt_proof_keys()
        self.assertEqual(document["active_key_id"], "receipt-v2")
        self.assertEqual(
            {(k["key_id"], k["status"]) for k in document["keys"]},
            {("receipt-v1", "retired"), ("receipt-v2", "active")},
        )

    async def test_without_signing_configured_the_legacy_check_is_labelled(self):
        self.gateway._receipt_keyring = None
        self.assertEqual(self.gateway._verify_stored_receipt(self.old)[2], "unchecked")


if __name__ == "__main__":
    unittest.main()
