'use client';

// 027 T696 — six hand-rolled SVG chart primitives (research R11).
//
// **No charting dependency.** `ui/package.json` stays exactly `next` + `react` + `react-dom`
// (SC-198), which is Principle III applied to the console: a chart library is a large runtime
// dependency to render six shapes, and the shapes this console needs are simple enough that owning
// them is cheaper than owning the integration.
//
// Every primitive follows the same two rules:
//
//   * **Missing data is drawn as missing**, never as zero. A sparkline over `[1, null, 3]` shows a
//     gap, because a line through the gap asserts a measurement nobody took.
//   * **Nothing is animated and nothing is interactive.** These read at a glance in a dense
//     operator view; motion there is noise, and a tooltip hides the number it should be showing.

import React from 'react';

type Num = number | null | undefined;

const isNum = (v: Num): v is number => typeof v === 'number' && Number.isFinite(v);

function extent(values: Num[]): [number, number] {
  const nums = values.filter(isNum);
  if (nums.length === 0) return [0, 1];
  const lo = Math.min(...nums);
  const hi = Math.max(...nums);
  return lo === hi ? [lo - 1, hi + 1] : [lo, hi];
}

/** Map a value into pixel space, inverted (SVG y grows downward). */
function scaleY(v: number, lo: number, hi: number, height: number, pad = 2): number {
  const t = (v - lo) / (hi - lo);
  return height - pad - t * (height - 2 * pad);
}

// -- 1. sparkline -------------------------------------------------------------------------------

export function Sparkline({
  values,
  width = 120,
  height = 24,
  label,
}: {
  values: Num[];
  width?: number;
  height?: number;
  label?: string;
}) {
  const [lo, hi] = extent(values);
  const step = values.length > 1 ? width / (values.length - 1) : width;

  // Split into runs of consecutive present values so a gap stays a gap.
  const runs: string[] = [];
  let current: string[] = [];
  values.forEach((v, i) => {
    if (isNum(v)) {
      current.push(`${(i * step).toFixed(1)},${scaleY(v, lo, hi, height).toFixed(1)}`);
    } else if (current.length) {
      runs.push(current.join(' '));
      current = [];
    }
  });
  if (current.length) runs.push(current.join(' '));

  return (
    <svg width={width} height={height} role="img" aria-label={label ?? 'sparkline'}>
      {runs.map((points, i) => (
        <polyline key={i} points={points} fill="none" stroke="currentColor" strokeWidth="1" />
      ))}
    </svg>
  );
}

// -- 2. time series with a band ------------------------------------------------------------------

export function TimeSeriesBand({
  values,
  lower,
  upper,
  width = 320,
  height = 80,
  label,
}: {
  values: Num[];
  lower?: Num[];
  upper?: Num[];
  width?: number;
  height?: number;
  label?: string;
}) {
  const [lo, hi] = extent([...values, ...(lower ?? []), ...(upper ?? [])]);
  const step = values.length > 1 ? width / (values.length - 1) : width;
  const pt = (v: number, i: number) => `${(i * step).toFixed(1)},${scaleY(v, lo, hi, height).toFixed(1)}`;

  const band =
    lower && upper
      ? [
          ...upper.map((v, i) => (isNum(v) ? pt(v, i) : null)).filter(Boolean),
          ...lower
            .map((v, i) => (isNum(v) ? pt(v, lower.length - 1 - i) : null))
            .filter(Boolean)
            .reverse(),
        ].join(' ')
      : null;

  return (
    <svg width={width} height={height} role="img" aria-label={label ?? 'time series'}>
      {band && <polygon points={band} fill="currentColor" opacity="0.12" />}
      <polyline
        points={values.map((v, i) => (isNum(v) ? pt(v, i) : '')).filter(Boolean).join(' ')}
        fill="none"
        stroke="currentColor"
        strokeWidth="1.25"
      />
    </svg>
  );
}

// -- 3. threshold bar ----------------------------------------------------------------------------

/** A value against its threshold. The threshold is drawn, never implied by colour alone. */
export function ThresholdBar({
  value,
  threshold,
  max,
  width = 180,
  height = 12,
  label,
}: {
  value: Num;
  threshold: Num;
  max?: Num;
  width?: number;
  height?: number;
  label?: string;
}) {
  const ceiling = isNum(max) ? max : Math.max(isNum(value) ? value : 0, isNum(threshold) ? threshold : 1);
  const w = (v: number) => Math.max(0, Math.min(width, (v / ceiling) * width));

  return (
    <svg width={width} height={height} role="img" aria-label={label ?? 'threshold'}>
      <rect x="0" y="0" width={width} height={height} fill="currentColor" opacity="0.08" />
      {isNum(value) && (
        <rect x="0" y="0" width={w(value)} height={height} fill="currentColor" opacity="0.45" />
      )}
      {isNum(threshold) && (
        <line
          x1={w(threshold)}
          x2={w(threshold)}
          y1="0"
          y2={height}
          stroke="currentColor"
          strokeWidth="1.5"
        />
      )}
    </svg>
  );
}

// -- 4. span waterfall ---------------------------------------------------------------------------

export type Span = { name: string; startMs: number; durationMs: number; depth?: number };

export function SpanWaterfall({
  spans,
  width = 420,
  rowHeight = 16,
  label,
}: {
  spans: Span[];
  width?: number;
  rowHeight?: number;
  label?: string;
}) {
  const end = Math.max(1, ...spans.map((s) => s.startMs + s.durationMs));
  const height = Math.max(rowHeight, spans.length * rowHeight);

  return (
    <svg width={width} height={height} role="img" aria-label={label ?? 'span waterfall'}>
      {spans.map((s, i) => (
        <g key={`${s.name}-${i}`}>
          <rect
            x={(s.startMs / end) * width}
            y={i * rowHeight + 2}
            width={Math.max(1, (s.durationMs / end) * width)}
            height={rowHeight - 4}
            fill="currentColor"
            opacity={0.25 + 0.1 * (s.depth ?? 0)}
          />
        </g>
      ))}
    </svg>
  );
}

// -- 5. parallel coordinates ---------------------------------------------------------------------

/** One line per trial across N hyperparameter axes — the HPO view. */
export function ParallelCoordinates({
  axes,
  rows,
  width = 420,
  height = 140,
  label,
}: {
  axes: string[];
  rows: Num[][];
  width?: number;
  height?: number;
  label?: string;
}) {
  const columns = axes.map((_, i) => extent(rows.map((r) => r[i])));
  const step = axes.length > 1 ? width / (axes.length - 1) : width;

  return (
    <svg width={width} height={height} role="img" aria-label={label ?? 'parallel coordinates'}>
      {axes.map((_, i) => (
        <line
          key={i}
          x1={i * step}
          x2={i * step}
          y1="0"
          y2={height}
          stroke="currentColor"
          opacity="0.15"
        />
      ))}
      {rows.map((row, r) => {
        // A row with a missing value is not drawn: interpolating across the gap would invent a
        // coordinate for a trial that has none.
        if (row.some((v) => !isNum(v))) return null;
        const points = row
          .map((v, i) => `${i * step},${scaleY(v as number, columns[i][0], columns[i][1], height)}`)
          .join(' ');
        return (
          <polyline key={r} points={points} fill="none" stroke="currentColor" strokeWidth="1" opacity="0.5" />
        );
      })}
    </svg>
  );
}

// -- 6. matrix heatmap ---------------------------------------------------------------------------

export function MatrixHeatmap({
  matrix,
  cell = 18,
  label,
}: {
  matrix: Num[][];
  cell?: number;
  label?: string;
}) {
  const flat = matrix.flat();
  const [lo, hi] = extent(flat);
  const rows = matrix.length;
  const cols = matrix[0]?.length ?? 0;

  return (
    <svg width={cols * cell} height={rows * cell} role="img" aria-label={label ?? 'matrix'}>
      {matrix.map((row, r) =>
        row.map((v, c) => (
          <rect
            key={`${r}-${c}`}
            x={c * cell}
            y={r * cell}
            width={cell - 1}
            height={cell - 1}
            fill="currentColor"
            // A missing cell is drawn at the lowest opacity AND is distinguishable from a true
            // minimum by its stroke — colour alone cannot say "no measurement".
            opacity={isNum(v) ? 0.1 + 0.8 * ((v - lo) / (hi - lo)) : 0.04}
            stroke={isNum(v) ? 'none' : 'currentColor'}
            strokeDasharray={isNum(v) ? undefined : '2 2'}
            strokeWidth={isNum(v) ? 0 : 0.5}
          />
        )),
      )}
    </svg>
  );
}
