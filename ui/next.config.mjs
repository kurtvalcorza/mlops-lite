/** @type {import('next').NextConfig} */

// Grafana origin allowed ONLY for the Health-tab iframe (frame-src). Build-time env, default :3001.
const GRAFANA_ORIGIN = process.env.NEXT_PUBLIC_GRAFANA_URL ?? 'http://localhost:3001';

// 004 US1 (FR-035): pragmatic CSP for a localhost single-operator console. 'unsafe-inline' is allowed
// for scripts/styles (Next hydration + Tailwind) — no nonce plumbing — while the high-value controls
// stay strict: only same-origin connect/script, the console is NOT framable (frame-ancestors 'none'),
// and the only foreign frame allowed is Grafana on the Health tab.
const CSP = [
  "default-src 'self'",
  "script-src 'self' 'unsafe-inline'",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  "connect-src 'self'",
  `frame-src ${GRAFANA_ORIGIN}`,
  "frame-ancestors 'none'",
  "base-uri 'self'",
  "form-action 'self'",
].join('; ');

const SECURITY_HEADERS = [
  { key: 'Content-Security-Policy', value: CSP },
  { key: 'X-Content-Type-Options', value: 'nosniff' },
  { key: 'X-Frame-Options', value: 'DENY' }, // legacy belt-and-suspenders for frame-ancestors
  { key: 'Referrer-Policy', value: 'no-referrer' },
];

const nextConfig = {
  reactStrictMode: true,
  // Runs natively in WSL via `next start` (not a container).
  outputFileTracingRoot: import.meta.dirname,
  async headers() {
    return [{ source: '/:path*', headers: SECURITY_HEADERS }];
  },
};

export default nextConfig;
