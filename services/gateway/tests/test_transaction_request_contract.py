"""The gateway's payment body matches the transaction service contract.

transaction-service-api.ts requires `customerId` to be a UUID when present and
`paymentMethod.last4` to be exactly 4 characters when present. Before this fix
the gateway always sent the merchant's customer reference as customerId and
sent "" for a missing last4, so a payment that passed fraud scoring then
failed at the transaction service (found in the 2026-10-04 local proof run).
"""
import re
import unittest
import uuid

from canonical_event import PaymentRequest
from orchestrator import build_transaction_request

UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def body(**overrides):
    fields = {
        "merchant_id": "mer_test",
        "customer_id": "customer-proof-001",
        "amount": 12.50,
        "fee_amount": 0.50,
        "payment_method_token": "pm_card_visa",
        "payment_method_last4": "",
        "payment_method_brand": "",
    }
    fields.update(overrides)
    request = PaymentRequest(**fields)
    event = request.to_canonical_event("idem-1")
    return build_transaction_request(event, request)


class TransactionRequestContractTests(unittest.TestCase):
    def test_non_uuid_customer_reference_moves_to_metadata(self):
        sent = body(customer_id="customer-proof-001")
        self.assertNotIn("customerId", sent)
        self.assertEqual(sent["metadata"]["customer_ref"], "customer-proof-001")

    def test_uuid_customer_id_is_sent_in_canonical_form(self):
        value = str(uuid.uuid4()).upper()
        sent = body(customer_id=value)
        self.assertRegex(sent["customerId"], UUID_RE)
        self.assertEqual(sent["customerId"], value.lower())
        self.assertEqual(sent["metadata"]["customer_ref"], value)

    def test_empty_card_display_fields_are_omitted(self):
        method = body()["paymentMethod"]
        self.assertEqual(set(method), {"type", "token"})

    def test_card_display_fields_are_sent_when_valid(self):
        method = body(payment_method_last4="4242", payment_method_brand="visa")["paymentMethod"]
        self.assertEqual((method["last4"], method["brand"]), ("4242", "visa"))

    def test_malformed_last4_is_omitted_not_sent(self):
        self.assertNotIn("last4", body(payment_method_last4="42")["paymentMethod"])

    def test_amounts_and_trace_fields(self):
        sent = body()
        self.assertEqual((sent["amount"], sent["feeAmount"]), ("12.50", "0.50"))
        for key in ("correlation_id", "event_id", "fraud_decision", "fraud_score"):
            self.assertIn(key, sent["metadata"])


if __name__ == "__main__":
    unittest.main()
