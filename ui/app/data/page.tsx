'use client';

import { useCallback, useEffect, useState } from 'react';
import Link from 'next/link';
import { PageTitle, Panel } from '@/components/Panel';
import { gwGet, gwPost } from '@/lib/gw';

type Version = { version: string; size_bytes: number; sha256: string; format: string; uri: string };
type Dataset = { name: string; versions: Version[] };
type Manifest = { name: string; version: string; size_bytes: number; sha256: string; format: string };
// Full version manifest (021 FR-215): `download_url` exists on the wire but is presigned against
// the INTERNAL store endpoint — not browser-reachable — so this stage is inspect-only (no byte
// download button; deferred until a public-presign knob exists).
type VersionDetail = {
  name: string;
  version: string;
  size_bytes: number;
  sha256: string;
  format: string;
  uri?: string;
  created_at?: number | string;
  download_url?: string;
};

// 014 US2 — the hand-rolled dataset-validation report.
type Rule = {
  name: string;
  passed: boolean;
  disposition: 'gate' | 'warn';
  value: unknown;
  threshold: unknown;
  detail: string;
};
type ValidationReport = {
  passed: boolean;
  rules: Rule[];
  stats: { row_count: number; columns: string[] };
  gate_failures: string[];
  warnings: string[];
};

// 021 T449 (FR-214..218): the data stage — the loop's entry. Register/list/dedupe (unchanged),
// version INSPECT (full manifest — inspect-only, FR-215), validate as a pre-train readiness GATE
// (gate vs warn dispositions), and the "train on this version" hand-off → /training?ds=… (T450,
// R7). Deliberately absent: edit/delete (immutability is the point) and EDA (FR-218).
export default function DataPage() {
  const [datasets, setDatasets] = useState<Dataset[]>([]);
  const [err, setErr] = useState('');
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const d = await gwGet<{ datasets: Dataset[] }>('datasets');
      setDatasets(d.datasets || []);
      setErr('');
    } catch (e) {
      setErr(String(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <>
      <PageTitle sub="Immutable, content-addressed versions — the loop's entry. Identical bytes dedupe.">
        data
      </PageTitle>

      <div className="grid gap-6 lg:grid-cols-[1fr_1.3fr]">
        <UploadForm onDone={load} />
        <Panel title="registered" hint="GET /datasets">
          {err && <p className="text-caption-md st-danger">[x] {err}</p>}
          {loading && <p className="text-caption-md text-mute">[~] loading…</p>}
          <div className="space-y-4">
            {datasets.map((d) => (
              <div key={d.name}>
                <p className="text-body-strong text-ink">{d.name}</p>
                <ul className="mt-1 space-y-1">
                  {d.versions.map((v) => (
                    <VersionRow key={v.version} name={d.name} version={v} />
                  ))}
                </ul>
              </div>
            ))}
            {!loading && datasets.length === 0 && (
              <p className="text-body-md text-mute">[ ] no datasets registered.</p>
            )}
          </div>
        </Panel>
      </div>
    </>
  );
}

// One dataset version row: inspect (manifest), validate (readiness gate), train-on-this-version.
function VersionRow({ name, version: v }: { name: string; version: Version }) {
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [detail, setDetail] = useState<VersionDetail | null>(null);
  const [busy, setBusy] = useState<'validate' | 'inspect' | ''>('');
  const [err, setErr] = useState('');

  const validate = async () => {
    setBusy('validate');
    setErr('');
    try {
      setReport(
        await gwPost<ValidationReport>(
          `datasets/${encodeURIComponent(name)}/${encodeURIComponent(v.version)}/validate`,
          {},
        ),
      );
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy('');
    }
  };

  // 021 (FR-215): full-manifest inspect — closes/opens like a disclosure.
  const inspect = async () => {
    if (detail) {
      setDetail(null);
      return;
    }
    setBusy('inspect');
    setErr('');
    try {
      setDetail(
        await gwGet<VersionDetail>(
          `datasets/${encodeURIComponent(name)}/${encodeURIComponent(v.version)}`,
        ),
      );
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy('');
    }
  };

  return (
    <li className="text-caption-md">
      <div className="flex items-baseline justify-between gap-3">
        <span className="text-ink">
          <span className="st-mute">[+]</span> {v.version}
          <span className="ml-2 text-ash">{v.format}</span>
        </span>
        <span className="flex items-center gap-3 text-mute">
          <span>
            {v.size_bytes} B · {v.sha256.slice(0, 12)}…
          </span>
          <button
            onClick={inspect}
            disabled={busy !== ''}
            className="hairline rounded-sm px-2 text-ink disabled:opacity-40"
          >
            {busy === 'inspect' ? '[~]…' : detail ? '[−] manifest' : '[i] inspect'}
          </button>
          <button
            onClick={validate}
            disabled={busy !== ''}
            className="hairline rounded-sm px-2 text-ink disabled:opacity-40"
          >
            {busy === 'validate' ? '[~]…' : '[?] validate'}
          </button>
          {/* T450 (FR-217): the data → training hand-off, context in the URL (R7) */}
          <Link
            href={`/training?ds=${encodeURIComponent(`${name}@${v.version}`)}`}
            className="hairline rounded-sm px-2 st-accent"
            title="open training with this pinned version prefilled"
          >
            [→] train on this
          </Link>
        </span>
      </div>
      {err && <p className="mt-1 st-danger">[x] {err}</p>}
      {detail && (
        <div className="mt-1 hairline rounded-sm p-2">
          <p className="text-ink">manifest — {detail.name} @ {detail.version}</p>
          <dl className="mt-1 space-y-0.5 text-mute">
            <ManifestRow k="sha256">{detail.sha256}</ManifestRow>
            <ManifestRow k="size">{detail.size_bytes} B</ManifestRow>
            <ManifestRow k="format">{detail.format}</ManifestRow>
            {detail.uri && <ManifestRow k="store uri">{detail.uri}</ManifestRow>}
            {detail.created_at != null && (
              <ManifestRow k="created">{String(detail.created_at)}</ManifestRow>
            )}
          </dl>
          <p className="mt-1 text-ash">
            [i] inspect-only — the pinned bytes live in the internal store (byte download deferred;
            the presigned URL is not browser-reachable).
          </p>
        </div>
      )}
      {report && (
        <div className="mt-1 hairline rounded-sm p-2">
          <p className={report.passed ? 'st-success' : 'st-danger'}>
            [{report.passed ? '✓' : 'x'}] readiness {report.passed ? 'passed' : 'FAILED'} ·{' '}
            {report.stats.row_count} rows
            {report.gate_failures.length > 0 && <span> · gate: {report.gate_failures.join(', ')}</span>}
            {report.warnings.length > 0 && (
              <span className="st-warning"> · warn: {report.warnings.join(', ')}</span>
            )}
          </p>
          <ul className="mt-1 space-y-0.5">
            {report.rules.map((r) => (
              <li
                key={r.name}
                className={r.passed ? 'text-ash' : r.disposition === 'gate' ? 'st-danger' : 'st-warning'}
              >
                [{r.passed ? '✓' : r.disposition === 'gate' ? 'x' : '!'}] {r.name} ({r.disposition})
                {!r.passed && r.detail ? ` — ${r.detail}` : ''}
              </li>
            ))}
          </ul>
          <p className="mt-1 text-ash">
            [i] gate rules block a train on this version; warn rules ship with a caution.
          </p>
        </div>
      )}
    </li>
  );
}

function ManifestRow({ k, children }: { k: string; children: React.ReactNode }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-ash">{k}</dt>
      <dd className="break-all text-right text-mute">{children}</dd>
    </div>
  );
}

function UploadForm({ onDone }: { onDone: () => void }) {
  const [name, setName] = useState('');
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');
  const [result, setResult] = useState<{ manifest: Manifest; dedup: boolean } | null>(null);

  const submit = async () => {
    if (!name.trim() || !file) return;
    setBusy(true);
    setErr('');
    setResult(null);
    try {
      const content_b64 = await toBase64(file);
      const fmt = file.name.includes('.') ? file.name.split('.').pop() : undefined;
      // Snapshot existing versions to detect idempotent dedupe (same bytes → same version).
      let before: string[] = [];
      try {
        const d = await gwGet<{ name: string; versions: Version[] }>(
          `datasets/${encodeURIComponent(name)}`,
        );
        before = d.versions.map((v) => v.version);
      } catch {
        /* new dataset — no prior versions */
      }
      const manifest = await gwPost<Manifest>('datasets', { name, content_b64, format: fmt });
      setResult({ manifest, dedup: before.includes(manifest.version) });
      onDone();
    } catch (e) {
      setErr(String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Panel title="register" hint="POST /datasets">
      <label className="mb-2 block text-caption-md text-mute">name</label>
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="dataset-name"
        className="hairline mb-3 w-full rounded-sm bg-soft p-2 text-body-md text-ink placeholder:text-ash"
      />
      <label className="mb-2 block text-caption-md text-mute">file</label>
      <input
        type="file"
        onChange={(e) => setFile(e.target.files?.[0] ?? null)}
        className="mb-3 w-full text-caption-md text-mute file:hairline file:mr-3 file:rounded-sm file:bg-card file:px-3 file:py-1 file:text-ink"
      />
      <button
        onClick={submit}
        disabled={busy || !name.trim() || !file}
        className="rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
      >
        {busy ? '[~] uploading…' : '[+] register'}
      </button>

      {err && <p className="mt-3 text-caption-md st-danger">[x] {err}</p>}
      {result && (
        <div className="mt-3 text-caption-md">
          <p className={result.dedup ? 'st-warning' : 'st-success'}>
            [{result.dedup ? '!' : '✓'}]{' '}
            {result.dedup ? 'identical bytes — deduped to existing version' : 'registered new version'}
          </p>
          <p className="mt-1 text-mute">
            v{result.manifest.version} · {result.manifest.size_bytes} B ·{' '}
            {result.manifest.sha256.slice(0, 16)}…
          </p>
        </div>
      )}
    </Panel>
  );
}

function toBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => {
      const s = String(reader.result);
      resolve(s.slice(s.indexOf(',') + 1));
    };
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}
