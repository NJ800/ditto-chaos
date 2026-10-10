/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: "#f6f8fb",
        surface: "#ffffff",
        raised: "#f1f4f8",
        line: "#dce3ec",
        ink: "#172033",
        dim: "#475569",
        faint: "#64748b",
        accent: "#2563eb",
        ok: "#15803d",
        warn: "#b45309",
        bad: "#b91c1c",
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
