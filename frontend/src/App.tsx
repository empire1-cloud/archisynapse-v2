import { useEffect, useState } from 'react';
import './App.css';
import { LedgerIcon, ReceiptIcon, ShieldIcon, SplitIcon, OutboxIcon } from './components/icons';
import { LedgerView } from './views/LedgerView';
import { RoyaltyView } from './views/RoyaltyView';
import { ReceiptsView } from './views/ReceiptsView';
import { RiskView } from './views/RiskView';
import { OutboxView } from './views/OutboxView';
import { detectDataSources, type DataSourceStatus } from './lib/apiClient';
import { MOCK_TENANT_ID, MOCK_ORG_ID } from './lib/mockData';

type ViewKey = 'ledger' | 'royalty' | 'receipts' | 'risk' | 'outbox';

const NAV: { key: ViewKey; label: string; icon: typeof LedgerIcon; desc: string }[] = [
  { key: 'ledger', label: 'Ledger', icon: LedgerIcon, desc: 'Double-entry journal entries and the chart of accounts.' },
  { key: 'royalty', label: 'Royalty Splits', icon: SplitIcon, desc: 'Obligation events, owner splits, and payout claims.' },
  { key: 'receipts', label: 'Receipts', icon: ReceiptIcon, desc: 'Signed payout receipts issued by the gateway.' },
  { key: 'risk', label: 'Risk & Fraud', icon: ShieldIcon, desc: 'Fraud-service decisions and risk scores per transaction.' },
  { key: 'outbox', label: 'Outbox', icon: OutboxIcon, desc: 'Delivery status and retry semantics for outbound events.' },
];

function initialView(): ViewKey {
  const param = new URLSearchParams(window.location.search).get('view');
  return NAV.some((n) => n.key === param) ? (param as ViewKey) : 'ledger';
}

function App() {
  const [active, setActive] = useState<ViewKey>(initialView);
  const [status, setStatus] = useState<DataSourceStatus>({ ledger: 'mock', gateway: 'mock' });

  useEffect(() => {
    detectDataSources().then(setStatus);
  }, []);

  const anyMock = status.ledger === 'mock' || status.gateway === 'mock';
  const current = NAV.find((n) => n.key === active)!;

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark">A</div>
          <div className="brand-text">
            <div className="brand-name">Archisynapse</div>
            <div className="brand-tag">Ledger Console</div>
          </div>
        </div>

        <div>
          <div className="nav-section-label">Console</div>
          <nav className="nav">
            {NAV.map((n) => (
              <button
                key={n.key}
                className={`nav-item ${active === n.key ? 'active' : ''}`}
                onClick={() => setActive(n.key)}
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

        {active === 'ledger' && <LedgerView />}
        {active === 'royalty' && <RoyaltyView />}
        {active === 'receipts' && <ReceiptsView />}
        {active === 'risk' && <RiskView />}
        {active === 'outbox' && <OutboxView />}
      </main>
    </div>
  );
}

export default App;
