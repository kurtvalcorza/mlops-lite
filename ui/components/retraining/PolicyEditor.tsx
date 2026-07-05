'use client';

// 021 T440/T441 (FR-243/244/245, research R6): the per-model policy editor — a structured form and
// an equivalent raw-JSON view over the SAME document, both saving through the same whole-document
// validated PUT. A validation 400 carries structured {errors:[{field, reason}]} rendered inline.
// Auto-promote (promotion_mode = auto-on-green) is OFF by default; enabling it interrupts with the
// shared ConfirmDialog warning that the platform will move @serving without the operator (FR-250).

import { useEffect, useState } from 'react';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { Panel } from '@/components/Panel';
import { GwError, gwGet, gwPut } from '@/lib/gw';

export type PolicyDoc = {
  modality: string;
  monitors: { kind: string; reference?: { name: string; version: string } }[];
  check_interval_s: number;
  on_breach: { action: string; dataset: string; params: { dataset_name?: string } };
  promotion_mode: string;
  enabled: boolean;
};

const MODALITIES = ['llm', 'vision', 'embeddings', 'asr'];
const AUTO_MODE = 'auto-on-green';

type FieldError = { field?: string; reason?: string };

function defaultDoc(): PolicyDoc {
  return {
    modality: 'llm',
    monitors: [{ kind: 'quality' }],
    check_interval_s: 900,
    on_breach: { action: 'retrain', dataset: 'latest', params: {} },
    promotion_mode: 'manual',
    enabled: true,
  };
}

export function PolicyEditor({
  initial,
  onSaved,
}: {
  /** Loads an existing policy into the editor (from the cycle board's edit action). */
  initial?: { model_name: string; doc: PolicyDoc } | null;
  onSaved: () => void;
}) {
  const [model, setModel] = useState(initial?.model_name ?? '');
  const [doc, setDoc] = useState<PolicyDoc>(initial?.doc ?? defaultDoc());
  const [view, setView] = useState<'form' | 'json'>('form');
  const [jsonText, setJsonText] = useState('');
  const [jsonErr, setJsonErr] = useState('');
  const [datasetNames, setDatasetNames] = useState<string[]>([]);
  const [askAuto, setAskAuto] = useState(false);
  const [saving, setSaving] = useState(false);
  const [fieldErrors, setFieldErrors] = useState<FieldError[]>([]);
  const [err, setErr] = useState('');
  const [saved, setSaved] = useState('');

  useEffect(() => {
    gwGet<{ datasets: { name: string }[] }>('datasets')
      .then((d) => setDatasetNames((d.datasets || []).map((x) => x.name)))
      .catch(() => setDatasetNames([]));
  }, []);

  // The board's edit action remounts us via key — but sync anyway if `initial` changes in place.
  useEffect(() => {
    if (initial) {
      setModel(initial.model_name);
      setDoc(initial.doc);
      setView('form');
      setSaved('');
    }
  }, [initial]);

  const monitor = doc.monitors[0] ?? { kind: 'quality' };

  const patch = (p: Partial<PolicyDoc>) => setDoc({ ...doc, ...p });
  const patchMonitor = (m: PolicyDoc['monitors'][0]) => patch({ monitors: [m] });

  const toJson = () => {
    setJsonText(JSON.stringify(doc, null, 2));
    setJsonErr('');
    setView('json');
  };
  const toForm = () => {
    try {
      setDoc(JSON.parse(jsonText) as PolicyDoc);
      setJsonErr('');
      setView('form');
    } catch (e) {
      setJsonErr(`invalid JSON: ${e instanceof Error ? e.message : String(e)}`);
    }
  };

  const requestMode = (mode: string) => {
    if (mode === AUTO_MODE && doc.promotion_mode !== AUTO_MODE) {
      setAskAuto(true); // FR-245: explicit warned opt-in, never a silent switch
      return;
    }
    patch({ promotion_mode: mode });
  };

  const save = async () => {
    setSaving(true);
    setErr('');
    setFieldErrors([]);
    setSaved('');
    // Whole-document PUT — the JSON view IS the wire document (single serialization path, R6).
    let body: PolicyDoc = doc;
    if (view === 'json') {
      try {
        body = JSON.parse(jsonText) as PolicyDoc;
        setDoc(body);
        setJsonErr('');
      } catch (e) {
        setJsonErr(`invalid JSON: ${e instanceof Error ? e.message : String(e)}`);
        setSaving(false);
        return;
      }
    }
    try {
      await gwPut(`policies/${encodeURIComponent(model.trim())}`, body);
      setSaved(`policy for ${model.trim()} saved`);
      onSaved();
    } catch (e) {
      if (e instanceof GwError && e.status === 400) {
        // the contract's structured shape: {errors: [{field, reason}]} — render inline (FR-243)
        const detail = (e.body as { detail?: { errors?: FieldError[] } } | null)?.detail;
        if (detail && Array.isArray(detail.errors)) setFieldErrors(detail.errors);
        else setErr(String(e));
      } else {
        setErr(String(e));
      }
    } finally {
      setSaving(false);
    }
  };

  const errFor = (field: string) => fieldErrors.find((f) => f.field === field)?.reason;

  return (
    <Panel title="declare policy" hint="PUT /policies/{model} — the standing loop, declared">
      <div className="mb-3 flex items-center justify-between">
        <span className="text-caption-md text-mute">one validated document · form and JSON are the same object</span>
        <span className="flex items-center gap-1 text-caption-md">
          <button
            onClick={() => view === 'json' && toForm()}
            className={
              'rounded-sm px-2 py-0.5 ' + (view === 'form' ? 'bg-card text-ink' : 'text-mute hover:bg-soft')
            }
          >
            [{view === 'form' ? '*' : ' '}] form
          </button>
          <button
            onClick={() => view === 'form' && toJson()}
            className={
              'rounded-sm px-2 py-0.5 ' + (view === 'json' ? 'bg-card text-ink' : 'text-mute hover:bg-soft')
            }
          >
            [{view === 'json' ? '*' : ' '}] json
          </button>
        </span>
      </div>

      <Field label="model name" error={errFor('model_name')}>
        <input
          value={model}
          onChange={(e) => setModel(e.target.value)}
          placeholder="e.g. vision-mobilenet"
          className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
        />
      </Field>

      {view === 'form' ? (
        <>
          <div className="grid grid-cols-2 gap-2">
            <Field label="modality (a breach retrains this flow)" error={errFor('modality')}>
              <select
                value={doc.modality}
                onChange={(e) => patch({ modality: e.target.value })}
                className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
              >
                {MODALITIES.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="monitor" error={errFor('monitors')}>
              <select
                value={monitor.kind}
                onChange={(e) =>
                  patchMonitor(
                    e.target.value === 'input_drift'
                      ? { kind: 'input_drift', reference: monitor.reference }
                      : { kind: e.target.value },
                  )
                }
                className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
              >
                <option value="quality">quality</option>
                <option value="input_drift">input_drift</option>
              </select>
            </Field>
          </div>
          {monitor.kind === 'input_drift' && (
            <Field label="reference dataset @ version" error={errFor('monitors[0].reference')}>
              <input
                value={monitor.reference ? `${monitor.reference.name}@${monitor.reference.version}` : ''}
                onChange={(e) => {
                  const [n, v] = e.target.value.split('@');
                  patchMonitor({ kind: 'input_drift', reference: { name: n ?? '', version: v ?? '' } });
                }}
                placeholder="name@version"
                className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
              />
            </Field>
          )}
          <div className="grid grid-cols-2 gap-2">
            <Field label="check interval (seconds ≥ 60)" error={errFor('check_interval_s')}>
              <input
                type="number"
                min={60}
                value={doc.check_interval_s}
                onChange={(e) => patch({ check_interval_s: Number(e.target.value) })}
                className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
              />
            </Field>
            <Field label="retrain dataset (latest @ launch)" error={errFor('on_breach')}>
              <select
                value={doc.on_breach.params.dataset_name ?? ''}
                onChange={(e) =>
                  patch({
                    on_breach: {
                      ...doc.on_breach,
                      params: { ...doc.on_breach.params, dataset_name: e.target.value },
                    },
                  })
                }
                className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
              >
                <option value="">(pick a dataset)</option>
                {datasetNames.map((n) => (
                  <option key={n} value={n}>
                    {n}
                  </option>
                ))}
              </select>
            </Field>
          </div>

          {/* T441: promotion mode — auto-promote is a warned, explicit opt-in (FR-245/250) */}
          <Field label="promotion mode" error={errFor('promotion_mode')}>
            <div className="hairline rounded-sm p-2 text-caption-md">
              {['manual', 'suggest', AUTO_MODE].map((m) => (
                <label key={m} className="mr-4 inline-flex items-center gap-1 text-ink">
                  <input
                    type="radio"
                    name="promotion_mode"
                    checked={doc.promotion_mode === m}
                    onChange={() => requestMode(m)}
                  />
                  {m}
                  {m === AUTO_MODE && <span className="st-warning">[!]</span>}
                </label>
              ))}
              <p className="mt-1 text-ash">
                manual = today&apos;s behaviour · suggest = open a suggestion on green ·{' '}
                <span className="st-warning">auto-on-green moves @serving without you</span> (off by
                default)
              </p>
            </div>
          </Field>

          <label className="mb-3 flex items-center gap-2 text-caption-md text-ink">
            <input
              type="checkbox"
              checked={doc.enabled}
              onChange={(e) => patch({ enabled: e.target.checked })}
            />
            enabled (the scheduler runs this policy)
          </label>
        </>
      ) : (
        <div className="mb-3">
          <textarea
            value={jsonText}
            onChange={(e) => setJsonText(e.target.value)}
            rows={14}
            spellCheck={false}
            className="hairline w-full rounded-sm bg-soft p-3 font-mono text-caption-md text-ink"
          />
          {jsonErr && <p className="mt-1 text-caption-md st-danger">[x] {jsonErr}</p>}
          <p className="mt-1 text-caption-md text-ash">
            [i] this is the exact wire document the validated PUT receives.
          </p>
        </div>
      )}

      <button
        onClick={save}
        disabled={saving || !model.trim()}
        className="rounded-sm bg-ink px-4 py-1 text-button-md text-canvas disabled:opacity-40"
      >
        {saving ? '[~] saving…' : '[+] save policy'}
      </button>
      {saved && <p className="mt-3 text-caption-md st-success">[✓] {saved}</p>}
      {err && <p className="mt-3 whitespace-pre-wrap text-caption-md st-danger">[x] {err}</p>}
      {fieldErrors.length > 0 && (
        <ul className="mt-3 space-y-0.5 text-caption-md st-danger">
          {fieldErrors.map((f, i) => (
            <li key={i}>
              [x] {f.field ?? 'document'}: {f.reason ?? 'invalid'}
            </li>
          ))}
        </ul>
      )}

      <ConfirmDialog
        open={askAuto}
        title="enable auto-promote"
        body={
          <>
            With <span className="text-ink">auto-on-green</span>, a gate-passing retrained candidate
            is promoted to <span className="text-ink">@serving</span>{' '}
            <span className="st-warning">without you</span> — the platform moves the champion on its
            own. Blocked candidates still stay put. You can switch back to{' '}
            <span className="text-ink">suggest</span> at any time.
          </>
        }
        confirmLabel="enable auto-promote"
        onConfirm={() => {
          setAskAuto(false);
          patch({ promotion_mode: AUTO_MODE });
        }}
        onCancel={() => setAskAuto(false)}
      />
    </Panel>
  );
}

function Field({
  label,
  error,
  children,
}: {
  label: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="mb-3">
      <label className="mb-1 block text-caption-md text-mute">{label}</label>
      {children}
      {error && <p className="mt-1 text-caption-md st-danger">[x] {error}</p>}
    </div>
  );
}
