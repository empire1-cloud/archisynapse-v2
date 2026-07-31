# Archisynapse Merchant Access Lifecycle

This operator closes the merchant-access lifecycle against the existing PostgreSQL gateway schema.

It supports:

- viewing a merchant's status and key metadata without exposing hashes or secrets
- rotating all active keys into one fresh key
- revoking one merchant-scoped key
- suspending a merchant and revoking every active key atomically
- resuming only a suspended merchant and issuing one fresh key atomically
- writing an audit event for every mutation

The generated API key is printed once. PostgreSQL stores only its Argon2 hash.

## Setup

Run the existing Archisynapse migrations first, then configure the gateway database:

```bash
export DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:5432/archisynapse
python -m pip install -r services/gateway/requirements.txt
```

## Commands

```bash
python tools/merchant-admin/app.py show mer_...
python tools/merchant-admin/app.py rotate-key mer_... --environment test
python tools/merchant-admin/app.py revoke-key mer_... <key_id>
python tools/merchant-admin/app.py suspend mer_...
python tools/merchant-admin/app.py resume mer_... --environment test
```

## Safety and recovery behavior

- Rotation refuses unless the merchant is `ACTIVE`.
- Suspension refuses for a `CLOSED` merchant.
- Resumption refuses unless the merchant is `SUSPENDED`.
- Suspension revokes all active keys in the same database transaction.
- Resumption creates a fresh key in the same transaction that reactivates the merchant.
- A failed transaction returns no usable key and leaves no partial lifecycle change.
- The operator never prints stored hashes, encrypted service credentials, or other secrets.

This is an operational control for the current gateway. It does not prove processor settlement, production readiness, compliance certification, or customer traction.
