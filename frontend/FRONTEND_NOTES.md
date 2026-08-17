# Archisynapse Ledger Console — first draft

A working React + TypeScript + Vite frontend for Archisynapse, built additively
alongside the existing backend (`services/*`). Nothing in `services/` was
touched, deleted, or rewritten — this is purely a new `frontend/` directory.

## Run it

```bash
cd frontend
npm install
npm run dev
```

Opens on `http://localhost:5173` (or whatever port Vite picks). No backend
required to see the console — it falls back to typed mock data automatically.

## What's live vs. mocked — be precise about this with Manda

**Nothing is live right now**, because no Archisynapse service was running
locally when this was built. But the wiring isn't hypothetical — `src/lib/apiClient.ts`
actively probes `VITE_LEDGER_SERVICE_URL` (default `http://127.0.0.1:3001`)
and `VITE_GATEWAY_URL` (default `http://127.0.0.1:8000`) on load via each
service's real `/health` endpoint, and every view carries a **Live** / **Mock
data** badge reflecting the actual probe result — never hardcoded.

Of the real backend surface, only part of it has an endpoint this console can
call at all today:

| View | Backend endpoint | Status |
|---|---|---|
| Chart of accounts | `GET /accounts` (ledger-service) | **Live-able** — will flip to Live the moment ledger-service is reachable |
| Trial balance | `GET /trial-balance` (ledger-service) | **Live-able**, wired in `apiClient.ts` but not yet surfaced in a view — easy next add |
| Journal / transactions list | — | **No list-all endpoint exists yet.** `ledger-service-api.ts` only has `GET /transactions/:id` (single). Mock-only until that endpoint is added upstream. |
| Royalty obligations, receipts | — | **No list-all endpoint exists yet.** `royalty_routes.py` only has `GET /api/v1/receipts/{id}` (single, by ID) and an admin-only rejections list. Mock-only until a list endpoint exists. |
| Outbox | — | The outbox (`royalty_outbox_simulator.py`) is a *reference implementation Lyrica itself must build*, not a gateway-hosted endpoint. Always mock here by design. |
| Risk / fraud | — | Risk scoring is embedded in the receipt's `decision` block, not separately listable. Mock-only. |

**All mock data is typed against the real Pydantic/TS schemas, not invented
shapes** — `src/types/{ledger,royalty,risk}.ts` mirror
`services/ledger/ledger-service-types.ts` and `services/gateway/royalty_events.py`
field-for-field, including the tenant-scoped idempotency key, the signed
receipt envelope (`decision`, `signature`, `payouts[]`), and the outbox state
machine. The Outbox view's retry legend (409 `processing` = retryable, 409
`idempotency_conflict` = terminal) is pulled directly from the comments and
logic in `royalty_outbox_simulator.py`, not guessed.

## Decisions for Manda to weigh in on

1. **Palette.** Obsidian base + deep gold accent (`#c9a24d`) is the starting
   point from your canon, deliberately not neon-blue. All accent usage is a
   single CSS variable (`--accent` in `src/index.css`) — a one-line change
   swaps it (e.g. to a deep teal) to A/B the feel.
2. **Which view is "home."** Ledger is first in the nav today because it's
   the most foundational (money in/out). Royalty Splits or Receipts might be
   a better landing view if the primary audience is creators/tenants rather
   than internal finance ops.
3. **Missing list endpoints.** The journal, receipts, and royalty views can't
   go fully live without `GET /transactions` and `GET /api/v1/receipts`
   (list) on the real services. Worth prioritizing if "see live data" is the
   next milestone — I did not add these to the backend since that's outside
   an additive-frontend-only change.
4. **Tenant switching.** Right now the console is hardcoded to one demo
   tenant (`lyrica-music-group`). A real console needs a tenant/org switcher
   once there's more than one to look at.

## What's intentionally not here

No auth, no write actions (nothing posts a transaction or registers a key),
no real signing-key material, no deploy config. This is a read-only console
for seeing the ledger/receipt/risk state clearly — exactly what was asked
for as a first draft.
