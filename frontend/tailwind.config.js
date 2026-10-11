/** @type {import('tailwindcss').Config} */
// Colours are CSS variables (RGB triplets, see src/index.css) so the light theme and
// the dark theme share every class name. frontend/DESIGN_SYSTEM.md documents the tokens.
const token = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  darkMode: "class",
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        bg: token("bg"),
        surface: token("surface"),
        raised: token("raised"),
        line: token("line"),
        ink: token("ink"),
        dim: token("dim"),
        faint: token("faint"),
        accent: token("accent"),
        onaccent: token("onaccent"),
        info: token("info"),
        ok: token("ok"),
        warn: token("warn"),
        bad: token("bad"),
      },
      fontFamily: {
        sans: ["Inter", "Segoe UI", "system-ui", "-apple-system", "sans-serif"],
        mono: ["JetBrains Mono", "Cascadia Mono", "Consolas", "ui-monospace", "monospace"],
      },
      fontSize: { xxs: ["0.6875rem", "1rem"] },
      boxShadow: { card: "0 1px 2px rgb(var(--shadow) / 0.06)" },
    },
  },
  plugins: [],
};
