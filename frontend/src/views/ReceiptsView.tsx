import { useEffect, useState } from 'react';
import { fetchReceipts, type DataSourceMode } from '../lib/apiClient';
import type { UnifiedReceipt } from '../types/royalty';
import { formatDateTime, formatMoney, truncateId } from '../lib/format';
import { SourceBadge } from '../components/SourceBadge';

const STATUS_PILL: Record<string, string> = {
  paid: 'pill-ok',
  held: 'pill-warn',
  blocked: 'pill-danger',
  processing: 'pill-neutral',
  reversed: 'pill-neutral',
  rejected: 'pill-danger',
};

export function ReceiptsView({ focusId }: { focusId?: string | null }) {
  const [receipts, setReceipts] = useState<UnifiedReceipt[]>([]);
  const [source, setSource] = useState<DataSourceMode>('mock');
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const r = await fetchReceipts();
      if (cancelled) return;
      setReceipts(r.data);
      setSource(r.source);
      setSelected(r.data[0]?.receipt_id ?? null);
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!focusId || receipts.length === 0) return;
    const match = receipts.find((r) => r.receipt_id === focusId || r.event_id === focusId);
    if (match) setSelected(match.receipt_id);
  }, [focusId, receipts]);

  if (loading) return <div className="empty-hint">Loading signed receipts…</div>;

  const active = receipts.find((r) => r.receipt_id === selected);

  return (
    <div className="split-layout">
      <div>
        <div className="panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Signed payout receipts</div>
              <div className="panel-sub">GET /api/v1/receipts/{'{'}id{'}'} — ed25519-signed by the gateway key</div>
            </div>
            <SourceBadge source={source} />
          </div>
          <table>
            <thead>
              <tr>
                <th>Receipt</th>
                <th>Event</th>
                <th>Status</th>
                <th>Policy</th>
                <th style={{ textAlign: 'right' }}>Net</th>
                <th>Issued</th>
              </tr>
            </thead>
            <tbody>
              {receipts.map((r) => (
                <tr
                  key={r.receipt_id}
                  className="clickable"
                  onClick={() => setSelected(r.receipt_id)}
                  style={selected === r.receipt_id ? { background: 'var(--bg-surface-raised)' } : undefined}
                >
                  <td>
                    <code className="idcode">{truncateId(r.receipt_id, 8)}</code>
                  </td>
                  <td className="text-secondary">{truncateId(r.event_id)}</td>
                  <td>
                    <span className={`pill ${STATUS_PILL[r.status]}`}>
                      <span className="pill-dot" />
                      {r.status}
                    </span>
                  </td>
                  <td className="text-secondary">{r.decision.policy}</td>
                  <td className="td-num">{formatMoney(r.amounts.net, r.amounts.currency)}</td>
                  <td className="text-tertiary">{formatDateTime(r.issued_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="detail-panel">
        {active ? (
          <div className="panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">Receipt</div>
                <div className="panel-sub">
                  <code className="idcode">{active.receipt_id}</code>
                </div>
              </div>
              <span className={`pill ${STATUS_PILL[active.status]}`}>
                <span className="pill-dot" />
                {active.status}
              </span>
            </div>

            {active.status_reasons.length > 0 && (
              <div style={{ padding: '12px 16px 0', display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {active.status_reasons.map((reason) => (
                  <span key={reason} className="pill pill-warn">
                    {reason}
                  </span>
                ))}
              </div>
            )}

            <div className="section-label" style={{ padding: '12px 16px 0' }}>
              Amounts
            </div>
            <div className="kv-list">
              <div className="kv-row">
                <span className="kv-key">Gross</span>
                <span className="kv-val">{formatMoney(active.amounts.gross, active.amounts.currency)}</span>
              </div>
              <div className="kv-row">
                <span className="kv-key">Platform fee</span>
                <span className="kv-val">{formatMoney(active.amounts.platform_fee, active.amounts.currency)}</span>
              </div>
              <div className="kv-row">
                <span className="kv-key">Net</span>
                <span className="kv-val">{formatMoney(active.amounts.net, active.amounts.currency)}</span>
              </div>
            </div>

            <div className="section-label" style={{ padding: '12px 16px 0' }}>
              Decision
            </div>
            <div className="kv-list">
              <div className="kv-row">
                <span className="kv-key">Policy</span>
                <span className="kv-val">{active.decision.policy}</span>
              </div>
              <div className="kv-row">
                <span className="kv-key">Risk score</span>
                <span className="kv-val">{active.decision.risk_score.toFixed(2)}</span>
              </div>
              <div className="kv-row">
                <span className="kv-key">Checks</span>
                <span className="kv-val">{active.decision.checks.join(', ')}</span>
              </div>
              <div className="kv-row">
                <span className="kv-key">Ledger txn</span>
                <span className="kv-val">{active.ledger_transaction_id ? truncateId(active.ledger_transaction_id) : '—'}</span>
              </div>
              <div className="kv-row">
                <span className="kv-key">Issued</span>
                <span className="kv-val">{formatDateTime(active.issued_at)}</span>
              </div>
            </div>

            <div className="section-label" style={{ padding: '12px 16px 0' }}>
              Signature
            </div>
            <div style={{ padding: '0 16px 16px' }}>
              <div className="sig-verified">
                <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                  <path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z" />
                  <path d="M9.5 12l2 2 3.5-3.5" />
                </svg>
                {active.signature.alg} · key {active.signature.key_id}
              </div>
              <div className="sig-block">{active.signature.value}</div>
              <div className="footnote">
                Signed over the canonical receipt body (sorted keys) by the gateway's ed25519 key. Verify against{' '}
                <code>GET /api/v1/keys/{active.signature.key_id}</code>.
              </div>
            </div>
          </div>
        ) : (
          <div className="panel">
            <div className="empty-hint">Select a receipt to inspect its signature.</div>
          </div>
        )}
      </div>
    </div>
  );
}
