# 003-frontend — Design Language

**Terminal / man-page visual system**, adapted from the OpenCode marketing design analysis
(`DESIGN-opencode.ai.md`). The operator console reads like a static-site README / TUI: monospace
everywhere, warm cream canvas, near-black ink, hairline borders, 4px radius on interactive elements,
ASCII bracket markers (`[+] [-] [x]`) as glyphs, one dark "terminal" surface.

## Two adaptations from the source (it's a *marketing* system; this is an *app*)

1. **Font → OSS.** Berkeley Mono is paid. Use **JetBrains Mono** (self-hosted woff2) — the source
   names it the closest metric match (weights 400/500/700). Required by Principle V (OSS). Fallback
   stack: `JetBrains Mono, ui-monospace, SFMono-Regular, Menlo, Consolas, monospace`.
2. **Semantic color is INVERTED.** The source keeps chrome monochrome and *reserves* the Apple HIG
   ramp for the in-product TUI. **Our UI is that in-product surface** — so we *use* the ramp for
   operator status: `success` = healthy / run completed; `danger` = failed / unhealthy; `warning` =
   drift flagged / supervisor backoff; `accent` = active / streaming / promoted. Chrome stays
   monochrome; status is where color lives.

## Tokens (carried from the source)

**Colors** — canvas `#fdfcfc`, ink `#201d1d`, body `#424245`, mute `#646262`, ash `#9a9898`;
surfaces soft `#f8f7f7` / card `#f1eeee` / dark `#201d1d` / dark-elevated `#302c2c`; hairline
`rgba(15,0,0,0.12)`. Semantic: accent `#007aff`, success `#30d158`, warning `#ff9f0a`, danger
`#ff3b30` (+ hover/active deepenings per the source).

**Type** (JetBrains Mono): display-xl 38/700, heading-md 16/700, body-md 16/400, body-strong 16/500,
button-md 16/500 (lh 2), caption-md 14/400. Hierarchy is size+weight on one face.

**Radius**: `none` 0px on every container (cards, nav, tables, the dark console); `sm` 4px on every
interactive element (buttons, inputs, badges); `full` only for avatars (n/a here).

**Spacing** (8px base): 1/4/8/12/16/24/32, section 96px. Content left-flush; bullets are ASCII
brackets, not indents. No shadows, no gradients — flat-on-cream; the only "elevation" is the dark
console surface.

## Component mapping (marketing → operator console)

| Source component | Operator-console use |
|---|---|
| `hero-tui-mockup` (dark terminal) | **Infer streaming console** (live tokens on the dark surface) + **Runs live log** — the natural home for the one dark surface |
| `tui-prompt-row` | the Infer prompt input + the active model/selector line |
| `list-row` with `[+]/[-]/[x]` | **Models / Datasets / Runs tables** — each row prefixed by a status bracket (`[✓]` promoted, `[+]` healthy, `[x]` failed, `[~]` running) |
| `badge-news` / `badge-section-label` | **tab labels** + **status badges** (daemon healthy/unhealthy, drift flagged) — colored per the inverted semantic rule |
| `text-input` / `textarea` | Infer prompt box; Runs launch form fields |
| `install-snippet` | command/config display (e.g. the API call equivalent, run config echo) |
| `chart-tile` (ASCII sparse-line) | **Health metric tiles** (GPU free, latency) above the embedded Grafana panels |
| `faq-row` (`+`/`−` toggle) | collapsible details (run params, drift per-feature PSI breakdown) |
| `primary-nav` (ASCII wordmark + links) | the six-tab top nav; wordmark = ASCII "MLOPS-LITE" |

## Build notes

- **Stack:** Tailwind CSS for utilities, with these tokens in `tailwind.config` (CSS variables for
  the palette). **No shadcn default** — the flat ASCII system is specific enough that ~8 small custom
  components are cleaner; reach for headless Radix primitives only where real behavior is needed
  (dialog, accordion). This **supersedes the earlier "Tailwind + shadcn/ui" default** in spec/plan.
- **Iconography = ASCII brackets**, not an icon library. `[+] [-] [x] [✓] [~] [!]` carry state.
- **One dark surface per view, max** — the streaming console (Infer) / live log (Runs). Everything
  else is hairline-on-cream.
- **Do not** introduce a sans-serif, drop shadows, gradients, or an SVG icon set — that breaks the
  identity. Status color is the only departure from monochrome, and only on status elements.
