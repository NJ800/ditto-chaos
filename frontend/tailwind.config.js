/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{js,ts,jsx,tsx}"],
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        bg: "#0b1220",
        panel: "#111a2e",
        panel2: "#0e1626",
        border: "#223049",
        accent: "#3b9eff",
        good: "#2bd576",
        warn: "#f5a623",
        bad: "#f05252",
        muted: "#8ea0bf",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["ui-monospace", "SFMono-Regular", "Menlo", "monospace"],
      },
    },
  },
  plugins: [],
};
