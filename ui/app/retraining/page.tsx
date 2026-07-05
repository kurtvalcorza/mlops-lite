'use client';

import { useRef, useState } from 'react';
import Link from 'next/link';
import { PageTitle } from '@/components/Panel';
import { CycleBoard, type CycleBoardHandle } from '@/components/retraining/CycleBoard';
import { PolicyEditor, type PolicyDoc } from '@/components/retraining/PolicyEditor';
import { SuggestionsInbox } from '@/components/retraining/SuggestionsInbox';

// 021 T444 (FR-243..248): the retraining stage — the previously-invisible autonomous layer made
// visible: declare standing per-model policies (form+JSON), watch the cycle board, and work the
// suggestions inbox. The reciprocal manual-vs-standing note frames the relationship with
// /monitoring (FR-248): SAME checks, SAME gate, SAME shared cooldown — different trigger.
export default function RetrainingPage() {
  const boardRef = useRef<CycleBoardHandle>(null);
  const [editing, setEditing] = useState<{ model_name: string; doc: PolicyDoc } | null>(null);

  return (
    <>
      <PageTitle sub="Standing per-model policies close the loop on their own; suggestions keep you in charge.">
        retraining
      </PageTitle>

      {/* FR-248 (reciprocal of the monitoring note): standing vs manual, same machinery */}
      <p className="mb-6 text-caption-md text-mute">
        [i] policies here run the <span className="text-ink">same</span> monitoring checks on a{' '}
        <span className="text-ink">standing schedule</span>. The manual, one-shot counterpart lives
        in{' '}
        <Link href="/monitoring" className="st-accent underline">
          monitoring
        </Link>{' '}
        — same gate, same cooldown; only the trigger differs.
      </p>

      <div className="mb-6 grid gap-6 lg:grid-cols-[1fr_1.4fr]">
        <PolicyEditor
          key={editing ? `edit:${editing.model_name}` : 'new'}
          initial={editing}
          onSaved={() => {
            setEditing(null);
            boardRef.current?.refresh();
          }}
        />
        <CycleBoard ref={boardRef} onEdit={setEditing} />
      </div>

      <SuggestionsInbox onPromoted={() => boardRef.current?.refresh()} />
    </>
  );
}
