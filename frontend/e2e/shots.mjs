// Captures every page at desktop and 375px mobile widths into frontend/screenshots/, and
// checks what a screenshot cannot show: sideways overflow, the mobile navigation drawer,
// the theme switch, and a visible keyboard focus ring.
// Usage: node e2e/shots.mjs   (E2E_BASE_URL defaults to the dev server on :5173)
import { chromium } from "playwright";
import { mkdirSync } from "node:fs";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:5173";
const PAGES = [["overview", "/"], ["monitoring", "/monitoring"], ["fleet", "/fleet"], ["control", "/control"],
               ["pipeline", "/pipeline"], ["agents", "/agents"], ["history", "/history"], ["insights", "/insights"], ["dependencies", "/dependencies"]];
const SIZES = [["desktop", 1440, 900], ["mobile", 375, 812]];
const problems = [];
const note = (text) => { problems.push(text); console.log("PROBLEM " + text); };

mkdirSync("screenshots", { recursive: true });
const browser = await chromium.launch();

async function visit(page, path) {
  await page.goto(BASE + path, { waitUntil: "domcontentloaded" });
  await page.locator("main h1").first().waitFor({ timeout: 20000 });
  await page.waitForTimeout(1200);
}

for (const [size, width, height] of SIZES) {
  const page = await browser.newPage({ viewport: { width, height } });
  page.on("console", (m) => { if (m.type() === "error") note(`console.error (${size}): ${m.text()}`); });
  page.on("pageerror", (e) => note(`uncaught (${size}): ${e.message}`));
  for (const [name, path] of PAGES) {
    await visit(page, path);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    if (overflow > 1) note(`${name} at ${width}px is ${overflow}px wider than the viewport`);
    await page.screenshot({ path: `screenshots/${name}-${size}.png`, fullPage: true });
    console.log(`saved screenshots/${name}-${size}.png`);
  }

  // Experiment details: the newest experiment in history, if there is one.
  await visit(page, "/history");
  const first = page.locator('a[href^="/history/exp-"]').first();
  if (await first.count()) {
    await first.click();
    await page.getByRole("heading", { level: 2, name: "What happened, step by step" }).waitFor({ timeout: 20000 });
    await page.waitForTimeout(1500);
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
    if (overflow > 1) note(`experiment detail at ${width}px is ${overflow}px wider than the viewport`);
    await page.screenshot({ path: `screenshots/experiment-detail-${size}.png`, fullPage: true });
    console.log(`saved screenshots/experiment-detail-${size}.png`);
  } else {
    console.log("skip experiment detail (history is empty)");
  }

  if (size === "desktop") {
    // Navigation: sidebar links, active state, keyboard focus ring.
    await visit(page, "/");
    const nav = page.getByRole("navigation", { name: "Primary" });
    if ((await nav.getByRole("link").count()) !== PAGES.length) note("sidebar does not list every page");
    if ((await nav.locator('a[aria-current="page"]').textContent())?.trim() !== "Overview") note("active nav item is not marked");
    await nav.getByRole("link", { name: "History" }).click();
    await page.getByRole("heading", { level: 1, name: "Experiment history" }).waitFor();
    await page.keyboard.press("Tab");
    const ring = await page.evaluate(() => {
      const el = document.activeElement;
      const s = el ? getComputedStyle(el) : null;
      return s ? { tag: el.tagName, style: s.outlineStyle, width: s.outlineWidth } : null;
    });
    if (!ring || ring.style === "none" || parseFloat(ring.width) < 2) note(`no visible focus ring on keyboard focus: ${JSON.stringify(ring)}`);
    else console.log(`ok   keyboard focus ring (${ring.tag}, ${ring.width} ${ring.style})`);

    // Theme: dark on request, remembered across a reload, and back to light.
    await page.getByRole("button", { name: "Switch to dark theme" }).click();
    if (!(await page.evaluate(() => document.documentElement.classList.contains("dark")))) note("dark theme was not applied");
    for (const [name, path] of [["overview", "/"], ["monitoring", "/monitoring"], ["control", "/control"]]) {
      await visit(page, path);
      if (!(await page.evaluate(() => document.documentElement.classList.contains("dark")))) note(`dark theme was not kept on ${name}`);
      await page.screenshot({ path: `screenshots/${name}-desktop-dark.png`, fullPage: true });
      console.log(`saved screenshots/${name}-desktop-dark.png`);
    }
    await page.getByRole("button", { name: "Switch to light theme" }).click();
    if (await page.evaluate(() => document.documentElement.classList.contains("dark"))) note("light theme was not restored");
    console.log("ok   theme switch (dark, kept across pages, back to light)");
  } else {
    // Mobile: the drawer opens, lists every page, navigates, and closes itself.
    await visit(page, "/");
    await page.getByRole("button", { name: "Open navigation" }).click();
    const drawer = page.getByRole("dialog");
    await drawer.waitFor();
    if ((await drawer.getByRole("link").count()) !== PAGES.length) note("mobile drawer does not list every page");
    await page.screenshot({ path: "screenshots/navigation-mobile.png" });
    console.log("saved screenshots/navigation-mobile.png");
    await drawer.getByRole("link", { name: "Live monitoring" }).click();
    await page.getByRole("heading", { level: 1, name: "Live monitoring" }).waitFor();
    if (await page.getByRole("dialog").count()) note("mobile drawer stayed open after navigating");
    await page.getByRole("button", { name: "Open navigation" }).click();
    await page.getByRole("dialog").waitFor();
    await page.keyboard.press("Escape");
    await page.getByRole("dialog").waitFor({ state: "detached" });
    console.log("ok   mobile navigation drawer (opens, navigates, closes on Escape)");
  }
  await page.close();
}
await browser.close();
console.log(problems.length ? `\n${problems.length} problem(s)` : "\nno console errors, no sideways overflow, navigation, focus ring and theme switch OK");
process.exit(problems.length ? 1 : 0);
