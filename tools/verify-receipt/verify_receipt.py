"""Offline receipt verifier.

Checks a signed Archisynapse receipt against a saved copy of the trusted keys,
with no network access and no database.

    python tools/verify-receipt/verify_receipt.py receipt.json keys.json

receipt.json: a receipt as returned by GET /v1/receipts/{event_id}
keys.json:    the response of GET /v1/proof/keys, saved once

Exit code 0 = valid and signed by a trusted key; 1 = not valid; 2 = bad input.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "gateway"))

from receipt_proof import verify_receipt_trusted  # noqa: E402


def load_trusted_keys(document: dict) -> dict[str, str]:
    keys = document.get("keys")
    if not isinstance(keys, list):
        raise ValueError("keys file must be the JSON returned by GET /v1/proof/keys")
    return {str(k["key_id"]): str(k["public_key_b64"]) for k in keys}


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        receipt = json.loads(Path(argv[1]).read_text())
        trusted = load_trusted_keys(json.loads(Path(argv[2]).read_text()))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"input error: {exc}", file=sys.stderr)
        return 2
    valid, message = verify_receipt_trusted(receipt, trusted)
    key_id = (receipt.get("_proof") or {}).get("key_id")
    print(json.dumps({"valid": valid, "message": message, "key_id": key_id}))
    return 0 if valid else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
