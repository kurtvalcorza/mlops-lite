'use client';

// 021 T438 (FR-240/242/248): the ONE-SHOT "retrain if this breaches" arm, shared by the drift and
// quality panels. Labeled distinct from standing policies on purpose: this fires once, for THIS
// check only — the standing, scheduled counterpart lives in the retraining stage (same checks,
// same gate, same shared OR+cooldown). The spec is auto-filled from the checked model:
// dataset_version pins to "latest" (resolved at launch), knobs fall to the flow defaults.

import Link from 'next/link';

export type RetrainDraft = {
  dataset_name: string;
  output_name: string;
  modality: string;
};

export const RETRAIN_MODALITIES = ['llm', 'vision', 'embeddings', 'asr'];

/** The wire shape the check endpoints expect under `retrain` (RetrainSpec). */
export function buildRetrainSpec(draft: RetrainDraft): Record<string, unknown> {
  return {
    dataset_name: draft.dataset_name,
    dataset_version: 'latest', // resolved at launch time (FR-181) — consumes the drifted data
    output_name: draft.output_name,
    modality: draft.modality,
    // steps/lora_r deliberately omitted → the flow's conservative defaults apply
  };
}

export function OneShotRetrain({
  datasetNames,
  armed,
  onArm,
  draft,
  onChange,
}: {
  datasetNames: string[];
  armed: boolean;
  onArm: (a: boolean) => void;
  draft: RetrainDraft;
  onChange: (d: RetrainDraft) => void;
}) {
  return (
    <div className="mt-1 mb-3 hairline rounded-sm p-2">
      <label className="flex items-center gap-2 text-caption-md text-ink">
        <input type="checkbox" checked={armed} onChange={(e) => onArm(e.target.checked)} />
        one-shot: retrain if this check breaches
      </label>
      <p className="mt-1 text-caption-md text-ash">
        [i] fires once, for this check only — the <em>standing</em> counterpart is a policy in{' '}
        <Link href="/retraining" className="underline">
          retraining
        </Link>{' '}
        (same check, same gate, same cooldown).
      </p>
      {armed && (
        <div className="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3">
          <div>
            <label className="mb-1 block text-caption-md text-mute">retrain dataset (latest @ launch)</label>
            <select
              value={draft.dataset_name}
              onChange={(e) => onChange({ ...draft, dataset_name: e.target.value })}
              className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
            >
              <option value="">(pick a dataset)</option>
              {datasetNames.map((n) => (
                <option key={n} value={n}>
                  {n}
                </option>
              ))}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-caption-md text-mute">output name</label>
            <input
              value={draft.output_name}
              onChange={(e) => onChange({ ...draft, output_name: e.target.value })}
              className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
            />
          </div>
          <div>
            <label className="mb-1 block text-caption-md text-mute">modality (knobs defaulted)</label>
            <select
              value={draft.modality}
              onChange={(e) => onChange({ ...draft, modality: e.target.value })}
              className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink"
            >
              {RETRAIN_MODALITIES.map((m) => (
                <option key={m} value={m}>
                  {m}
                </option>
              ))}
            </select>
          </div>
        </div>
      )}
    </div>
  );
}

/** Renders the check's retrain outcome — `skipped: cooldown` is a FIRST-CLASS outcome (FR-242),
 *  not an error: the shared debounce means a retrain fired recently across either breach signal. */
export function RetrainOutcome({
  retrain,
}: {
  retrain: { run_id?: string; error?: string; skipped?: string } | null | undefined;
}) {
  if (!retrain) return null;
  if (retrain.error) {
    return <p className="text-caption-md st-danger">[x] retrain failed: {retrain.error}</p>;
  }
  if (retrain.skipped) {
    return (
      <p className="text-caption-md st-warning">
        [~] retrain skipped: <span className="text-ink">{retrain.skipped}</span> — a retrain fired
        recently (shared OR+cooldown debounce). Expected behaviour, not an error; the next genuine
        breach after the window can fire.
      </p>
    );
  }
  return (
    <p className="text-caption-md st-accent">
      [→] retrain launched: {retrain.run_id} —{' '}
      <Link href="/training" className="underline">
        watch in training
      </Link>
    </p>
  );
}
