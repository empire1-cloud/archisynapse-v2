"""Fresh service tokens for local harnesses that start the services directly.

The ledger and transaction services refuse to start without inbound service
tokens (ARCHISYNAPSE_SERVICE_AUTH=enforce is the default). Harnesses that
launch them outside docker compose call this once and merge each part into
the matching process environment. Tokens are random per run and never written
to disk.
"""

from __future__ import annotations

import secrets


def local_service_token_env() -> dict[str, dict[str, str]]:
    gateway_to_transaction = secrets.token_hex(32)
    gateway_to_ledger = secrets.token_hex(32)
    transaction_to_ledger = secrets.token_hex(32)
    return {
        "ledger": {
            "ARCHISYNAPSE_SERVICE_AUTH": "enforce",
            "ARCHISYNAPSE_INBOUND_SERVICE_TOKENS": (
                f"gateway:read:{gateway_to_ledger},transaction:write:{transaction_to_ledger}"
            ),
        },
        "transaction": {
            "ARCHISYNAPSE_SERVICE_AUTH": "enforce",
            "ARCHISYNAPSE_INBOUND_SERVICE_TOKENS": f"gateway:write:{gateway_to_transaction}",
            "ARCHISYNAPSE_TRANSACTION_TO_LEDGER_TOKEN": transaction_to_ledger,
        },
        "gateway": {
            "ARCHISYNAPSE_GATEWAY_TO_TRANSACTION_TOKEN": gateway_to_transaction,
            "ARCHISYNAPSE_GATEWAY_TO_LEDGER_TOKEN": gateway_to_ledger,
        },
    }


if __name__ == "__main__":
    # Shell form for start.sh: prints NAME=value lines prefixed by service.
    for service, values in local_service_token_env().items():
        for name, value in values.items():
            print(f"{service.upper()}_{name}={value}")
