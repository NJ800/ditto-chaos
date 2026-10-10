/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#0f1012",
        surface: "#16181b",
        raised: "#1c1f23",
        line: "#2a2e33",
        ink: "#e4e6e8",
        dim: "#a0a6ad",
        faint: "#70777f",
        accent: "#6b9bd8",
        ok: "#4fae7b",
        warn: "#d6a03a",
        bad: "#dc5f58",
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "-apple-system", "sans-serif"],
        mono: ["JetBrains Mono", "Cascadia Mono", "Consolas", "ui-monospace", "monospace"],
      },
      fontSize: { xxs: ["0.6875rem", "1rem"] },
    },
  },
  plugins: [],
};
