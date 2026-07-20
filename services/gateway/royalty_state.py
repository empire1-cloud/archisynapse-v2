"""
Persistence for the royalty receipt loop — follows the same pattern as
runtime_state.py (JSON files under .runtime/, atomic write via temp+replace).
"""

import json
from pathlib import Path
from typing import Any, Optional

STATE_DIR = Path(__file__).resolve().parent / ".runtime"
STATE_DIR.mkdir(exist_ok=True)

ROYALTY_RECEIPTS_DIR = STATE_DIR / "royalty_receipts"
ROYALTY_RECEIPTS_DIR.mkdir(exist_ok=True)

ROYALTY_IDEMPOTENCY_FILE = STATE_DIR / "royalty_idempotency.json"
ROYALTY_REJECTIONS_FILE = STATE_DIR / "royalty_rejections.json"
ROYALTY_TENANT_API_KEYS_FILE = STATE_DIR / "royalty_tenant_api_keys.json"


def _read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True))
    tmp.replace(path)


def save_royalty_receipt(receipt: dict) -> None:
    receipt_id = receipt["receipt_id"]
    _write_json(ROYALTY_RECEIPTS_DIR / f"{receipt_id}.json", receipt)


def load_royalty_receipt(receipt_id: str) -> Optional[dict]:
    return _read_json(ROYALTY_RECEIPTS_DIR / f"{receipt_id}.json", None)


def _idempotency_key(tenant_id: str, idempotency_key: str) -> str:
    return f"{tenant_id}:{idempotency_key}"


def get_idempotency_record(tenant_id: str, idempotency_key: str) -> Optional[dict]:
    store = _read_json(ROYALTY_IDEMPOTENCY_FILE, {})
    return store.get(_idempotency_key(tenant_id, idempotency_key))


def save_idempotency_record(tenant_id: str, idempotency_key: str, request_hash: str, receipt_id: str) -> None:
    store = _read_json(ROYALTY_IDEMPOTENCY_FILE, {})
    store[_idempotency_key(tenant_id, idempotency_key)] = {
        "request_hash": request_hash,
        "receipt_id": receipt_id,
    }
    _write_json(ROYALTY_IDEMPOTENCY_FILE, store)


def record_rejection(correlation_id: Optional[str], key_id: Optional[str], reason: str) -> None:
    rejections = _read_json(ROYALTY_REJECTIONS_FILE, [])
    rejections.append(
        {"correlation_id": correlation_id, "key_id": key_id, "reason": reason}
    )
    _write_json(ROYALTY_REJECTIONS_FILE, rejections)


def list_rejections() -> list[dict]:
    return _read_json(ROYALTY_REJECTIONS_FILE, [])


def register_tenant_api_key(tenant_id: str, api_key: str) -> None:
    store = _read_json(ROYALTY_TENANT_API_KEYS_FILE, {})
    store[tenant_id] = api_key
    _write_json(ROYALTY_TENANT_API_KEYS_FILE, store)


def check_tenant_api_key(tenant_id: str, api_key: str) -> bool:
    store = _read_json(ROYALTY_TENANT_API_KEYS_FILE, {})
    return store.get(tenant_id) == api_key
