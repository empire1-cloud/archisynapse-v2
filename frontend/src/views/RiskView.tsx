import { useEffect, useState } from 'react';
import { fetchRiskAssessments, type DataSourceMode } from '../lib/apiClient';
import type { RiskAssessment } from '../types/risk';
import { formatDateTime, normalizedRiskPct, riskBand } from '../lib/format';
import { SourceBadge } from '../components/SourceBadge';
import { RiskMeter, riskPillClass } from '../components/RiskMeter';

const DECISION_PILL: Record<RiskAssessment['fraud_decision'], string> = {
  allow_payout: 'pill-ok',
  hold_payout: 'pill-warn',
  block_payout: 'pill-danger',
};

export function RiskView() {
  const [rows, setRows] = useState<RiskAssessment[]>([]);
  const [source, setSource] = useState<DataSourceMode>('mock');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      const r = await fetchRiskAssessments();
      if (cancelled) return;
      setRows(r.data);
      setSource(r.source);
      setLoading(false);
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  if (loading) return <div className="empty-hint">Loading risk assessments…</div>;

  const avg = rows.length ? rows.reduce((s, r) => s + normalizedRiskPct(r.risk_score), 0) / rows.length : 0;
  const blocked = rows.filter((r) => r.fraud_decision === 'block_payout').length;
  const held = rows.filter((r) => r.fraud_decision === 'hold_payout').length;

  return (
    <div>
      <div className="stat-row">
        <div className="stat-card">
          <div className="stat-label">Avg risk score</div>
          <div className="stat-value">{avg.toFixed(0)}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Held for review</div>
          <div className="stat-value warn">{held}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Blocked</div>
          <div className="stat-value danger">{blocked}</div>
        </div>
        <div className="stat-card">
          <div className="stat-label">Assessed</div>
          <div className="stat-value">{rows.length}</div>
        </div>
      </div>

      <div className="panel">
        <div className="panel-header">
          <div>
            <div className="panel-title">Transaction risk assessments</div>
            <div className="panel-sub">
              POST /risk/royalty (fraud service) — royalty_decision.py maps outcome to allow / hold / block
            </div>
          </div>
          <SourceBadge source={source} />
        </div>
        <table>
          <thead>
            <tr>
              <th>Transaction</th>
              <th>Decision</th>
              <th>Risk</th>
              <th>Reasons</th>
              <th>Evaluated</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const band = riskBand(r.risk_score);
              return (
                <tr key={r.transaction_id}>
                  <td>
                    <code className="idcode">{r.transaction_id}</code>
                  </td>
                  <td>
                    <span className={`pill ${DECISION_PILL[r.fraud_decision]}`}>
                      <span className="pill-dot" />
                      {r.fraud_decision.replace('_', ' ')}
                    </span>
                  </td>
                  <td>
                    <RiskMeter score={r.risk_score} />
                  </td>
                  <td>
                    {r.reasons.length === 0 ? (
                      <span className="text-tertiary">—</span>
                    ) : (
                      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
                        {r.reasons.map((reason) => (
                          <span key={reason} className={`pill ${riskPillClass(band)}`}>
                            {reason}
                          </span>
                        ))}
                      </div>
                    )}
                  </td>
                  <td className="text-tertiary">{formatDateTime(r.evaluated_at)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="footnote">
        If the fraud service is unreachable or errors, the gateway fails to <strong>hold</strong> — never a silent
        allow (royalty_decision.py: <code>fraud_service_error</code> / <code>fraud_service_unavailable</code>, risk
        score 0.5).
      </div>
    </div>
  );
}
