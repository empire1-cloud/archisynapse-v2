"""Gateway sends its service token to the transaction and ledger services.

Proves:
  * each internal call carries `Authorization: Bearer <token>` for its own hop
    (gateway -> transaction, gateway -> ledger) next to X-Organization-ID;
  * a token file is re-read on every call, so a rotated token takes effect
    without restarting the gateway;
  * with no token configured, no Authorization header is sent, so an
    enforcing service refuses the call (fail closed, no silent trust);
  * every gateway call site that sends X-Organization-ID goes through these
    helpers (no hand-built header left behind).
"""

import os
import pathlib
import re
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from service_auth import (  # noqa: E402
    ledger_service_headers,
    outbound_service_token,
    transaction_service_headers,
)

GATEWAY_DIR = pathlib.Path(__file__).resolve().parents[1]
T_TOKEN = "t" * 40 + "-gateway-to-transaction"
L_TOKEN = "l" * 40 + "-gateway-to-ledger"


class ServiceAuthHeaderTests(unittest.TestCase):
    def test_each_hop_uses_its_own_token(self):
        env = {
            "ARCHISYNAPSE_GATEWAY_TO_TRANSACTION_TOKEN": T_TOKEN,
            "ARCHISYNAPSE_GATEWAY_TO_LEDGER_TOKEN": L_TOKEN,
        }
        self.assertEqual(
            transaction_service_headers("mer_a", env),
            {"X-Organization-ID": "mer_a", "Authorization": f"Bearer {T_TOKEN}"},
        )
        self.assertEqual(
            ledger_service_headers("mer_a", env),
            {"X-Organization-ID": "mer_a", "Authorization": f"Bearer {L_TOKEN}"},
        )

    def test_token_file_is_reread_after_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "gateway-to-transaction.token")
            env = {"ARCHISYNAPSE_GATEWAY_TO_TRANSACTION_TOKEN_FILE": path}
            pathlib.Path(path).write_text(T_TOKEN + "\n")
            self.assertEqual(transaction_service_headers("mer_a", env)["Authorization"], f"Bearer {T_TOKEN}")
            pathlib.Path(path).write_text(L_TOKEN + "\n")
            self.assertEqual(transaction_service_headers("mer_a", env)["Authorization"], f"Bearer {L_TOKEN}")

    def test_direct_value_wins_over_file(self):
        env = {
            "ARCHISYNAPSE_GATEWAY_TO_LEDGER_TOKEN": L_TOKEN,
            "ARCHISYNAPSE_GATEWAY_TO_LEDGER_TOKEN_FILE": "/does/not/exist",
        }
        self.assertEqual(outbound_service_token("ARCHISYNAPSE_GATEWAY_TO_LEDGER", env), L_TOKEN)

    def test_no_token_sends_no_authorization(self):
        self.assertEqual(transaction_service_headers("mer_a", {}), {"X-Organization-ID": "mer_a"})
        self.assertEqual(ledger_service_headers("mer_a", {}), {"X-Organization-ID": "mer_a"})

    def test_no_call_site_builds_the_organization_header_by_hand(self):
        offenders = []
        for path in GATEWAY_DIR.glob("*.py"):
            if path.name in ("service_auth.py",) or path.name.startswith("test_"):
                continue
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if re.search(r"""["']X-Organization-ID["']\s*:""", line):
                    offenders.append(f"{path.name}:{number}")
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
