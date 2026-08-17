import { useEffect, useState } from 'react';
import { fetchObligationEvents, fetchReceipts, type DataSourceMode } from '../lib/apiClient';
import type { RoyaltyObligationCreated, UnifiedReceipt } from '../types/royalty';
import { formatBps, formatDateTime, formatMoney, truncateId } from '../lib/format';
import { SourceBadge } from '../components/SourceBadge';

const STATUS_PILL: Record<string, string> = {
  paid: 'pill-ok',
  held: 'pill-warn',
  blocked: 'pill-danger',
  processing: 'pill-neutral',
  reversed: 'pill-neutral',
  rejected: 'pill-danger',
};

export function RoyaltyView({ focusId }: { focusId?: string | null }) {
  const [events, setEvents] = useState<RoyaltyObligationCreated[]>([]);
  const [receipts, setReceipts] = useState<UnifiedReceipt[]>([]);
  const [source, setSource] = useState<DataSourceMode>('mock');
  const [selected, setSelected] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [e, r] = await Promise.all([fetchObligationEvents(), fetchReceipts()]);
      if (cancelled) return;
      setEvents(e.data);
      setReceipts(r.data);
      setSource(e.source);
      setSelected(e.data[0]?.event_id ?? null);
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!focusId || events.length === 0) return;
    if (events.some((e) => e.event_id === focusId)) setSelected(focusId);
  }, [focusId, events]);

  if (loading) return <div className="empty-hint">Loading royalty obligations…</div>;

  const receiptFor = (eventId: string) => receipts.find((r) => r.event_id === eventId);
  const activeEvent = events.find((e) => e.event_id === selected);
  const activeReceipt = activeEvent ? receiptFor(activeEvent.event_id) : undefined;

  return (
    <div className="split-layout">
      <div>
        <div className="panel">
          <div className="panel-header">
            <div>
              <div className="panel-title">Royalty obligations</div>
              <div className="panel-sub">royalty.obligation.created events — splits[].bps must sum to 10000</div>
            </div>
            <SourceBadge source={source} />
          </div>
          <table>
            <thead>
              <tr>
                <th>Event</th>
                <th>Track</th>
                <th>Trigger</th>
                <th>Owners</th>
                <th>Claim status</th>
                <th style={{ textAlign: 'right' }}>Gross</th>
              </tr>
            </thead>
            <tbody>
              {events.map((ev) => {
                const receipt = receiptFor(ev.event_id);
                return (
                  <tr
                    key={ev.event_id}
                    className="clickable"
                    onClick={() => setSelected(ev.event_id)}
                    style={selected === ev.event_id ? { background: 'var(--bg-surface-raised)' } : undefined}
                  >
                    <td>
                      <code className="idcode">{truncateId(ev.event_id)}</code>
                    </td>
                    <td>{ev.track.track_id}</td>
                    <td className="text-secondary">{ev.trigger.kind}</td>
                    <td className="text-secondary">{ev.splits.length}</td>
                    <td>
                      {receipt ? (
                        <span className={`pill ${STATUS_PILL[receipt.status]}`}>
                          <span className="pill-dot" />
                          {receipt.status}
                        </span>
                      ) : (
                        <span className="pill pill-neutral">
                          <span className="pill-dot" />
                          no receipt
                        </span>
                      )}
                    </td>
                    <td className="td-num">{formatMoney(ev.amount.value, ev.amount.currency)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </div>

      <div className="detail-panel">
        {activeEvent ? (
          <div className="panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">{activeEvent.track.track_id}</div>
                <div className="panel-sub">
                  <code className="idcode">{activeEvent.event_id}</code>
                </div>
              </div>
              {activeReceipt && (
                <span className={`pill ${STATUS_PILL[activeReceipt.status]}`}>
                  <span className="pill-dot" />
                  {activeReceipt.status}
                </span>
              )}
            </div>

            <div className="section-label" style={{ padding: '12px 16px 0' }}>
              Split — {activeEvent.splits.length} owner{activeEvent.splits.length > 1 ? 's' : ''}
            </div>
            <div style={{ padding: '0 16px' }}>
              <div className="split-bar">
                {activeEvent.splits.map((s) => (
                  <div key={s.owner_id} className="split-bar-seg" style={{ width: `${s.bps / 100}%` }} />
                ))}
              </div>
            </div>
            <div className="kv-list">
              {activeEvent.splits.map((s) => (
                <div className="kv-row" key={s.owner_id}>
                  <span className="kv-key">{s.owner_id}</span>
                  <span className="kv-val">{formatBps(s.bps)}</span>
                </div>
              ))}
            </div>

            <div className="section-label" style={{ padding: '12px 16px 0' }}>
              Trigger &amp; provenance
            </div>
            <div className="kv-list">
              <div className="kv-row">
                <span className="kv-key">Kind</span>
                <span className="kv-val">{activeEvent.trigger.kind}</span>
              </div>
              <div className="kv-row">
                <span className="kv-key">Source</span>
                <span className="kv-val">{activeEvent.trigger.source_ref}</span>
              </div>
              <div className="kv-row">
                <span className="kv-key">DNA tag</span>
                <span className="kv-val">{activeEvent.track.dna_tag}</span>
              </div>
              <div className="kv-row">
                <span className="kv-key">VICS proof</span>
                <span className="kv-val">{activeEvent.track.vics_proof.proof_id}</span>
              </div>
              <div className="kv-row">
                <span className="kv-key">Occurred</span>
                <span className="kv-val">{formatDateTime(activeEvent.occurred_at)}</span>
              </div>
            </div>

            {activeReceipt && (
              <>
                <div className="section-label" style={{ padding: '12px 16px 0' }}>
                  Payout claims
                </div>
                <table>
                  <thead>
                    <tr>
                      <th>Owner</th>
                      <th>State</th>
                      <th style={{ textAlign: 'right' }}>Amount</th>
                    </tr>
                  </thead>
                  <tbody>
                    {activeReceipt.payouts.map((p) => (
                      <tr key={p.owner_id}>
                        <td>{p.owner_id}</td>
                        <td>
                          <span
                            className={`pill ${
                              p.state === 'posted' ? 'pill-ok' : p.state === 'held' ? 'pill-warn' : 'pill-danger'
                            }`}
                          >
                            {p.state}
                          </span>
                        </td>
                        <td className="td-num">{formatMoney(p.amount, activeReceipt.amounts.currency)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </>
            )}
          </div>
        ) : (
          <div className="panel">
            <div className="empty-hint">Select an obligation to inspect its split.</div>
          </div>
        )}
      </div>
    </div>
  );
}
