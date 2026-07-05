'use client';

import { useEffect, useRef, useState } from 'react';

// 021 (FR-250, research R5): the single high-trust friction primitive. Backs all three
// consequence-carrying actions — promote override (typed reason required), preemptive swap (names
// the holder to evict), and enabling auto-promote (warning copy). Each caller supplies its own
// title/body/confirm label; `requireReason` blocks confirm until a non-empty reason is entered.
export function ConfirmDialog({
  open,
  title,
  body,
  confirmLabel = 'confirm',
  tone = 'warning',
  requireReason = false,
  reasonLabel = 'reason (required)',
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: string;
  body: React.ReactNode;
  confirmLabel?: string;
  tone?: 'warning' | 'danger';
  requireReason?: boolean;
  reasonLabel?: string;
  onConfirm: (reason: string) => void;
  onCancel: () => void;
}) {
  const [reason, setReason] = useState('');
  const boxRef = useRef<HTMLDivElement | null>(null);

  // Reset the captured reason whenever the dialog opens fresh.
  useEffect(() => {
    if (open) setReason('');
  }, [open]);

  // Esc cancels — a high-trust dialog must always have a cheap way out.
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCancel();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, onCancel]);

  if (!open) return null;

  const blocked = requireReason && !reason.trim();

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-ink/40 p-4"
      onMouseDown={(e) => {
        // click outside the box cancels
        if (boxRef.current && !boxRef.current.contains(e.target as Node)) onCancel();
      }}
      role="dialog"
      aria-modal="true"
      aria-label={title}
    >
      <div ref={boxRef} className="hairline w-full max-w-md rounded-none bg-canvas">
        <div className="hairline flex items-baseline gap-2 border-x-0 border-t-0 px-4 py-2">
          <span className={tone === 'danger' ? 'st-danger' : 'st-warning'}>[!]</span>
          <h2 className="text-heading-md text-ink">{title}</h2>
        </div>
        <div className="p-4">
          <div className="text-body-md text-mute">{body}</div>
          {requireReason && (
            <div className="mt-3">
              <label className="mb-1 block text-caption-md text-mute">{reasonLabel}</label>
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                autoFocus
                placeholder="why this action is justified"
                className="hairline w-full rounded-sm bg-soft px-2 py-1 text-body-md text-ink placeholder:text-ash"
              />
            </div>
          )}
          <div className="mt-4 flex justify-end gap-2">
            <button
              onClick={onCancel}
              className="hairline rounded-sm px-4 py-1 text-button-md text-mute"
            >
              cancel
            </button>
            <button
              onClick={() => onConfirm(reason.trim())}
              disabled={blocked}
              title={blocked ? 'a reason is required' : undefined}
              className={
                'rounded-sm px-4 py-1 text-button-md text-canvas disabled:opacity-40 ' +
                (tone === 'danger' ? 'bg-danger' : 'bg-ink')
              }
            >
              {confirmLabel}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
