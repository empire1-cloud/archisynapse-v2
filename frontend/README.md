# Archisynapse — Ledger Console

First-draft frontend for Archisynapse: a read-only console over the ledger,
signed royalty receipts, risk/fraud decisions, and the outbox delivery
pipeline. React + TypeScript + Vite, no UI framework, no state library.

See [`FRONTEND_NOTES.md`](FRONTEND_NOTES.md) for what's wired live vs.
mocked and the open decisions for design direction.

## Develop

```bash
npm install
npm run dev
```

## Build

```bash
npm run build
```

## Configure a live backend

```bash
# .env.local
VITE_LEDGER_SERVICE_URL=http://127.0.0.1:3001
VITE_GATEWAY_URL=http://127.0.0.1:8000
```

The console probes `/health` on both at load and switches each panel's badge
between **Live** and **Mock data** accordingly — never silently.
