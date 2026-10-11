import { useSyncExternalStore } from "react";

export type Theme = "light" | "dark";

const KEY = "ditto-chaos-theme";
const listeners = new Set<() => void>();

function stored(): Theme {
  try {
    return window.localStorage.getItem(KEY) === "dark" ? "dark" : "light"; // light unless the operator chose dark
  } catch {
    return "light";
  }
}

let current: Theme = typeof window === "undefined" ? "light" : stored();

function apply(theme: Theme): void {
  if (typeof document === "undefined") return;
  document.documentElement.classList.toggle("dark", theme === "dark");
}
apply(current);

export function setTheme(theme: Theme): void {
  current = theme;
  apply(theme);
  try {
    window.localStorage.setItem(KEY, theme);
  } catch {
    // storage unavailable (private window): the choice lasts for this page only
  }
  listeners.forEach((l) => l());
}

export function useTheme(): Theme {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => current,
    () => "light" as Theme,
  );
}

/** Chart colours cannot come from CSS classes (Recharts writes SVG attributes), so the
 *  same tokens are repeated here per theme. Series slots are a fixed, validated order:
 *  see "Charts" in frontend/DESIGN_SYSTEM.md. */
export interface ChartColors {
  grid: string;
  axis: string;
  tick: string;
  text: string;
  tooltipBg: string;
  tooltipBorder: string;
  fault: string;
  limit: string;
  baseline: string;
  series: string[];
}

export const CHART: Record<Theme, ChartColors> = {
  light: {
    grid: "#e3e8ef",
    axis: "#c5cfdb",
    tick: "#5b6b82",
    text: "#172033",
    tooltipBg: "#ffffff",
    tooltipBorder: "#dce3ec",
    fault: "#b91c1c",
    limit: "#a14a08",
    baseline: "#5b6b82",
    series: ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#008300", "#6250d6", "#e34948"],
  },
  dark: {
    grid: "#2a2e33",
    axis: "#383d44",
    tick: "#838a92",
    text: "#e4e6e8",
    tooltipBg: "#16181b",
    tooltipBorder: "#2a2e33",
    fault: "#dc5f58",
    limit: "#d6a03a",
    baseline: "#838a92",
    series: ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181", "#008300", "#9085e9", "#e66767"],
  },
};

/** Number of categorical slots; a ninth series is never given a generated colour. */
export const SERIES_SLOTS = CHART.light.series.length;
