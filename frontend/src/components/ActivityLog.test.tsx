import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, api } from "../api/client";
import type { EventDetail, EventKind, ExperimentEvent, ExperimentEventsResponse } from "../api/types";
import { ActivityLog } from "./ActivityLog";

vi.mock("../api/client", async (orig) => {
  const real = await orig<typeof import("../api/client")>();
  return { ...real, api: { ...real.api, events: vi.fn() } };
});
const events = api.events as unknown as ReturnType<typeof vi.fn>;

const ev = (seq: number, state: string, kind: EventKind, message: string, detail: EventDetail = {}, t_s = seq * 1.25): ExperimentEvent => ({
  seq,
  ts: "2026-10-10T17:00:00+00:00",
  t_s,
  state,
  kind,
  message,
  detail,
});
const res = (list: ExperimentEvent[], over: Partial<ExperimentEventsResponse> = {}): ExperimentEventsResponse => ({
  experiment_id: "exp-1",
  state: "COMPLETED",
  running: false,
  terminal: true,
  dry_run: false,
  last_seq: list.length ? Math.max(...list.map((e) => e.seq)) : 0,
  events: list,
  ...over,
});

const FULL = [
  ev(1, "PRECHECK", "info", "Experiment exp-1 started (REAL fault injection): delay on mongodb."),
  ev(2, "PRECHECK", "decision", "Policy check passed: every target is allowlisted.", { apply_order: ["a1"] }),
  ev(3, "INJECTING", "command", "Ran: docker run --rm -d netshoot tc qdisc replace", { command: "docker run --rm -d netshoot tc qdisc replace dev eth0 root netem delay 300ms", executed: true, returncode: 0, read_only: false, duration_ms: 412 }),
  ev(4, "MONITORING", "health", "Direct read OK 41 ms; search FAILED; staleness p95 unavailable.", { direct_ok: true, direct_ms: 41, search_ok: false, search_ms: null, stale_p95_s: null, lost_acked: 0, search_drift: null }),
  ev(5, "MONITORING", "decision", "Kill switch triggered: twin staleness p95 > 15.0s for 3 consecutive ticks. Rolling back.", { kill_switch: true, reason: "staleness" }),
  ev(6, "REVERTING", "command", "Command exited 1: docker run tc qdisc del", { command: "docker run --rm netshoot tc qdisc del dev eth0 root", executed: true, returncode: 1, stderr: "RTNETLINK answers: No such file", read_only: false }),
  ev(7, "REVERTING", "error", "Removal of delay from mongodb could NOT be verified.", { verified: false }),
  ev(8, "RECOVERY_REQUIRED", "error", "Experiment ended with RECOVERY REQUIRED. Rollback was NOT fully verified.", { final: true }),
];

beforeEach(() => {
  events.mockReset();
});

const rows = () => within(screen.getByRole("log")).getAllByRole("listitem");

describe("ActivityLog", () => {
  it("shows a loading state, then the events in sequence order with time, state and kind", async () => {
    let release: (r: ExperimentEventsResponse) => void = () => {};
    events.mockReturnValue(new Promise<ExperimentEventsResponse>((r) => (release = r)));
    render(<ActivityLog experimentId="exp-1" />);
    expect(screen.getByRole("status", { name: "Loading" })).toBeInTheDocument();
    await act(async () => release(res([FULL[4], FULL[0], FULL[3], FULL[1], FULL[2]]))); // server order must not matter
    expect(rows().map((r) => r.getAttribute("data-seq"))).toEqual(["1", "2", "3", "4", "5"]);
    expect(within(rows()[0]).getByText("+1.3s")).toBeInTheDocument();
    expect(within(rows()[0]).getByText("precheck")).toBeInTheDocument();
    expect(within(rows()[0]).getByText("INFO")).toBeInTheDocument();
    expect(within(rows()[4]).getByText("DECISION")).toBeInTheDocument(); // kind is text, not colour alone
    expect(within(rows()[4]).getByText(/Kill switch triggered: twin staleness/)).toBeInTheDocument();
    expect(events).toHaveBeenCalledWith("exp-1", 0, expect.anything());
  });

  it("distinguishes executed commands from dry-run previews and shows exit code and error output", async () => {
    const dry = ev(9, "INJECTING", "command", "Would run (dry run, not executed): docker kill dry-run-fake-id", { command: "docker kill dry-run-fake-id", executed: false, read_only: false });
    events.mockResolvedValue(res([FULL[2], FULL[5], dry], { dry_run: true }));
    render(<ActivityLog experimentId="exp-1" />);
    const [ok, failed, preview] = await waitFor(() => rows());
    expect(within(ok).getByText("EXECUTED")).toBeInTheDocument();
    expect(within(ok).getByText("exit code 0")).toBeInTheDocument();
    expect(within(ok).getByText(/netem delay 300ms/).tagName).toBe("CODE");
    expect(within(failed).getByText("exit code 1")).toBeInTheDocument();
    expect(within(failed).getByText("RTNETLINK answers: No such file")).toBeInTheDocument();
    expect(within(preview).getByText("DRY RUN — NOT EXECUTED")).toBeInTheDocument();
    expect(within(preview).queryByText("EXECUTED")).not.toBeInTheDocument();
    expect(within(preview).queryByText(/exit code/)).not.toBeInTheDocument();
    expect(screen.getByText("dry run: nothing is executed")).toBeInTheDocument();
  });

  it("copies a command with a keyboard-accessible button", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
    events.mockResolvedValue(res([FULL[2]]));
    render(<ActivityLog experimentId="exp-1" />);
    const button = await screen.findByRole("button", { name: "Copy command" });
    button.focus();
    await userEvent.keyboard("{Enter}");
    expect(writeText).toHaveBeenCalledWith("docker run --rm -d netshoot tc qdisc replace dev eth0 root netem delay 300ms");
    expect(await screen.findByText("Copied")).toBeInTheDocument();
  });

  it("shows unavailable health measurements as unavailable, and failures as failures", async () => {
    events.mockResolvedValue(res([FULL[3]]));
    render(<ActivityLog experimentId="exp-1" />);
    const row = (await waitFor(() => rows()))[0];
    expect(within(row).getByText("direct 41 ms")).toBeInTheDocument();
    expect(within(row).getByText("search FAILED")).toBeInTheDocument();
    expect(within(row).getByText("staleness p95 unavailable")).toBeInTheDocument();
    expect(within(row).getByText("search drift unavailable")).toBeInTheDocument();
    expect(within(row).getByText("lost updates 0")).toBeInTheDocument(); // a real zero stays a zero
  });

  it("filters by lifecycle step, explains the step, and returns to all events", async () => {
    const user = userEvent.setup();
    events.mockResolvedValue(res(FULL, { state: "RECOVERY_REQUIRED" }));
    render(<ActivityLog experimentId="exp-1" />);
    await waitFor(() => expect(rows()).toHaveLength(8));
    const step = screen.getByRole("button", { name: /^monitoring 2$/ });
    await user.click(step);
    expect(step).toHaveAttribute("aria-pressed", "true");
    expect(rows().map((r) => r.getAttribute("data-seq"))).toEqual(["4", "5"]);
    expect(screen.getByText(/Probes Ditto every tick and decides/)).toBeInTheDocument();
    expect(screen.getByText(/Showing 2 of 8 events/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /^outcome 1$/ }));
    expect(rows().map((r) => r.getAttribute("data-seq"))).toEqual(["8"]);
    await user.click(screen.getByRole("button", { name: "Show all events" }));
    expect(rows()).toHaveLength(8);
    expect(screen.getByRole("button", { name: /^baseline 0$/ })).toBeDisabled(); // no events in that step
    expect(screen.getByText("RECOVERY REQUIRED")).toBeInTheDocument();
  });

  it("expands structured details on request", async () => {
    events.mockResolvedValue(res([FULL[1]]));
    render(<ActivityLog experimentId="exp-1" />);
    const toggle = await screen.findByRole("button", { name: "Details" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await userEvent.click(toggle);
    expect(screen.getByText(/"apply_order"/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Hide details" })).toHaveAttribute("aria-expanded", "true");
  });

  it("polls incrementally with after_seq, appends without duplicates, and stops once terminal", async () => {
    events
      .mockResolvedValueOnce(res(FULL.slice(0, 3), { terminal: false, running: true, state: "INJECTING" }))
      .mockResolvedValueOnce(res([FULL[2], FULL[3], FULL[4]], { terminal: false, running: true, state: "MONITORING" })) // overlaps seq 3
      .mockResolvedValueOnce(res(FULL.slice(5), { terminal: true, state: "RECOVERY_REQUIRED" }))
      .mockResolvedValue(res([]));
    render(<ActivityLog experimentId="exp-1" pollMs={15} />);
    await waitFor(() => expect(rows()).toHaveLength(3));
    expect(screen.getByText("live, updating")).toBeInTheDocument();
    await waitFor(() => expect(rows()).toHaveLength(8));
    expect(rows().map((r) => r.getAttribute("data-seq"))).toEqual(["1", "2", "3", "4", "5", "6", "7", "8"]);
    expect(events.mock.calls.map((c) => c[1])).toEqual([0, 3, 5]);
    expect(screen.getByText("8 event(s), complete")).toBeInTheDocument();
    await new Promise((r) => setTimeout(r, 80));
    expect(events).toHaveBeenCalledTimes(3); // no polling after the run finished
  });

  it("stops polling when unmounted", async () => {
    events.mockResolvedValue(res(FULL.slice(0, 2), { terminal: false, running: true, state: "PRECHECK" }));
    const view = render(<ActivityLog experimentId="exp-1" pollMs={15} />);
    await waitFor(() => expect(events.mock.calls.length).toBeGreaterThanOrEqual(2));
    view.unmount();
    const n = events.mock.calls.length;
    await new Promise((r) => setTimeout(r, 80));
    expect(events.mock.calls.length).toBe(n);
  });

  it("pauses auto-scroll when the operator scrolls up and resumes on Jump to latest", async () => {
    events
      .mockResolvedValueOnce(res(FULL.slice(0, 4), { terminal: false, running: true, state: "MONITORING" }))
      .mockResolvedValueOnce(res([FULL[4]], { terminal: false, running: true, state: "MONITORING" }))
      .mockResolvedValue(res([], { terminal: false, running: true, state: "MONITORING", last_seq: 5 }));
    render(<ActivityLog experimentId="exp-1" pollMs={40} />);
    await waitFor(() => expect(rows()).toHaveLength(4));
    const log = screen.getByRole("log");
    Object.defineProperty(log, "scrollHeight", { value: 1000, configurable: true });
    Object.defineProperty(log, "clientHeight", { value: 300, configurable: true });
    expect(screen.queryByRole("button", { name: "Jump to latest" })).not.toBeInTheDocument();

    log.scrollTop = 100; // the operator scrolls up to read earlier events
    fireEvent.scroll(log);
    expect(screen.getByRole("button", { name: "Jump to latest" })).toBeInTheDocument();
    await waitFor(() => expect(rows()).toHaveLength(5));
    expect(log.scrollTop).toBe(100); // new events did not yank the view down

    await userEvent.click(screen.getByRole("button", { name: "Jump to latest" }));
    expect(log.scrollTop).toBe(1000);
    expect(screen.queryByRole("button", { name: "Jump to latest" })).not.toBeInTheDocument();
  });

  it("follows new events while the operator stays at the bottom", async () => {
    events
      .mockResolvedValueOnce(res(FULL.slice(0, 2), { terminal: false, running: true, state: "PRECHECK" }))
      .mockResolvedValue(res(FULL.slice(2), { terminal: true }));
    const spy = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(640);
    render(<ActivityLog experimentId="exp-1" pollMs={15} />);
    await waitFor(() => expect(rows()).toHaveLength(8));
    expect(screen.getByRole("log").scrollTop).toBe(640);
    spy.mockRestore();
  });

  it("shows an empty state for a running experiment and a legacy note for an old record", async () => {
    events.mockResolvedValueOnce(res([], { terminal: false, running: true, state: "PRECHECK" })).mockResolvedValue(res([], { terminal: false, running: true }));
    const view = render(<ActivityLog experimentId="exp-1" pollMs={1000} />);
    expect(await screen.findByText("No events recorded yet.")).toBeInTheDocument();
    view.unmount();

    events.mockReset();
    events.mockResolvedValue(res([])); // terminal, zero events: a record from before the log existed
    render(<ActivityLog experimentId="exp-legacy" />);
    expect(await screen.findByText(/saved before the log existed/)).toBeInTheDocument();
    expect(screen.queryByRole("log")).not.toBeInTheDocument();
    expect(events).toHaveBeenCalledTimes(1);
  });

  it("reports an API error, keeps the events already shown, and recovers on the next poll", async () => {
    events
      .mockResolvedValueOnce(res(FULL.slice(0, 2), { terminal: false, running: true, state: "PRECHECK" }))
      .mockRejectedValueOnce(new ApiError(0, "Cannot reach the backend at http://127.0.0.1:8000.", "unreachable"))
      .mockResolvedValue(res(FULL.slice(2), { terminal: true }));
    render(<ActivityLog experimentId="exp-1" pollMs={30} />);
    await waitFor(() => expect(rows()).toHaveLength(2));
    expect(await screen.findByText(/Could not update the activity log: Cannot reach the backend/)).toBeInTheDocument();
    expect(rows()).toHaveLength(2); // nothing invented, nothing dropped
    await waitFor(() => expect(rows()).toHaveLength(8));
    expect(screen.queryByText(/Could not update the activity log/)).not.toBeInTheDocument();
    expect(events.mock.calls.map((c) => c[1])).toEqual([0, 2, 2]); // the failed request was retried from the same point
  });

  it("shows the error when the first load fails", async () => {
    events.mockRejectedValue(new ApiError(404, "no saved experiment 'exp-x'"));
    render(<ActivityLog experimentId="exp-x" pollMs={1000} />);
    expect(await screen.findByText(/Could not update the activity log: no saved experiment 'exp-x'/)).toBeInTheDocument();
  });

  it("reloads from scratch when the experiment changes", async () => {
    events.mockResolvedValue(res(FULL.slice(0, 2)));
    const view = render(<ActivityLog experimentId="exp-1" />);
    await waitFor(() => expect(rows()).toHaveLength(2));
    events.mockResolvedValue(res([ev(1, "PRECHECK", "info", "Dry run exp-2 started.")], { experiment_id: "exp-2" }));
    view.rerender(<ActivityLog experimentId="exp-2" />);
    expect(await screen.findByText("Dry run exp-2 started.")).toBeInTheDocument();
    expect(rows()).toHaveLength(1);
    expect(events).toHaveBeenLastCalledWith("exp-2", 0, expect.anything());
  });
});
