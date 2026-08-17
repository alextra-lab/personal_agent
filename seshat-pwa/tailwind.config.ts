import type { Config } from 'tailwindcss';

const config: Config = {
  content: [
    './src/pages/**/*.{js,ts,jsx,tsx,mdx}',
    './src/components/**/*.{js,ts,jsx,tsx,mdx}',
    './src/app/**/*.{js,ts,jsx,tsx,mdx}',
  ],
  darkMode: 'class',
  theme: {
    extend: {
      fontFamily: {
        // Serif prose (FRE-1264 AC-4) — matches the ticket's target stack.
        serif: ['Georgia', 'Cambria', '"Times New Roman"', 'Times', 'serif'],
      },
      colors: {
        // Theme-aware semantic tokens (FRE-1264) — resolve through the CSS
        // custom properties in globals.css, which flip under `.dark`.
        // Additive: does not touch the `seshat` / `slate` / `blue` overrides
        // below, which keep not-yet-converted components' dark look intact.
        // rgb(var(--x) / <alpha-value>) (not a bare var(--x)) so Tailwind can
        // apply opacity modifiers (bg-accent/15 etc.) — globals.css stores
        // these as RGB triplets, not hex, to match (FRE-1265).
        bg: 'rgb(var(--bg) / <alpha-value>)',
        surface: 'rgb(var(--surface) / <alpha-value>)',
        line: 'rgb(var(--line) / <alpha-value>)',
        ink: 'rgb(var(--ink) / <alpha-value>)',
        'ink-muted': 'rgb(var(--ink-muted) / <alpha-value>)',
        accent: 'rgb(var(--accent) / <alpha-value>)',
        'accent-hover': 'rgb(var(--accent-hover) / <alpha-value>)',
        seshat: {
          dark: '#1e2940',            // was #0f172a
          surface: '#2b3a55',         // was #1e293b
          border: '#3d4d6b',          // was #334155
          accent: '#2f6bff',          // was #3b82f6
          'accent-hover': '#1f5cff',  // was #2563eb
          muted: '#9aa6b6',           // was #94a3b8
        },
        // Lifted slate ramp — overrides only these steps; deep-merges with defaults.
        // Every existing bg-slate-N / text-slate-N utility picks up the new value.
        slate: {
          100: '#f3f6fa',
          200: '#e3e8ef',
          300: '#cdd5df',
          400: '#9aa6b6',
          500: '#6e7b90',
          600: '#566179',
          700: '#3d4d6b',
          800: '#2b3a55',
          900: '#1e2940',
          950: '#131c2e',
        },
        // Vivid product blue (links / user avatar / selection rings)
        blue: {
          400: '#5b8bff',
          500: '#2f6bff',
          600: '#1f5cff',
        },
      },
      animation: {
        'pulse-dot': 'pulse 1.2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
      },
      typography: {
        DEFAULT: {
          css: {
            // Remove backtick quotes around inline code
            'code::before': { content: '""' },
            'code::after': { content: '""' },
            // Inline code pill styling
            code: {
              backgroundColor: 'rgb(51 65 85)',  // slate-700
              borderRadius: '0.25rem',
              padding: '0.1em 0.35em',
              fontWeight: '400',
            },
          },
        },
      },
    },
  },
  plugins: [
    require('@tailwindcss/typography'),
  ],
};

export default config;
