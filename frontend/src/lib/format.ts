export function formatMoney(amount: string | number, currency = 'USD'): string {
  const n = typeof amount === 'string' ? Number(amount) : amount;
  if (Number.isNaN(n)) return amount.toString();
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency,
    minimumFractionDigits: 2,
    maximumFractionDigits: 4,
  }).format(n);
}

export function formatBps(bps: number): string {
  return `${(bps / 100).toFixed(2)}%`;
}

export function formatDateTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return new Intl.DateTimeFormat('en-US', {
    month: 'short',
    day: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(d);
}

export function formatRelative(iso: string, nowIso = '2026-08-16T02:00:00Z'): string {
  const then = new Date(iso).getTime();
  const now = new Date(nowIso).getTime();
  const diffMs = now - then;
  const diffMin = Math.round(diffMs / 60000);
  if (Math.abs(diffMin) < 1) return 'just now';
  if (diffMin > 0) {
    if (diffMin < 60) return `${diffMin}m ago`;
    const h = Math.round(diffMin / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.round(h / 24)}d ago`;
  }
  const future = Math.abs(diffMin);
  if (future < 60) return `in ${future}m`;
  return `in ${Math.round(future / 60)}h`;
}

export function truncateId(id: string, keep = 6): string {
  if (id.length <= keep * 2 + 3) return id;
  return `${id.slice(0, keep)}…${id.slice(-4)}`;
}

export function riskBand(score: number): 'low' | 'medium' | 'high' {
  // Accepts either 0-1 (receipt decision.risk_score) or 0-100 (fraud service)
  const normalized = score > 1 ? score / 100 : score;
  if (normalized < 0.3) return 'low';
  if (normalized < 0.65) return 'medium';
  return 'high';
}

export function normalizedRiskPct(score: number): number {
  const normalized = score > 1 ? score / 100 : score;
  return Math.round(normalized * 100);
}
