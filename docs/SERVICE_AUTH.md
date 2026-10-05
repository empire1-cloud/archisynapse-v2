# Service-to-service authentication

The ledger and transaction services act for whichever organization the
`X-Organization-ID` header names. They now accept that header only from a
caller that proves which internal service it is.

| Caller | Calls | Scope |
| --- | --- | --- |
| gateway | transaction service | write |
| gateway | ledger service | read (GET/HEAD only; the gateway never posts to the ledger) |
| transaction service | ledger service | write |

Anything else, including a request that only sends `X-Organization-ID`, gets
`401`. A read-only caller attempting a write gets `403`. `/health` and
`/ready` stay open.

## Local (docker compose)

Nothing to do. The `service-tokens` service creates one random token per row
above on first start and keeps them in `service_tokens_store`, a volume only
it mounts. Each service then gets its own volume with only what it needs:

| Service | Holds |
| --- | --- |
| gateway | its two outbound tokens (transaction write, ledger read) |
| transaction | its ledger write token; SHA-256 of the gateway's token |
| ledger | SHA-256 of the gateway's and the transaction service's tokens |

So the gateway never holds a ledger write token, and a receiving service
never holds a usable copy of its callers' tokens. `docker compose down -v`
deletes them; the next `up` creates new ones.

## Outside compose

Receiving service (ledger or transaction):

```
ARCHISYNAPSE_SERVICE_AUTH=enforce
ARCHISYNAPSE_INBOUND_SERVICE_TOKENS=gateway:read:sha256:<hex>,transaction:write:sha256:<hex>
# or ARCHISYNAPSE_INBOUND_SERVICE_TOKENS_FILE=/path/to/file (one entry per line)
# Each entry is caller:scope:token or caller:scope:sha256:<SHA-256 of the
# token, 64 hex>. Prefer the digest form: the service can check a token
# without holding it. printf '%s' "$TOKEN" | sha256sum
```

Calling service:

```
# gateway
ARCHISYNAPSE_GATEWAY_TO_TRANSACTION_TOKEN=<token>   # or ..._TOKEN_FILE
ARCHISYNAPSE_GATEWAY_TO_LEDGER_TOKEN=<token>        # or ..._TOKEN_FILE
# transaction service
ARCHISYNAPSE_TRANSACTION_TO_LEDGER_TOKEN=<token>    # or ..._TOKEN_FILE
```

Every hop has its own variable name, so a shared environment cannot hand one
service another service's token.

Tokens must be at least 32 characters; use 32 random bytes. Keep them in a
secret manager, never in source control or `.env` files that are committed.

In `enforce` mode (the default) a service with no valid entries refuses to
start. A caller with no token sends no `Authorization` header and is refused.
Nothing falls back to trusting the header.

## Rotating a token

1. Add the new token as a second entry for the same caller on the receiving
   service and restart it. Both tokens work.
2. Give the caller the new token. Callers re-read token files on every call,
   so no caller restart is needed.
3. Remove the old entry and restart the receiving service.

## Limits

- The gateway holds valid tokens, so a compromised gateway can still act for
  any merchant. Merchant API-key authentication at the gateway is the control
  for that.
- Request bodies are parsed only after authentication; an unauthenticated
  request is refused before the service reads its body.
- Tokens travel over the internal network. Use TLS or a private network you
  control in production.
- `ARCHISYNAPSE_SERVICE_AUTH=off` restores the old behaviour. Use it only
  where nothing but the gateway can reach these services.

Evidence: `docs/evidence/2026-10-05-service-auth-proof-run/`, the `Docker
stack` workflow, and the `service-auth.test.ts` suites in both services.
