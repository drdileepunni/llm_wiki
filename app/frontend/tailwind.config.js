/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,jsx}'],
  theme: {
    extend: {
      fontFamily: {
        display: ['"Playfair Display"', 'serif'],
        body: ['"DM Sans"', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'monospace'],
      },
      colors: {
        ink: {
          950: '#0a0a0f',
          900: '#111118',
          800: '#1a1a24',
          700: '#252533',
          600: '#323244',
        },
        accent: {
          DEFAULT: '#7c6af7',
          dim: '#5a4fd4',
          glow: 'rgba(124, 106, 247, 0.15)',
        },
        surface: '#16161f',
        border: '#2a2a3a',
        muted: '#6b6b8a',
        success: '#4ade80',
        warning: '#fbbf24',
      },
    },
  },
  plugins: [],
}
