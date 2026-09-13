CREATE TABLE IF NOT EXISTS economic_truth_outbox (
  id uuid PRIMARY KEY,
  source text NOT NULL,
  external_id text NOT NULL,
  action_id text NOT NULL,
  stage text NOT NULL CHECK (stage IN ('outcome','verify','reverse')),
  payload jsonb NOT NULL,
  state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','sending','delivered')),
  attempts integer NOT NULL DEFAULT 0,
  last_error text,
  response jsonb,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (source, external_id, stage)
);
CREATE INDEX IF NOT EXISTS economic_truth_outbox_due
  ON economic_truth_outbox (next_attempt_at, created_at)
  WHERE state IN ('pending','sending');
