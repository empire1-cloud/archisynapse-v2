# Lyrica VICS Ownership Verifier

This gateway integration verifies Lyrica ownership proof before Archisynapse creates a royalty obligation.

## Safety posture

The verifier is disabled by default. When disabled, missing, misconfigured, unavailable, or returning a mismatched proof, royalty ownership verification fails closed and the event receives `ownership_invalid`. A structurally valid JSON event is never treated as proof by itself.

Test fixtures remain independent and require `ROYALTY_TEST_FIXTURES_ENABLED=true`. Never enable that variable in a production environment.

## Required environment variables

```bash
LYRICA_VICS_VERIFIER_ENABLED=true
LYRICA_VICS_VERIFY_URL=https://<lyrica-service>/internal/vics/verify
LYRICA_VICS_SERVICE_TOKEN=<service-to-service bearer token>
LYRICA_VICS_VERIFY_TIMEOUT_SECONDS=5
```

The service token must be stored in the deployment secret manager. Do not commit it, print it, return it to clients, or reuse Lyrica's private VICS signing secret.

## Request contract

Archisynapse sends:

```json
{
  "track_id": "trk_9f3a2b1c",
  "dna_tag": "dna_v2_7c1e",
  "soulprint_hash": "sp_sha256_4b09",
  "vics_proof_id": "vics_01H",
  "creator_id": "cre_a1b2c3"
}
```

Headers:

```text
Authorization: Bearer <LYRICA_VICS_SERVICE_TOKEN>
X-Empire1-Service: archisynapse-v2
Content-Type: application/json
```

## Required Lyrica response

```json
{
  "verified": true,
  "revoked": false,
  "track_id": "trk_9f3a2b1c",
  "dna_tag": "dna_v2_7c1e",
  "soulprint_hash": "sp_sha256_4b09",
  "vics_proof_id": "vics_01H",
  "creator_id": "cre_a1b2c3",
  "expires_at": "2026-12-31T23:59:59Z"
}
```

`expires_at` is optional. When present, it must be a valid future ISO-8601 timestamp.

The verifier accepts the proof only when:

1. HTTP status is 200.
2. `verified` is exactly `true`.
3. `revoked` is not `true`.
4. Every identity and proof field exactly matches the royalty event.
5. The proof is not expired.

Any non-200 response, timeout, malformed JSON, missing field, mismatch, revocation, or expiration returns `False` and creates no allowed financial posting.

## Key rotation

1. Add a new service token to Lyrica and the deployment secret manager.
2. Deploy Lyrica so both old and new tokens are accepted during the overlap window.
3. Update `LYRICA_VICS_SERVICE_TOKEN` in Archisynapse and deploy.
4. Verify one valid proof and one invalid-token rejection.
5. Revoke the old token in Lyrica.
6. Record the rotation date and deployment receipt.

Do not rotate the Ed25519 event-signing key through this token flow. Tenant event keys use the separate gateway admin registration path.

## Validation

Focused isolated suite:

```bash
pytest services/gateway/test_lyrica_vics_verifier.py
```

Coverage includes exact valid binding, revoked proof, DNA mismatch, Soulprint mismatch, creator mismatch, expiration, non-200 response, missing configuration, malformed JSON, and timeout.
