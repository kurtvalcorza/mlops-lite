import type { Config } from 'tailwindcss';

// Terminal / man-page system (design-language.md). Palette is driven by CSS variables defined in
// app/globals.css so the tokens have one source of truth. Chrome is monochrome; the Apple-HIG
// semantic ramp is reserved for operator STATUS only (inverted-semantic rule).
const config: Config = {
  content: ['./app/**/*.{ts,tsx}', './components/**/*.{ts,tsx}', './lib/**/*.{ts,tsx}'],
  theme: {
    // Flat-on-cream: no shadows, no gradients. Override the defaults to keep us honest.
    boxShadow: { none: 'none' },
    extend: {
      colors: {
        canvas: 'var(--canvas)',
        ink: 'var(--ink)',
        body: 'var(--body)',
        mute: 'var(--mute)',
        ash: 'var(--ash)',
        soft: 'var(--surface-soft)',
        card: 'var(--surface-card)',
        dark: 'var(--surface-dark)',
        'dark-elevated': 'var(--surface-dark-elevated)',
        hairline: 'var(--hairline)',
        accent: 'var(--accent)',
        success: 'var(--success)',
        warning: 'var(--warning)',
        danger: 'var(--danger)',
      },
      fontFamily: {
        // JetBrains Mono is wired via next/font in app/layout.tsx (--font-mono).
        mono: ['var(--font-mono)', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'Consolas', 'monospace'],
      },
      borderRadius: { none: '0px', sm: '4px', full: '9999px' },
      fontSize: {
        'display-xl': ['38px', { lineHeight: '1.1', fontWeight: '700' }],
        'heading-md': ['16px', { lineHeight: '1.4', fontWeight: '700' }],
        'body-md': ['16px', { lineHeight: '1.6', fontWeight: '400' }],
        'body-strong': ['16px', { lineHeight: '1.6', fontWeight: '500' }],
        'button-md': ['16px', { lineHeight: '2', fontWeight: '500' }],
        'caption-md': ['14px', { lineHeight: '1.5', fontWeight: '400' }],
      },
      spacing: { section: '96px' },
    },
  },
  plugins: [],
};

export default config;
