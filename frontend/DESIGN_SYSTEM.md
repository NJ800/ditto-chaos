# ditto-chaos dashboard design system

This is the reference for anyone changing the dashboard's look. It records the decisions,
the tokens that implement them, and the measurements behind them. The tokens live in
`src/index.css` (CSS variables) and `tailwind.config.js` (class names); chart colours
live in `src/lib/theme.ts`.

## What the dashboard is for

An operator uses it to watch a Ditto sandbox, start a fault, see it stopped and rolled
back, and read what was learned. Two things follow from that:

- **Density over decoration.** Numbers, states and logs are the content. Nothing is
  hidden behind a click to make a page look calmer.
- **Safety states must be impossible to miss.** A failed rollback, an unverified
  recovery, a real run versus a dry run: each has a fixed colour, a text label and,
  where it is page-wide, its own banner.

## Decisions

| Decision | Choice | Why |
|---|---|---|
| Default theme | Light. Dark is kept and selectable. | The brief asks for a bright default; the original dark palette is preserved for operators who prefer it. |
| Theme switch | A `dark` class on `<html>`, set by `src/lib/theme.ts`, remembered in `localStorage`. | No theme library is needed for one class and one stored value. |
| Navigation | Fixed left sidebar from 1024px; a drawer below that. Every item has an icon and a label. | Eight destinations do not fit a phone-width tab bar; a sidebar keeps the top bar free for live status. |
| Top bar | Target state, operating mode, running experiment, connection, API token, theme. | These are true on every page, so they stay visible on every page. |
| Accent | Blue `#1D4ED8`, not the suggested indigo `#4F46E5`. | Indigo and violet accents are the most recognisable "generated dashboard" look. Blue also keeps the accent clearly apart from the violet chart series. |
| Shape | 8px radius on panels, 6px on controls, full pills for status badges only. One hairline border plus a very light shadow. | Restrained, and the pill shape is reserved for status so it reads as status. |
| Headings | Sentence case, dark ink. Uppercase is used only inside status badges. | Small tracked-out uppercase labels on every panel made the pages read as a template. |
| Typeface | System UI sans (Inter when installed); monospace only for ids, commands and figures that must align. | No font download, and tabular figures line up in tables. |
| Motion | Colour transitions on hover only. Nothing animates on its own. `prefers-reduced-motion` turns transitions off. | An operations screen should not move unless the data did. |
| Component library | The existing shared components in `src/components/ui.tsx`, plus one Radix-based drawer written in the shadcn/ui pattern. | See "Why shadcn/ui was not installed wholesale". |

## Colour tokens

Use the class name, never a hex value, in components.

| Token (class) | Role | Light | Dark |
|---|---|---|---|
| `bg` | Page background | `#F6F8FB` | `#0F1012` |
| `surface` | Panels, sidebar, top bar, inputs | `#FFFFFF` | `#16181B` |
| `raised` | Table headers, hover, inset blocks | `#EEF2F7` | `#1C1F23` |
| `line` | Borders and dividers | `#DCE3EC` | `#2A2E33` |
| `ink` | Primary text | `#172033` | `#E4E6E8` |
| `dim` | Secondary text | `#475569` | `#A0A6AD` |
| `faint` | Captions, axis labels | `#5B6B82` | `#838A92` |
| `accent` | Links, primary button, active nav, focus ring | `#1D4ED8` | `#6B9BD8` |
| `onaccent` | Text on `accent` and `bad` fills | `#FFFFFF` | `#0F1012` |
| `info` | Informational notes | `#0369A1` | `#5EB1DE` |
| `ok` | Healthy, verified, completed | `#127035` | `#4FAE7B` |
| `warn` | Degraded, stale, needs attention | `#A14A08` | `#D6A03A` |
| `bad` | Failed, rollback not verified, recovery required | `#B91C1C` | `#E26E67` |

Changes from the suggested palette, all for contrast: secondary text is `#475569` rather
than `#64748B`; healthy and warning are one step darker; information is `#0369A1`
rather than `#0284C7` (4.1:1 on white, below the 4.5:1 text minimum).

### Measured contrast (WCAG ratio)

| Pair | Light | Dark |
|---|---|---|
| `ink` on `surface` | 16.3 | 14.2 |
| `dim` on `surface` | 7.6 | 7.2 |
| `faint` on `surface` / on `raised` | 5.4 / 4.8 | 5.1 / 4.7 |
| `accent` on `surface` (links, focus ring) | 6.7 | 6.2 |
| `ok` / `warn` / `bad` on `surface` | 6.2 / 6.0 / 6.5 | 6.5 / 7.6 / 5.6 |
| Badge text on its own 10% tint, `ok` / `warn` / `bad` | 5.4 / 5.2 / 5.5 | 5.6 / 6.4 / 5.0 |
| `onaccent` on `accent` / on `bad` | 6.7 / 6.5 | 6.6 / 6.0 |

Every text pair is at or above 4.5:1. The border token is 1.3:1 against the surface by
design: borders separate, they do not carry meaning.

## Status

Status is never colour alone. Each state has a colour, a dot and a written label; the
page-wide ones also have an icon.

| State | Token | Where |
|---|---|---|
| Healthy, verified, recovered, completed | `ok` | Badges, stat values |
| Degraded, stale, expired, autonomous mode | `warn` | Badges, warning notes |
| Failed, rollback not verified, recovery required | `bad` | Badges, error notes, the red banner |
| Proposed, allowlisted, current mode | `accent` | Badges |
| Not measured, unknown, not supported | `dim` | Badges; shown as the word, never as 0 |

- **Recovery required** is a full-width banner under the top bar on every page: red
  left edge, warning icon, the manual commands, and the two resolve actions.
- **Dry run versus real run.** A dry run is labelled "DRY RUN — NOT EXECUTED" on every
  command row. Starting a real run needs the authorization checkbox and uses the red
  danger button, which is otherwise used only to stop a run or force-clear a lockout.

## Typography and spacing

| Use | Class |
|---|---|
| Page title | `text-xl font-semibold` |
| Panel title | `text-sm font-semibold` |
| Body, table cells | `text-sm` / `text-xs` |
| Captions, badge text | `text-xs` / `text-xxs` (11px) |
| Figures, ids, commands | `.num` (monospace, tabular figures) |

Spacing uses Tailwind's 4px scale: 16px between panels and inside them, 12px table cell
padding, 8px between controls. Page content is capped at 1500px wide.

## Components

All in `src/components/ui.tsx` unless noted. Pages compose these; they do not restyle them.

| Component | Use |
|---|---|
| `PageHeader` | Page title, one-line description, page-level actions |
| `Panel` | The card: title, optional meta text, optional actions, body |
| `Stat` | A labelled figure (KPI) with an optional sub-line and tone |
| `Badge`, `StateBadge` | Status pill with dot and label |
| `Button` | `default`, `primary` (one per view), `danger` (starting a real fault, stopping a run, forcing the lockout clear); `busy` shows "Working…" |
| `Note`, `ErrorNote` | Inline message; `bad` notes are announced as alerts and can offer Retry |
| `Empty`, `Skeleton` | Empty and loading states |
| `KV`, `List`, `Mono` | Key/value lists, bullet lists, inline ids |
| `.th`, `.td`, `.field`, `.label` | Table and form styles (`src/index.css`) |
| `MetricChart` | Every time-series chart (`MetricChart.tsx`) |
| `ActivityLog` | The experiment timeline (`ActivityLog.tsx`) |
| `Sheet` | Navigation drawer on small screens (`sheet.tsx`) |
| `Layout` | Sidebar, top bar, recovery banner, connection notes (`Layout.tsx`) |

## Charts

- One component, `MetricChart`, on Recharts. Every chart shows real rows from the
  backend, a time axis, a unit on the value axis, and a tooltip.
- **Stop limits** are a dashed line in `warn` labelled "stop limit"; **baselines** are a
  dashed grey line labelled "baseline p95". **Fault intervals** are a shaded red band.
- A failed request is a gap in the line, never a zero.
- Two or more series get a legend. One series does not; the panel title names it.
- **Series colours are fixed slots, assigned in order and never cycled.** A series keeps
  its slot when a filter hides the others. Status colours are not used as series colours.

| Slot | Light | Dark |
|---|---|---|
| 1 | `#2A78D6` | `#3987E5` |
| 2 | `#EB6834` | `#D95926` |
| 3 | `#1BAF7A` | `#199E70` |
| 4 | `#EDA100` | `#C98500` |
| 5 | `#E87BA4` | `#D55181` |
| 6 | `#008300` | `#008300` |
| 7 | `#6250D6` | `#9085E9` |
| 8 | `#E34948` | `#E66767` |

This order was checked with a palette validator against this app's own chart surfaces
(`#FFFFFF` and `#16181B`): adjacent slots stay distinguishable under simulated
colour-blindness (worst pair ΔE 9.1 light, 8.4 dark) and in normal vision (19.6 / 19.3).
Slots 3, 4 and 5 are below 3:1 against white, which is why multi-series charts always
carry a legend and a tooltip.

## Accessibility rules

- Every interactive element shows a 2px `accent` focus ring with a 2px offset.
- Icons next to text are decorative (`aria-hidden`); icon-only buttons have an `aria-label`.
- The navigation drawer traps focus, closes on Escape and returns focus to its button.
- No page scrolls sideways at 375px; wide tables scroll inside their own panel.

## Why shadcn/ui was not installed wholesale

shadcn/ui's current CLI and manual instructions target Tailwind v4 (`@import
"tailwindcss"`, `@theme inline`, `tw-animate-css`). This project is on Tailwind 3.4 and
React 18, and upgrading Tailwind is outside a visual redesign. The app also already had
its own Button, Badge, Card, Table and form primitives with tests around them; adding a
second set would duplicate them.

What was taken from shadcn/ui is its pattern, where the app had a real gap:

- colour tokens as CSS variables with a `.dark` override;
- the `cn()` helper (`clsx` + `tailwind-merge`);
- a Sheet built on Radix Dialog for the mobile navigation;
- Lucide icons.

## References consulted

| Reference | What was taken from it |
|---|---|
| shadcn/ui installation and Tailwind v4 pages | Token-as-CSS-variable approach, `cn()`, Sheet on Radix Dialog, Lucide as icon set |
| Anthropic `frontend-design` skill | The list of generic-looking defaults to avoid: tracked uppercase labels everywhere, one soft shadow under identical cards, decorative gradients, hover motion on every card, accent-coloured headline words |
| UI UX Pro Max README | Checklist items: 4.5:1 text contrast in light mode, visible focus, badges not colour-only, reduced motion, breakpoints 375 / 768 / 1024 / 1440, SVG icons |
| Data-visualisation skill (bundled with the assistant) | The categorical palette, its fixed order and the validator; legend and status-colour rules |
| Grafana Saga overview, Vercel Geist introduction | General principles only (consistency, accessibility, high-contrast neutrals). Both pages are overviews; no specific values were taken from them. |

Tailwind Plus is a paid product and was not used.

## Added dependencies

| Package | Version | Licence | Why |
|---|---|---|---|
| `lucide-react` | 1.55.0 | ISC | Navigation and status icons |
| `@radix-ui/react-dialog` | 1.2.0 | MIT | Accessible drawer for the mobile navigation |
| `clsx` | 2.1.1 | MIT | Conditional class names |
| `tailwind-merge` | 2.6.1 | MIT | Lets a passed class override a default one (v2 is the line for Tailwind 3) |
