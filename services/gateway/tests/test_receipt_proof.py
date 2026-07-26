import unittest

from receipt_proof import ReceiptSigner, build_receipt_signer_from_env, verify_receipt


class ReceiptProofTests(unittest.TestCase):
    def test_sign_and_verify(self):
        signer = ReceiptSigner.generate(key_id="proof-test-v1")
        receipt = {
            "event_id": "evt_1",
            "merchant_id": "mer_1",
            "amount": 12.5,
            "currency": "USD",
            "status": "completed",
        }
        signed = signer.attach(receipt)
        valid, message = verify_receipt(signed)
        self.assertTrue(valid)
        self.assertEqual(message, "receipt signature is valid")
        self.assertEqual(signed["_proof"]["key_id"], "proof-test-v1")

    def test_tampering_fails(self):
        signer = ReceiptSigner.generate()
        signed = signer.attach(
            {"event_id": "evt_2", "amount": 10.0, "currency": "USD"}
        )
        signed["amount"] = 11.0
        valid, _ = verify_receipt(signed)
        self.assertFalse(valid)

    def test_environment_round_trip(self):
        original = ReceiptSigner.generate(key_id="receipt-env-v1")
        loaded = build_receipt_signer_from_env(
            {
                "ARCHISYNAPSE_RECEIPT_SIGNING_PRIVATE_KEY": original.private_seed_b64(),
                "ARCHISYNAPSE_RECEIPT_SIGNING_KEY_ID": "receipt-env-v1",
            }
        )
        self.assertIsNotNone(loaded)
        assert loaded is not None
        signed = loaded.attach({"event_id": "evt_3", "status": "failed"})
        self.assertTrue(verify_receipt(signed)[0])
        self.assertEqual(loaded.public_key_b64(), original.public_key_b64())


if __name__ == "__main__":
    unittest.main()
