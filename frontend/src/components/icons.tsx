type IconProps = { className?: string };

const base = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.6,
  strokeLinecap: 'round' as const,
  strokeLinejoin: 'round' as const,
  viewBox: '0 0 24 24',
};

export function LedgerIcon({ className }: IconProps) {
  return (
    <svg className={className} {...base}>
      <path d="M5 3h11l3 3v15H5z" />
      <path d="M9 9h7M9 13h7M9 17h4" />
    </svg>
  );
}

export function SplitIcon({ className }: IconProps) {
  return (
    <svg className={className} {...base}>
      <path d="M6 3v9a3 3 0 0 0 3 3h9" />
      <path d="M14 12l4 3-4 3" />
      <circle cx="6" cy="3" r="0" />
      <path d="M6 3v0" />
    </svg>
  );
}

export function ReceiptIcon({ className }: IconProps) {
  return (
    <svg className={className} {...base}>
      <path d="M6 2h12v20l-3-2-3 2-3-2-3 2z" />
      <path d="M9 7h6M9 11h6" />
    </svg>
  );
}

export function ShieldIcon({ className }: IconProps) {
  return (
    <svg className={className} {...base}>
      <path d="M12 3l7 3v6c0 4.5-3 7.5-7 9-4-1.5-7-4.5-7-9V6z" />
      <path d="M9.5 12l2 2 3.5-3.5" />
    </svg>
  );
}

export function OutboxIcon({ className }: IconProps) {
  return (
    <svg className={className} {...base}>
      <path d="M4 13V6a1 1 0 0 1 1-1h5l2 2h7a1 1 0 0 1 1 1v5" />
      <path d="M4 13l3.5 5h9L20 13" />
      <path d="M4 13h5.2a1 1 0 0 1 .9.55l.8 1.6a1 1 0 0 0 .9.55h.4a1 1 0 0 0 .9-.55l.8-1.6a1 1 0 0 1 .9-.55H20" />
    </svg>
  );
}
