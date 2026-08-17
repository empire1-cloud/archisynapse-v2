import { useEffect, useMemo, useState } from 'react';
import './App.css';
import { BoltIcon, LedgerIcon, ReceiptIcon, ShieldIcon, SplitIcon, OutboxIcon } from './components/icons';
import { FlowView } from './views/FlowView';
import { LedgerView } from './views/LedgerView';
import { RoyaltyView } from './views/RoyaltyView';
import { ReceiptsView } from './views/ReceiptsView';
import { RiskView } from './views/RiskView';
import { OutboxView } from './views/OutboxView';
import { detectDataSources, type DataSourceStatus } from './lib/apiClient';
import { MOCK_TENANT_ID, MOCK_ORG_ID, mockOutboxRows, mockRiskAssessments, mockTransactions } from './lib/mockData';
import { formatMoney, normalizedRiskPct } from './lib/format';

type ViewKey = 'flow' | 'ledger' | 'royalty' | 'receipts' | 'risk' | 'outbox';

const NAV: { key: ViewKey; label: string; icon: typeof LedgerIcon; desc: string; hero?: boolean }[] = [
  { key: 'flow', label: 'Flow', icon: BoltIcon, desc: 'How one obligation actually moves through risk, the ledger, and its signed receipt — drag anything.', hero: true },
  { key: 'ledger', label: 'Ledger', icon: LedgerIcon, desc: 'Double-entry journal entries and the chart of accounts.' },
  { key: 'royalty', label: 'Royalty Splits', icon: SplitIcon, desc: 'Obligation events, owner splits, and payout claims.' },
  { key: 'receipts', label: 'Receipts', icon: ReceiptIcon, desc: 'Signed payout receipts issued by the gateway.' },
  { key: 'risk', label: 'Risk & Fraud', icon: ShieldIcon, desc: 'Fraud-service decisions and risk scores per transaction.' },
  { key: 'outbox', label: 'Outbox', icon: OutboxIcon, desc: 'Delivery status and retry semantics for outbound events.' },
];

const ID_PREFIX_VIEW: Record<string, ViewKey> = {
  evt_: 'royalty',
  txn_: 'ledger',
  rcp_: 'receipts',
};

const KEYWORD_VIEW: Record<string, ViewKey> = {
  flow: 'flow',
  ledger: 'ledger',
  journal: 'ledger',
  royalty: 'royalty',
  splits: 'royalty',
  receipts: 'receipts',
  risk: 'risk',
  fraud: 'risk',
  outbox: 'outbox',
};

function initialView(): ViewKey {
  const param = new URLSearchParams(window.location.search).get('view');
  return NAV.some((n) => n.key === param) ? (param as ViewKey) : 'flow';
}

function App() {
  const [active, setActive] = useState<ViewKey>(initialView);
  const [status, setStatus] = useState<DataSourceStatus>({ ledger: 'mock', gateway: 'mock' });
  const [command, setCommand] = useState('');
  const [feedback, setFeedback] = useState<string | null>(null);
  const [focusId, setFocusId] = useState<string | null>(null);

  useEffect(() => {
    detectDataSources().then(setStatus);
  }, []);

  useEffect(() => {
    if (!feedback) return;
    const t = setTimeout(() => setFeedback(null), 2600);
    return () => clearTimeout(t);
  }, [feedback]);

  const anyMock = status.ledger === 'mock' || status.gateway === 'mock';
  const current = NAV.find((n) => n.key === active)!;

  const metrics = useMemo(() => {
    const posted = mockTransactions.filter((t) => t.status === 'POSTED');
    const volume = posted.reduce((sum, t) => sum + Number(t.amount), 0);
    const avgRisk = Math.round(
      mockRiskAssessments.reduce((s, r) => s + normalizedRiskPct(r.risk_score), 0) / mockRiskAssessments.length,
    );
    const receipted = mockOutboxRows.filter((r) => r.state === 'receipted').length;
    const outboxHealth = Math.round((receipted / mockOutboxRows.length) * 100);
    return { volume, avgRisk, outboxHealth };
  }, []);

  function runCommand() {
    const raw = command.trim();
    if (!raw) return;
    const lower = raw.toLowerCase();

    if (KEYWORD_VIEW[lower]) {
      setActive(KEYWORD_VIEW[lower]);
      setFocusId(null);
      setFeedback(`→ ${NAV.find((n) => n.key === KEYWORD_VIEW[lower])!.label}`);
      setCommand('');
      return;
    }

    const prefix = Object.keys(ID_PREFIX_VIEW).find((p) => lower.startsWith(p));
    if (prefix) {
      setActive(ID_PREFIX_VIEW[prefix]);
      setFocusId(raw);
      setFeedback(`→ jumped to ${raw}`);
      setCommand('');
      return;
    }

    setFeedback(`no match for "${raw}" — try a view name or an evt_/txn_/rcp_ id`);
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">A</div>
          <div className="brand-text">
            <div className="brand-name">Archisynapse</div>
            <div className="brand-tag">Sovereign Ledger</div>
          </div>
        </div>

        <div>
          <div className="nav-section-label">Console</div>
          <nav className="nav">
            {NAV.map((n) => (
              <button
                key={n.key}
                className={`nav-item ${active === n.key ? 'active' : ''} ${n.hero ? 'hero' : ''}`}
                onClick={() => {
                  setActive(n.key);
                  setFocusId(null);
                }}
              >
                <n.icon className="icon" />
                {n.label}
              </button>
            ))}
          </nav>
        </div>

        <div className="sidebar-footer">
          <div className="tenant-chip">
            <div className="tenant-dot" />
            <div className="tenant-chip-text">
              <div className="tenant-chip-name">{MOCK_TENANT_ID}</div>
              <div className="tenant-chip-id">{MOCK_ORG_ID}</div>
            </div>
          </div>
          <span>Archisynapse v1 draft · not for production use</span>
        </div>
      </aside>

      <div style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div className="top-strip">
          <div className="metric-chip pill-wrap">
            <span className="metric-label">Posted volume</span>
            <span className="metric-value">{formatMoney(metrics.volume)}</span>
          </div>
          <div className="metric-chip pill-wrap">
            <span className="metric-label">Avg risk</span>
            <span className="metric-value">{metrics.avgRisk}</span>
          </div>
          <div className="metric-chip pill-wrap">
            <span className="metric-label">Outbox health</span>
            <span className="metric-value">{metrics.outboxHealth}%</span>
          </div>
          <div className="top-strip-spacer" />
          <div className="mode-toggle">
            <button className="mode-btn" onClick={() => setActive('flow')}>
              <BoltIcon className="mode-btn-icon" />
              Flow
            </button>
            <button className={`mode-btn ${status.ledger === 'live' ? 'primary' : ''}`}>
              {status.ledger === 'live' ? 'Ledger: live' : 'Ledger: mock'}
            </button>
          </div>
        </div>

        <main className="main">
          {anyMock && (
            <div className="mock-banner">
              <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8">
                <path d="M12 9v4M12 17h.01" />
                <path d="M10.3 3.9L2.6 18a1.8 1.8 0 0 0 1.6 2.7h15.6a1.8 1.8 0 0 0 1.6-2.7L13.7 3.9a1.8 1.8 0 0 0-3.4 0z" />
              </svg>
              <span>
                <strong>Mock data</strong> — no reachable backend was found at the configured service URLs
                (VITE_LEDGER_SERVICE_URL / VITE_GATEWAY_URL). Every shape below matches the real API exactly; nothing
                here is a live transaction.
              </span>
            </div>
          )}

          <div className="view-header">
            <div>
              <h1 className="view-title">{current.label}</h1>
              <p className="view-desc">{current.desc}</p>
            </div>
          </div>

          {active === 'flow' && <FlowView />}
          {active === 'ledger' && <LedgerView focusId={focusId} />}
          {active === 'royalty' && <RoyaltyView focusId={focusId} />}
          {active === 'receipts' && <ReceiptsView focusId={focusId} />}
          {active === 'risk' && <RiskView />}
          {active === 'outbox' && <OutboxView />}

          <div className="command-bar">
            <span className="command-bar-prompt">›</span>
            <input
              value={command}
              onChange={(e) => setCommand(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && runCommand()}
              placeholder="jump to a view (ledger, risk, outbox…) or an id (evt_, txn_, rcp_…)"
            />
            {feedback && <span className="command-bar-feedback">{feedback}</span>}
            <span className="command-bar-hint">real navigation — not a chat AI</span>
            <button className="command-bar-btn" onClick={runCommand}>
              EXECUTE
            </button>
          </div>
        </main>
      </div>
    </div>
  );
}

export default App;
