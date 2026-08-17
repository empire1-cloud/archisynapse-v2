import { useCallback, useRef, useState } from 'react';
import { mockObligationEvents, mockOutboxRows, mockReceipts, mockTransactions } from '../lib/mockData';
import { formatMoney } from '../lib/format';
import { BoltIcon, CoinIcon, LedgerIcon, OutboxIcon, ReceiptIcon, ShieldIcon } from '../components/icons';

const NODE_W = 236;
const NODE_H = 122;

type NodeId = 'event' | 'risk' | 'outbox' | 'ledger' | 'receipt' | 'payout';

interface FlowNodeDef {
  id: NodeId;
  x: number;
  y: number;
  kicker: string;
  title: string;
  icon: typeof LedgerIcon;
  metrics: { label: string; value: string; tone?: 'ok' | 'warn' | 'accent' }[];
}

const EDGES: { from: NodeId; to: NodeId; branch?: boolean }[] = [
  { from: 'event', to: 'risk' },
  { from: 'event', to: 'outbox', branch: true },
  { from: 'risk', to: 'ledger' },
  { from: 'ledger', to: 'receipt' },
  { from: 'outbox', to: 'receipt', branch: true },
  { from: 'receipt', to: 'payout' },
];

function buildNodes(): FlowNodeDef[] {
  const ev = mockObligationEvents[0];
  const receipt = mockReceipts[0];
  const outbox = mockOutboxRows[0];
  const ledgerTxns = mockTransactions.filter((t) => t.referenceId === ev.event_id);

  return [
    {
      id: 'event',
      x: 20,
      y: 28,
      kicker: 'royalty.obligation.created',
      title: ev.track.track_id,
      icon: BoltIcon,
      metrics: [
        { label: 'Gross', value: formatMoney(ev.amount.value, ev.amount.currency) },
        { label: 'Splits', value: String(ev.splits.length) },
      ],
    },
    {
      id: 'risk',
      x: 20,
      y: 300,
      kicker: 'fraud service · risk decision',
      title: receipt.decision.policy,
      icon: ShieldIcon,
      metrics: [
        { label: 'Risk', value: receipt.decision.risk_score.toFixed(2), tone: 'ok' },
        { label: 'Checks', value: String(receipt.decision.checks.length) },
      ],
    },
    {
      id: 'outbox',
      x: 340,
      y: 468,
      kicker: 'lyrica outbox',
      title: 'delivery attempt',
      icon: OutboxIcon,
      metrics: [
        { label: 'State', value: outbox.state, tone: 'ok' },
        { label: 'Attempts', value: String(outbox.attempts) },
      ],
    },
    {
      id: 'ledger',
      x: 380,
      y: 138,
      kicker: 'ledger service',
      title: ledgerTxns[0]?.id ?? 'txn',
      icon: LedgerIcon,
      metrics: [
        { label: 'Entries', value: String(ledgerTxns.reduce((n, t) => n + t.entries.length, 0)) },
        { label: 'Status', value: 'posted', tone: 'ok' },
      ],
    },
    {
      id: 'receipt',
      x: 700,
      y: 258,
      kicker: 'signed receipt',
      title: receipt.receipt_id.slice(0, 12) + '…',
      icon: ReceiptIcon,
      metrics: [
        { label: 'Net', value: formatMoney(receipt.amounts.net, receipt.amounts.currency), tone: 'accent' },
        { label: 'Sig', value: receipt.signature.alg },
      ],
    },
    {
      id: 'payout',
      x: 840,
      y: 468,
      kicker: 'payout claims',
      title: `${receipt.payouts.length} owners`,
      icon: CoinIcon,
      metrics: [
        { label: 'State', value: receipt.payouts[0]?.state ?? '—', tone: 'ok' },
        { label: 'Owners', value: String(receipt.payouts.length) },
      ],
    },
  ];
}

const INITIAL_NODES = buildNodes();

function center(pos: { x: number; y: number }) {
  return { x: pos.x + NODE_W / 2, y: pos.y + NODE_H / 2 };
}

export function FlowView() {
  const [positions, setPositions] = useState<Record<NodeId, { x: number; y: number }>>(() =>
    Object.fromEntries(INITIAL_NODES.map((n) => [n.id, { x: n.x, y: n.y }])) as Record<NodeId, { x: number; y: number }>,
  );
  const [dragging, setDragging] = useState<NodeId | null>(null);
  const canvasRef = useRef<HTMLDivElement>(null);
  const dragOffset = useRef({ dx: 0, dy: 0 });

  const onPointerDown = useCallback(
    (id: NodeId) => (e: React.PointerEvent) => {
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const pos = positions[id];
      dragOffset.current = {
        dx: e.clientX - rect.left - pos.x,
        dy: e.clientY - rect.top - pos.y,
      };
      setDragging(id);
      (e.target as Element).setPointerCapture(e.pointerId);
    },
    [positions],
  );

  const onPointerMove = useCallback(
    (e: React.PointerEvent) => {
      if (!dragging) return;
      const canvas = canvasRef.current;
      if (!canvas) return;
      const rect = canvas.getBoundingClientRect();
      const rawX = e.clientX - rect.left - dragOffset.current.dx;
      const rawY = e.clientY - rect.top - dragOffset.current.dy;
      const x = Math.min(Math.max(rawX, 4), rect.width - NODE_W - 4);
      const y = Math.min(Math.max(rawY, 4), rect.height - NODE_H - 4);
      setPositions((prev) => ({ ...prev, [dragging]: { x, y } }));
    },
    [dragging],
  );

  const endDrag = useCallback(() => setDragging(null), []);

  return (
    <div className="flow-shell">
      <div className="flow-hint">
        <BoltIcon className="flow-hint-icon" />
        Drag any node — the energy lines are live, not decorative. This traces one real obligation
        (<code className="idcode">{mockObligationEvents[0].event_id}</code>) through risk, the ledger, and its signed receipt.
      </div>
      <div className="flow-canvas" ref={canvasRef} onPointerMove={onPointerMove} onPointerUp={endDrag} onPointerLeave={endDrag}>
        <svg>
          <defs>
            <marker id="arrow" markerWidth="8" markerHeight="8" refX="6" refY="4" orient="auto">
              <path d="M0,0 L8,4 L0,8 Z" fill="var(--accent)" />
            </marker>
          </defs>
          {EDGES.map((edge) => {
            const a = center(positions[edge.from]);
            const b = center(positions[edge.to]);
            const midX = (a.x + b.x) / 2;
            const d = `M ${a.x} ${a.y} C ${midX} ${a.y}, ${midX} ${b.y}, ${b.x} ${b.y}`;
            return (
              <g key={`${edge.from}-${edge.to}`}>
                <path d={d} className={`energy-line ${edge.branch ? 'branch' : ''}`} />
                <path d={d} className={`energy-line energy-line-dash ${edge.branch ? 'branch' : ''}`} />
              </g>
            );
          })}
        </svg>

        {INITIAL_NODES.map((n) => {
          const pos = positions[n.id];
          return (
            <div
              key={n.id}
              className={`flow-node ${dragging === n.id ? 'dragging' : ''}`}
              style={{ left: pos.x, top: pos.y }}
              onPointerDown={onPointerDown(n.id)}
            >
              <div className="flow-node-head">
                <div className="flow-node-icon">
                  <n.icon />
                </div>
              </div>
              <div className="flow-node-kicker">{n.kicker}</div>
              <div className="flow-node-title">{n.title}</div>
              <div className="flow-node-metrics">
                {n.metrics.map((m) => (
                  <div key={m.label}>
                    <div className="flow-node-metric-label">{m.label}</div>
                    <div
                      className="flow-node-metric-value"
                      style={{
                        color:
                          m.tone === 'ok'
                            ? 'var(--ok)'
                            : m.tone === 'warn'
                              ? 'var(--warn)'
                              : m.tone === 'accent'
                                ? 'var(--accent-strong)'
                                : 'var(--text-primary)',
                      }}
                    >
                      {m.value}
                    </div>
                  </div>
                ))}
              </div>
            </div>
          );
        })}
      </div>
      <div className="footnote">
        Node positions are just this session's layout state — refresh to reset. Values shown are pulled from the same
        typed mock records as the table views (or live data once a backend is reachable), not re-invented for this view.
      </div>
    </div>
  );
}
