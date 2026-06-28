// ASCII-bracket status glyph, colored per the inverted-semantic rule (status is where color lives).
type Tone = 'success' | 'danger' | 'warning' | 'accent' | 'mute';

const GLYPH: Record<Tone, string> = {
  success: '✓',
  danger: 'x',
  warning: '!',
  accent: '~',
  mute: ' ',
};

export function Badge({ tone, children }: { tone: Tone; children?: React.ReactNode }) {
  return (
    <span className={`st-${tone} text-body-strong`}>
      [{GLYPH[tone]}]{children ? <span className="ml-1">{children}</span> : null}
    </span>
  );
}
