# Proof run

Drives the real Archisynapse services end to end and writes evidence JSON.

1. Start PostgreSQL 16 and apply `migrations/*.sql` to database `archisynapse`.
   Create a separate database for analytics (see the evidence README for why).
2. Start `python stripe_stand_in.py` (listens on 127.0.0.1:12111). It is a
   local stand-in for the Stripe test API, not Stripe.
3. Start the services with `ARCHISYNAPSE_PROCESSOR=stripe_test`,
   `STRIPE_SECRET_KEY=sk_test_<anything>` and
   `STRIPE_API_BASE_URL=http://127.0.0.1:12111/v1` on the transaction service,
   plus the gateway keys from `.env.example`.
4. Run:

   ```bash
   GATEWAY_DIR=services/gateway OUT_DIR=docs/evidence/<date>-local-proof-run \
   GIT_COMMIT=$(git rev-parse HEAD) ARCHISYNAPSE_ADMIN_TOKEN=... \
   python tools/proof-run/proof_run.py
   ```

To prove the real Stripe test path instead, skip step 2, set a real `sk_test_`
key and leave `STRIPE_API_BASE_URL` unset. Never use an `sk_live_` key.
