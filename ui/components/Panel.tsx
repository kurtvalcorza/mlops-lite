import React from 'react';

// A hairline-on-cream section block with a man-page-style heading. The default elevation in this
// system; the only "raised" surface is the dark console (see Console.tsx).
export function Panel({
  title,
  hint,
  children,
  className = '',
}: {
  title?: string;
  hint?: string;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`hairline rounded-none bg-canvas ${className}`}>
      {title && (
        <div className="hairline flex items-baseline gap-3 border-x-0 border-t-0 px-4 py-2">
          <h2 className="text-heading-md text-ink">{title}</h2>
          {hint && <span className="text-caption-md text-ash">{hint}</span>}
        </div>
      )}
      <div className="p-4">{children}</div>
    </section>
  );
}

export function PageTitle({ children, sub }: { children: React.ReactNode; sub?: string }) {
  return (
    <div className="mb-6">
      <h1 className="text-display-xl text-ink">{children}</h1>
      {sub && <p className="mt-1 text-body-md text-mute">{sub}</p>}
    </div>
  );
}
