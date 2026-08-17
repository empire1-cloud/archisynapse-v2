import { useEffect, useMemo, useState } from 'react';
import { fetchAccounts, fetchTransactions, type DataSourceMode } from '../lib/apiClient';
import type { Account, Transaction } from '../types/ledger';
import { formatDateTime, formatMoney, truncateId } from '../lib/format';
import { SourceBadge } from '../components/SourceBadge';

const STATUS_PILL: Record<Transaction['status'], string> = {
  POSTED: 'pill-ok',
  PENDING: 'pill-warn',
  FAILED: 'pill-danger',
  REVERSED: 'pill-neutral',
};

export function LedgerView() {
  const [accounts, setAccounts] = useState<Account[]>([]);
  const [accountsSource, setAccountsSource] = useState<DataSourceMode>('mock');
  const [txns, setTxns] = useState<Transaction[]>([]);
  const [txnSource, setTxnSource] = useState<DataSourceMode>('mock');
  const [selected, setSelected] = useState<Transaction | null>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const [a, t] = await Promise.all([fetchAccounts(), fetchTransactions()]);
      if (cancelled) return;
      setAccounts(a.data);
      setAccountsSource(a.source);
      setTxns(t.data);
      setTxnSource(t.source);
      setSelected(t.data[0] ?? null);
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const accountName = useMemo(() => {
    const map = new Map(accounts.map((a) => [a.id, a]));
    return (id: string) => map.get(id)?.name ?? id;
  }, [accounts]);

  const totals = useMemo(() => {
    const posted = txns.filter((t) => t.status === 'POSTED');
    const volume = posted.reduce((sum, t) => sum + Number(t.amount), 0);
    const pending = txns.filter((t) => t.status === 'PENDING').length;
    const balanced = txns.every((t) => {
      const debit = t.entries.filter((e) => e.debitCredit === 'DEBIT').reduce((s, e) => s + Number(e.amount), 0);
      const credit = t.entries.filter((e) => e.debitCredit === 'CREDIT').reduce((s, e) => s + Number(e.amount), 0);
      return Math.abs(debit - credit) < 0.0001;
    });
    return { volume, pending, count: txns.length, balanced };
  }, [txns]);

  if (loading) return <div className="empty-hint">Loading ledger…</div>;

  return (
    <div>
      <div className="stat-row">
        <div className="stat-card">
          <div className="stat-label">Posted volume</div>
          <div className="stat-value accent">{formatMoney(totals.volume)}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Transactions</div>
          <div className="stat-value">{totals.count}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Pending</div>
          <div className="stat-value warn">{totals.pending}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Double-entry integrity</div>
          <div className={`stat-value ${totals.balanced ? 'ok' : 'danger'}`}>
            {totals.balanced ? 'Balanced' : 'Discrepancy'}
          </div>
        </div>
      </div>

      <div className="split-layout">
        <div>
          <div className="panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">Journal — transactions</div>
                <div className="panel-sub">Double-entry postings, newest first</div>
              </div>
              <SourceBadge source={txnSource} />
            </div>
            <table>
              <thead>
                <tr>
                  <th>Transaction</th>
                  <th>Type</th>
                  <th>Description</th>
                  <th>Status</th>
                  <th style={{ textAlign: 'right' }}>Amount</th>
                </tr>
              </thead>
              <tbody>
                {txns.map((t) => (
                  <tr
                    key={t.id}
                    className="clickable"
                    onClick={() => setSelected(t)}
                    style={selected?.id === t.id ? { background: 'var(--bg-surface-raised)' } : undefined}
                  >
                    <td>
                      <code className="idcode">{truncateId(t.id)}</code>
                    </td>
                    <td className="text-secondary">{t.type}</td>
                    <td>{t.description}</td>
                    <td>
                      <span className={`pill ${STATUS_PILL[t.status]}`}>
                        <span className="pill-dot" />
                        {t.status}
                      </span>
                    </td>
                    <td className="td-num">{formatMoney(t.amount, t.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="panel">
            <div className="panel-header">
              <div>
                <div className="panel-title">Chart of accounts</div>
                <div className="panel-sub">Balances denormalized for query speed</div>
              </div>
              <SourceBadge source={accountsSource} />
            </div>
            <table>
              <thead>
                <tr>
                  <th>Code</th>
                  <th>Account</th>
                  <th>Type</th>
                  <th style={{ textAlign: 'right' }}>Balance</th>
                </tr>
              </thead>
              <tbody>
                {accounts.map((a) => (
                  <tr key={a.id}>
                    <td className="text-tertiary num">{a.code}</td>
                    <td>{a.name}</td>
                    <td className="text-secondary">{a.type}</td>
                    <td className="td-num">{formatMoney(a.balance, a.currency)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>

        <div className="detail-panel">
          {selected ? (
            <div className="panel">
              <div className="panel-header">
                <div>
                  <div className="panel-title">Entry detail</div>
                  <div className="panel-sub">
                    <code className="idcode">{selected.id}</code>
                  </div>
                </div>
                <span className={`pill ${STATUS_PILL[selected.status]}`}>
                  <span className="pill-dot" />
                  {selected.status}
                </span>
              </div>
              <div className="kv-list">
                <div className="kv-row">
                  <span className="kv-key">Reference</span>
                  <span className="kv-val">{truncateId(selected.referenceId ?? '—')}</span>
                </div>
                <div className="kv-row">
                  <span className="kv-key">Idempotency key</span>
                  <span className="kv-val">{selected.idempotencyKey ?? '—'}</span>
                </div>
                <div className="kv-row">
                  <span className="kv-key">Posted at</span>
                  <span className="kv-val">{formatDateTime(selected.postedAt)}</span>
                </div>
              </div>
              <div className="section-label" style={{ padding: '0 16px' }}>
                Journal entries
              </div>
              <table>
                <thead>
                  <tr>
                    <th>Account</th>
                    <th>D/C</th>
                    <th style={{ textAlign: 'right' }}>Amount</th>
                  </tr>
                </thead>
                <tbody>
                  {selected.entries.map((e) => (
                    <tr key={e.id}>
                      <td>{accountName(e.accountId)}</td>
                      <td>
                        <span className={`pill ${e.debitCredit === 'DEBIT' ? 'pill-neutral' : 'pill-accent'}`}>
                          {e.debitCredit}
                        </span>
                      </td>
                      <td className="td-num">{formatMoney(e.amount, selected.currency)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
              <div className="footnote" style={{ padding: '0 16px 14px' }}>
                Entries are immutable — corrections post as reversing entries, never in-place edits.
              </div>
            </div>
          ) : (
            <div className="panel">
              <div className="empty-hint">Select a transaction to inspect its entries.</div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
