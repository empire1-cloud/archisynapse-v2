import json
from pathlib import Path
from typing import Any, Dict, List


STATE_DIR = Path(__file__).resolve().parent / ".runtime"
STATE_DIR.mkdir(exist_ok=True)

MERCHANT_FILE = STATE_DIR / "merchant_credentials.json"
LEDGER_RECOVERY_FILE = STATE_DIR / "ledger_recovery.json"
ANALYTICS_RECOVERY_FILE = STATE_DIR / "analytics_recovery.json"
RECEIPTS_DIR = STATE_DIR / "receipts"
RECEIPTS_DIR.mkdir(exist_ok=True)


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


def load_merchant_credentials() -> Dict[str, Dict[str, str]]:
    return _read_json(MERCHANT_FILE, {})


def save_merchant_credentials(credentials: Dict[str, Dict[str, str]]) -> None:
    _write_json(MERCHANT_FILE, credentials)


def get_recovery_queue(path: Path) -> List[Dict[str, Any]]:
    return _read_json(path, [])


def push_recovery_item(path: Path, item: Dict[str, Any]) -> None:
    queue = get_recovery_queue(path)
    correlation_id = item.get("correlation_id")
    queue = [existing for existing in queue if existing.get("correlation_id") != correlation_id]
    queue.append(item)
    _write_json(path, queue)


def remove_recovery_item(path: Path, correlation_id: str) -> None:
    queue = get_recovery_queue(path)
    queue = [existing for existing in queue if existing.get("correlation_id") != correlation_id]
    _write_json(path, queue)


def save_receipt(receipt: Dict[str, Any]) -> None:
    event_id = receipt.get("event_id", "unknown")
    receipt_file = RECEIPTS_DIR / f"{event_id}.json"
    _write_json(receipt_file, receipt)


def load_receipt(event_id: str) -> Dict[str, Any] | None:
    receipt_file = RECEIPTS_DIR / f"{event_id}.json"
    return _read_json(receipt_file, None)


def load_all_receipts() -> Dict[str, Dict[str, Any]]:
    receipts = {}
    for receipt_file in RECEIPTS_DIR.glob("*.json"):
        data = _read_json(receipt_file, None)
        if data and "event_id" in data:
            receipts[data["event_id"]] = data
    return receipts
