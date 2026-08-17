import type { DataSourceMode } from '../lib/apiClient';

export function SourceBadge({ source }: { source: DataSourceMode }) {
  if (source === 'live') {
    return (
      <span className="badge badge-live" title="Served from a reachable backend service">
        <span className="badge-dot" />
        Live
      </span>
    );
  }
  return (
    <span className="badge badge-mock" title="Typed mock data — shape-matched to the real API, not a live call">
      <span className="badge-dot" />
      Mock data
    </span>
  );
}
