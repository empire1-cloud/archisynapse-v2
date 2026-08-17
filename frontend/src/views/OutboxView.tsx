import { useEffect, useState } from 'react';
import { fetchOutboxRows, type DataSourceMode } from '../lib/apiClient';
import type { OutboxRow, OutboxState } from '../types/royalty';
import { formatDateTime, truncateId } from '../lib/format';
import { SourceBadge } from '../components/SourceBadge';

const STATE_PILL: Record<OutboxState, string> = {
  pending: 'pill-neutral',
  sent: 'pill-warn',
  processing: 'pill-accent',
  receipted: 'pill-ok',
  rejected: 'pill-danger',
};

const STATE_LABEL: Record<OutboxState, string> = {
  pending: 'Pending',
  sent: 'Retrying',
  processing: 'In flight',
  receipted: 'Receipted',
  rejected: 'Rejected (terminal)',
};

export function OutboxView() {
  const [rows, setRows] = useState<OutboxRow[]>([]);
  const [source, setSource] = useState<DataSourceMode>('mock');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const r = await fetchOutboxRows();
      if (cancelled) return;
      setRows(r.data);
      setSource(r.source);
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) return <div className="empty-hint">Loading outbox…</div>;

  const counts = rows.reduce(
    (acc, r) => {
      acc[r.state] = (acc[r.state] ?? 0) + 1;
      return acc;
    },
    {} as Record<OutboxState, number>,
  );

  return (
    <div>
      <div className="stat-row">
        <div className="stat-card">
          <div className="stat-label">In flight</div>
          <div className="stat-value accent">{(counts.processing ?? 0) + (counts.sent ?? 0) + (counts.pending ?? 0)}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Receipted</div>
          <div className="stat-value ok">{counts.receipted ?? 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Rejected (terminal)</div>
          <div className="stat-value danger">{counts.rejected ?? 0}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Total rows</div>
          <div className="stat-value">{rows.length}</div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">409 retry semantics</div>
            <div className="panel-sub">royalty_outbox_simulator.py — what the outbox does with each response</div>
          </div>
        </div>
        <div className="kv-list">
          <div className="kv-row">
            <span className="kv-key">
              <span className="pill pill-warn" style={{ marginRight: 8 }}>
                409 processing
              </span>
              retryable
            </span>
            <span className="kv-val" style={{ textAlign: 'left', maxWidth: 420 }}>
              The original request with this idempotency_key is still being processed. Same row retries with capped
              exponential backoff + jitter, same event_id / idempotency_key / correlation_id.
            </span>
          </div>
          <div className="kv-row">
            <span className="kv-key">
              <span className="pill pill-danger" style={{ marginRight: 8 }}>
                409 idempotency_conflict
              </span>
              terminal
            </span>
            <span className="kv-val" style={{ textAlign: 'left', maxWidth: 420 }}>
              Same idempotency_key replayed with a different payload. Not retried — this outbox row is marked{' '}
              <code>rejected</code> permanently; a genuinely new attempt needs a new idempotency_key.
            </span>
          </div>
          <div className="kv-row">
            <span className="kv-key">
              <span className="pill pill-neutral" style={{ marginRight: 8 }}>
                503 / connection error
              </span>
              retryable
            </span>
            <span className="kv-val" style={{ textAlign: 'left', maxWidth: 420 }}>
              retry_later or a network failure — always retried, same as 409 processing.
            </span>
          </div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Outbox rows</div>
            <div className="panel-sub">Lyrica-side transactional outbox against POST /api/v1/events</div>
          </div>
          <SourceBadge source={source} />
        </div>
        <table>
          <thead>
            <tr>
              <th>Event</th>
              <th>State</th>
              <th>Attempts</th>
              <th>Last error</th>
              <th>Next attempt</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.event_id}>
                <td>
                  <code className="idcode">{truncateId(r.event_id)}</code>
                </td>
                <td>
                  <span className={`pill ${STATE_PILL[r.state]}`}>
                    <span className="pill-dot" />
                    {STATE_LABEL[r.state]}
                  </span>
                </td>
                <td className="td-num">{r.attempts}</td>
                <td className="text-secondary" style={{ maxWidth: 320 }}>
                  {r.last_error ?? <span className="text-tertiary">—</span>}
                </td>
                <td className="text-tertiary">{formatDateTime(r.next_attempt_at)}</td>
                <td className="text-tertiary">{formatDateTime(r.created_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
