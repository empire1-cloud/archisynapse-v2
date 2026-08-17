import { normalizedRiskPct, riskBand } from '../lib/format';

export function RiskMeter({ score }: { score: number }) {
  const pct = normalizedRiskPct(score);
  const band = riskBand(score);
  return (
    <div className="risk-meter">
      <div className="risk-track">
        <div className={`risk-fill ${band}`} style={{ width: `${pct}%` }} />
      </div>
      <span className={`risk-score-num num`}>{pct}</span>
    </div>
  );
}

export function riskPillClass(band: 'low' | 'medium' | 'high') {
  if (band === 'low') return 'pill-ok';
  if (band === 'medium') return 'pill-warn';
  return 'pill-danger';
}
