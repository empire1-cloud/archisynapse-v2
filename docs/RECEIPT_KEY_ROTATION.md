# Receipt Signing Key Rotation

Archisynapse signs every stored payment receipt with an Ed25519 key. This runbook
replaces that key without breaking any receipt signed before the change.

## What changes and what does not

- New receipts are signed with the new key.
- Every earlier receipt keeps its original signature and still verifies, because
  its public key stays in the trusted key ring as a **retired** key.
- No receipt is re-signed, edited, or deleted.

## Trust model

`GET /v1/receipts/{event_id}/verify` and `/evidence` accept a receipt only when:

1. its `key_id` is one Archisynapse issued (active or retired), and
2. the signature verifies against that trusted public key.

The public key embedded in a receipt is not trusted on its own. A receipt signed
with any other key is reported as `valid: false`, `key_status: "unknown"`.

The legacy `verify_receipt` function still exists and is used only when no
signing key is configured; its result is labelled `key_status: "unchecked"`.

## Rotate

1. Read the current public key and key id:

   ```bash
   curl -s "$GATEWAY_URL/v1/proof/key"
   ```

2. Generate a new 32-byte seed and choose a new key id (never reuse one):

   ```bash
   python - <<'PY'
   import base64, os
   print(base64.urlsafe_b64encode(os.urandom(32)).decode().rstrip("="))
   PY
   ```

3. Update the environment in one deploy:

   ```dotenv
   ARCHISYNAPSE_RECEIPT_SIGNING_PRIVATE_KEY=<new seed>
   ARCHISYNAPSE_RECEIPT_SIGNING_KEY_ID=receipt-v2
   ARCHISYNAPSE_RECEIPT_RETIRED_PUBLIC_KEYS={"receipt-v1":"<old public key from step 1>"}
   ```

   On later rotations, keep every earlier entry and add the newly retired key.

4. Restart the gateway and confirm:

   ```bash
   curl -s "$GATEWAY_URL/v1/proof/keys"
   ```

   The new key shows `"status": "active"`; the old one shows `"status": "retired"`.

5. Verify one receipt from before the rotation:

   ```bash
   curl -s -H "Authorization: Bearer $MERCHANT_KEY" \
     "$GATEWAY_URL/v1/receipts/<old event id>/verify"
   ```

   Expect `"valid": true` and `"key_status": "retired"`.

Startup fails with a clear error if the retired-keys value is not a JSON object,
a key is not a 32-byte Ed25519 public key, or the active key id also appears as
retired.

## Verify offline

Save the key list once, then check any receipt with no network access:

```bash
curl -s "$GATEWAY_URL/v1/proof/keys" > keys.json
python tools/verify-receipt/verify_receipt.py receipt.json keys.json
```

Exit code 0 means valid and signed by a trusted key.

## Proven by tests

`services/gateway/tests/test_receipt_key_rotation.py` (runs in CI):

- receipt signed before rotation verifies after it, across two rotations
- new receipts sign with the new key
- a receipt signed with an unissued key is rejected, although the legacy check accepts it
- key-id spoofing with an attacker key, or with a copied public key, is rejected
- tampering is rejected
- bad configuration fails loudly
- the offline verifier gives the same answers
- the gateway verify helper and `/v1/proof/keys` report key status

## Not covered

- Secure storage of the private seed (secrets manager, HSM). Today it is an
  environment variable.
- Revoking a compromised key. Retired keys stay trusted by design; revocation
  needs a separate list and is not built.
- The royalty receipt signer in `royalty_keys.py` is a separate key and is not
  part of this ring.
