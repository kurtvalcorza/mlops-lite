'use client';

import { useCallback, useEffect, useState } from 'react';
import { PageTitle, Panel } from '@/components/Panel';
import { gwGet, gwPost } from '@/lib/gw';

type Version = { version: string; size_bytes: number; sha256: string; format: string; uri: string };
type Dataset = { name: string; versions: Version[] };
type Manifest = { name: string; version: string; size_bytes: number; sha256: string; format: string };

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

export default function DatasetsPage() {
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
      <PageTitle sub="Upload immutable, content-addressed dataset versions; identical bytes dedupe.">
        datasets
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

// 014 US2 — a dataset version row with an inline "validate" action that renders the readiness report.
function VersionRow({ name, version: v }: { name: string; version: Version }) {
  const [report, setReport] = useState<ValidationReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState('');

  const validate = async () => {
    setBusy(true);
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
      setBusy(false);
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
          <button onClick={validate} disabled={busy} className="hairline rounded-sm px-2 text-ink disabled:opacity-40">
            {busy ? '[~]…' : '[?] validate'}
          </button>
        </span>
      </div>
      {err && <p className="mt-1 st-danger">[x] {err}</p>}
      {report && (
        <div className="mt-1 hairline rounded-sm p-2">
          <p className={report.passed ? 'st-success' : 'st-danger'}>
            [{report.passed ? '✓' : 'x'}] readiness {report.passed ? 'passed' : 'FAILED'} ·{' '}
            {report.stats.row_count} rows
            {report.gate_failures.length > 0 && <span> · gate: {report.gate_failures.join(', ')}</span>}
            {report.warnings.length > 0 && <span className="st-warning"> · warn: {report.warnings.join(', ')}</span>}
          </p>
          <ul className="mt-1 space-y-0.5">
            {report.rules.map((r) => (
              <li key={r.name} className={r.passed ? 'text-ash' : r.disposition === 'gate' ? 'st-danger' : 'st-warning'}>
                [{r.passed ? '✓' : r.disposition === 'gate' ? 'x' : '!'}] {r.name} ({r.disposition})
                {!r.passed && r.detail ? ` — ${r.detail}` : ''}
              </li>
            ))}
          </ul>
        </div>
      )}
    </li>
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
    <Panel title="upload" hint="POST /datasets">
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
            [{result.dedup ? '!' : '✓'}] {result.dedup ? 'identical bytes — deduped to existing version' : 'registered new version'}
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
