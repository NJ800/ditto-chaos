// Real-browser smoke test: opens every page of the dashboard against a RUNNING backend,
// exercises the non-mutating controls (preview, dry run, activity log, history detail),
// and fails on any console error/warning, uncaught exception, failed request or HTTP >= 400.
//
//   npm run e2e                          (dashboard on http://localhost:5173)
//   $env:E2E_BASE_URL="http://127.0.0.1:8000"; npm run e2e    (the API serving the built dashboard)
//
// It never authorizes a real fault: the only run it starts is a dry run.
import { mkdirSync } from "node:fs";
import { chromium } from "playwright";

const BASE = process.env.E2E_BASE_URL ?? "http://localhost:5173";
const SHOTS = process.env.E2E_SCREENSHOT_DIR ?? "";
const TOKEN = process.env.E2E_API_TOKEN ?? ""; // set when the server runs with API_TOKEN
let expect401 = false;
const problems = [];
const note = (kind, text) => problems.push(`${kind}: ${text}`);

const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } });
page.on("console", (m) => {
  if (expect401 && m.text().includes("401")) return; // the browser's own log line for the expected 401
  if (m.type() === "error" || m.type() === "warning") note(`console.${m.type()}`, `${m.text()} @ ${page.url()}`);
});
page.on("pageerror", (e) => note("uncaught", `${e.message} @ ${page.url()}`));
page.on("requestfailed", (r) => {
  const why = r.failure()?.errorText ?? "";
  if (!why.includes("ERR_ABORTED")) note("request failed", `${r.method()} ${r.url()} ${why}`); // aborts are our own cancellations
});
page.on("response", (r) => {
  if (expect401 && r.status() === 401) return;
  if (r.status() >= 400) note(`http ${r.status()}`, `${r.request().method()} ${r.url()}`);
});

let step = 0;
async function shot(name) {
  if (!SHOTS) return;
  mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: `${SHOTS}/${String(++step).padStart(2, "0")}-${name}.png`, fullPage: true });
}
async function open(path, heading) {
  await page.goto(BASE + path, { waitUntil: "domcontentloaded" });
  await page.getByRole("heading", { level: 1, name: heading }).waitFor({ timeout: 20000 });
  if (await page.locator("vite-error-overlay").count()) note("vite overlay", path);
  await page.waitForTimeout(1500); // let the page's own requests and the stream settle
  console.log(`ok   ${path}`);
}

try {
  if (TOKEN) {
    // Without the token nothing may load, and the page must say why and offer the control.
    expect401 = true;
    await page.goto(BASE + "/", { waitUntil: "domcontentloaded" });
    await page.getByText(/Not authorized: enter the API token/).first().waitFor({ timeout: 20000 });
    if (await page.getByRole("heading", { level: 2, name: "Target health" }).count()) note("auth", "data shown without a token");
    await page.getByRole("button", { name: "Set API token" }).click();
    await page.getByLabel(/API token/).fill(TOKEN);
    await page.getByRole("button", { name: "Save" }).click();
    await page.getByRole("heading", { level: 2, name: "Target health" }).waitFor({ timeout: 20000 });
    expect401 = false;
    console.log("ok   token gate (401 without a token, loads with it)");
  }
  await open("/", "Overview");
  await page.getByRole("heading", { level: 2, name: "Target health" }).waitFor();
  await page.getByRole("status").getByText("Live", { exact: true }).waitFor({ timeout: 15000 }); // stream connected
  await shot("overview");

  await open("/monitoring", "Live monitoring");
  await page.getByRole("heading", { level: 2, name: "Request latency" }).waitFor();
  await shot("monitoring");

  await open("/control", "Experiment control");
  await page.getByRole("button", { name: "Preview plan" }).click();
  await page.getByText("Preview only. Nothing has been executed.").first().waitFor({ timeout: 15000 });
  await shot("control-preview");
  const dry = page.getByRole("button", { name: "Dry run (no injection)" });
  if (await dry.isEnabled()) {
    // A previous run's log may already be on screen: wait for THIS dry run's id to replace it.
    const runLink = page.locator('section a[href^="/history/exp-"]').first();
    const before = (await runLink.count()) ? await runLink.textContent() : "";
    await dry.click();
    await page.waitForFunction(
      (old) => {
        const a = document.querySelector('section a[href^="/history/exp-"]');
        return a !== null && a.textContent !== old;
      },
      before,
      { timeout: 20000 },
    );
    await page.getByText("Running dry run").or(page.getByText("Last dry run")).first().waitFor();
    const log = page.getByRole("log", { name: "Experiment events" });
    await log.waitFor({ timeout: 20000 });
    await page.getByText("DRY RUN — NOT EXECUTED").first().waitFor({ timeout: 30000 });
    await page.getByText(/event\(s\), complete/).waitFor({ timeout: 60000 }); // polling stopped at the end
    if (await page.getByText("EXECUTED", { exact: true }).count()) note("dry run", "a command is shown as EXECUTED");
    if (await page.getByText("NOT VERIFIED", { exact: true }).count()) note("dry run", "a previewed fault is shown as NOT VERIFIED");
    await page.getByRole("button", { name: /^monitoring \d+$/ }).click();
    await page.getByText(/Probes Ditto every tick/).first().waitFor();
    await shot("control-dry-run-filtered");
    await page.getByRole("button", { name: "Show all events" }).click();
    console.log("ok   dry run + activity log");
  } else {
    console.log("skip dry run (an experiment is already in progress)");
  }
  await shot("control");

  // Ranked candidates: ranking and selecting are read-only and must not start a run.
  await page.getByRole("button", { name: /^Rank (candidates|again)$/ }).click();
  await page.getByRole("heading", { level: 3, name: "Ranked candidates" }).waitFor({ timeout: 20000 });
  const pick = page.getByRole("button", { name: /^Select candidate \d+:/ }).first();
  if (await pick.count()) {
    await pick.click();
    await page.getByText("selected by operator").first().waitFor({ timeout: 15000 });
    if (await page.getByRole("heading", { name: "Running experiment", exact: true }).count()) note("candidates", "selecting a candidate started a run");
    if (!(await page.getByRole("button", { name: "Run proposed scenario" }).isDisabled())) note("candidates", "run is enabled without authorization");
    console.log("ok   ranked candidates + select (nothing started)");
  }
  await shot("control-candidates");

  await open("/agents", "Agents");
  await shot("agents");

  await open("/history", "Experiment history");
  const first = page.locator('a[href^="/history/exp-"]').first();
  if (await first.count()) {
    await first.click();
    await page.getByRole("heading", { level: 2, name: "What happened, step by step" }).waitFor({ timeout: 20000 });
    await page.getByRole("region", { name: "Activity log" }).waitFor();
    await page.waitForTimeout(1500);
    await shot("history-detail");
    console.log("ok   history detail");
  }

  await open("/insights", "Insights and learning");
  await page.getByRole("heading", { level: 2, name: "Evidence base" }).waitFor({ timeout: 20000 });
  await shot("insights");

  // Re-run after a fix: open the panel and read the plan. It is never started here.
  const rerun = page.getByRole("button", { name: "Re-run experiment" }).first();
  if (await rerun.count()) {
    await rerun.click();
    const panel = page.getByRole("group", { name: "Re-run experiment" });
    await panel.or(page.getByText(/A revised plan is required/)).first().waitFor({ timeout: 15000 });
    if (await panel.count()) {
      if (!(await panel.getByRole("button", { name: "Start follow-up run" }).isDisabled())) note("rerun", "start is enabled without both confirmations");
    }
    await shot("insights-rerun");
    console.log("ok   re-run panel (plan shown, not started)");
  }

  await open("/dependencies", "Dependencies");
  await shot("dependencies");

  // Products & twins: real fleet data, a filter that is applied by the backend, a live twin read.
  await open("/fleet", "Products & digital twins");
  await page.getByText("Synthetic data.").first().waitFor();
  await page.getByRole("heading", { level: 2, name: "Health by product category" }).waitFor();
  const category = page.getByLabel("Product category");
  const firstProfile = await category.locator("option").nth(1).getAttribute("value");
  const filtered = page.waitForResponse((r) => r.url().includes(`/api/fleet/twins?profile=${firstProfile}`) && r.ok());
  await category.selectOption(firstProfile);
  await filtered;
  const twin = page.locator("table button.num").first();
  const twinId = await twin.textContent();
  const detail = page.waitForResponse((r) => r.url().includes(`/api/fleet/twins/${encodeURIComponent(twinId)}`) && r.ok());
  await twin.click();
  await detail;
  await page.getByText(/read live from Ditto/).waitFor({ timeout: 15000 });
  await shot("fleet");
  console.log("ok   fleet filter + live twin detail");

  await page.goto(BASE + "/no-such-page", { waitUntil: "domcontentloaded" });
  await page.getByText("This page does not exist.").waitFor();
  console.log("ok   404 route");
} catch (e) {
  note("smoke test step failed", e.message.split("\n")[0]);
  await shot("failure");
} finally {
  await browser.close();
}

if (problems.length) {
  console.error(`\n${problems.length} problem(s):`);
  for (const p of [...new Set(problems)]) console.error("  - " + p);
  process.exit(1);
}
console.log("\nno console errors or warnings, no uncaught exceptions, no failed requests");
