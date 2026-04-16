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
      colors: {
        seshat: {
          dark: '#0f172a',
          surface: '#1e293b',
          border: '#334155',
          accent: '#3b82f6',
          'accent-hover': '#2563eb',
          muted: '#94a3b8',
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
