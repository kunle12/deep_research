# Web UI restyle — LCARS theme

Status: implemented 2026-11-25. Proposals live in
[`docs/ui-mockups-lcars/`](ui-mockups-lcars/README.md).

The web UI now uses the LCARS (Okudagram) design language from
`~/dev/kunle_workspace/docs/mockups`, applied to every screen and dialog.
Light is the default theme; dark is a persisted toggle.

## Approved decisions

| # | Decision | Implementation |
|---|----------|----------------|
| 1 | No rail | Rails removed entirely (duplicated the band and wasted width). The bottom band's last segment is the `Shared <count>` link (replacing the static `PDL` label); the old blue top-bar stub is now a live clock |
| 2 | Light first | `:root` is the light token set; `[data-theme="dark"]` overrides. `initTheme()` defaults to light unless the user saved dark |
| 3 | Real telemetry | Bottom band shows live `reports · artifacts`, `tags`, auth mode and the PDL tag; the top-bar Shared count comes from `listSharedWithMe()` |
| 4 | Keep the SSO tab | Access panel shows `EMAIL` (active) + `SSO · soon`; SSO integration lands after this restyle |
| 5 | Replace native prompts | `promptDialog()` / `alertDialog()` in `views/dialog.js`; rename, add source and error paths use them |
| 6 | Mobile compatible | ≤980 px: filters collapse, report columns stack, dialogs full-width, top bar wraps with the Shared chip + search; ≤620 px further compacts the top bar (account hidden) |

## Design tokens

- **Palette** — peach `#FFCC99`, amber `#FF9933`, lilac `#CC99CC`, sky `#99CCFF`,
  marigold `#FFCC66`, mauve `#C08BC0`, red `#CC6666`, LED green `#63C08A`.
  Light paper `#F2ECE3`, screen gradient `#F8FBFF → #E8EEF8`, ink `#2A2630`;
  dark paper `#0E0F12`, screen `#16181D → #0F1114`, ink `#EDE7DF`.
- **Type** — Saira Extra Condensed (display, uppercase, tracked), Saira (UI),
  IBM Plex Mono (kickers, labels, meta, readouts). Self-hosted woff2 in
  `deep_research/webui/static/fonts/`, declared in `styles.css` (`@font-face`);
  no third-party requests (CSP `default-src 'self'`).
- **Shape** — LCARS elbows (`0 17px 17px 0` pills), 16 px cards, 18–20 px
  dialog/section panels, 24 px screen panel, 20 px bottom-band top radius.
- **Path colours** — quick→sky, deep→lilac, academic→mauve, url_source→peach,
  applied→marigold, merged→dim (card accent bars use `:has(.badge-*)`).
- **Dialog caps** — peach by default, amber for New research, lilac for research
  progress, red only for destructive confirms (`.confirm-modal.danger`).

## Layout / DOM

`static/index.html`:

```
.topbar      brand capsule · PDL tag · live clock · search · account (email + Workspace in SSO) · theme · new research
#app         main "screen" panel, full frame width (page-scrolls)
.bottomband  fixed telemetry band (reports/artifacts · tags · mode · Shared <count>)
```

All IDs used by `app.js` are preserved (`#app`, `#global-search`,
`#theme-toggle`, `#stats`, `#new-research`, `#account`, `#nav-shared`) plus new
readouts (`#shared-count` inside the bottom-band Shared link, `#top-clock` in
the top bar, `#telemetry-tags` and `#telemetry-mode` in the bottom band).
`#stats` now lives in the bottom band.

`app.js` additions: `renderStats()` (band), `setMode()` (band), `setNavActive()`
(route-aware Shared state), `refreshSharedCount()` (multi-user only; never
fetches signed-out), `applyAuthChrome()` (hides search/new research and shows
"signed out / shared view · read only" when unauthenticated).

## Fixed defects found during the restyle

1. **Job progress dialog never rendered** (`views/jobDialog.js`): the dialog was
   appended to a locally created overlay that was never attached to the DOM;
   only `openModal()`'s overlay is attached. The dialog is now appended to
   `openModal().overlay` and that overlay gets the `jobdialog-overlay` class
   (centering + entrance animation). Verified in-browser: 620×319 dialog paints,
   Escape/overlay close and cleanup still run.
2. **Inline SVG icons were invisible** (`dom.js`): `el("svg"/"path")` used
   `document.createElement`, so the query icon rendered as an empty circle.
   `el()` now creates SVG tags in the SVG namespace (and sets `class` via
   `setAttribute` for SVG).
3. **Path labels** — `url_source` now displays as `URL SOURCE` (class names
   unchanged) in the library and report badges.

## Accessibility / behaviour preserved

- Keyboard shortcuts (`/`, `j`/`k`, Escape) and focus-visible outlines kept.
- Taskbar rows keep in-place updates, spinner/status semantics and the ✕ action.
- Shared/read-only views still render no mutation controls (verified: 0
  Rename/Share/Delete buttons on an anonymous capability link).
- CSRF header, login wall and 401 → `#/login` handling unchanged.
- `prefers-reduced-motion` disables the new animations.

## Revisions after design review

- **Left rail removed** (desktop and mobile): it duplicated the bottom-band
  telemetry and consumed horizontal space. The screen panel now spans the full
  frame; the top bar is the single navigation surface.
- **Shared count moved into the bottom band**: the last (amber) segment is the
  `Shared <count>` link to `#/shared`, replacing the static `PDL` label.
- **Blue top-bar stub is now a live clock** (local `HH:MM`, updated every 30 s,
  full timestamp in the tooltip) instead of an empty decorative pill.

## Verification

- Browser (Playwright, production build served by uvicorn):
  - sign-in → library → report → share dialog (create link) → rename prompt →
    add-source prompt → delete confirm → new research → live job taskbar →
    progress dialog → anonymous share link, at 1600×1000 and 390×844.
  - Zero console/page errors on clean flows; zero mutation controls on the
    anonymous share view; bottom band shows live counts.
- `node --check` on every changed ES module.
- Full pytest suite green (see commit message / log).

## Adversarial review disposition

Reviewer: DeepSeek V4.1 Flash 1017 (independent subagent). 10 findings; all
verified against the tree before fixing.

| ID | Sev | Finding | Disposition |
|----|-----|---------|-------------|
| R1 | HIGH | `≤620px` hid the whole account chip, making sign-out impossible on mobile | **fixed**: hide only `.account-email`; keep the Sign out control. New-research collapses to `＋` (labelled) to make room |
| R2 | MED | Read-only shared reports still rendered per-reference **Delete** and **Get PDF** (both mutate) | **fixed**: `mutate` threaded into `refsList`/`refCard`; both actions only render for owners |
| R3 | MED | Late `listSharedWithMe`/`getStats` responses could repaint signed-out chrome (stale count, 401s) | **fixed**: `authEpoch` incremented on identity change; results dropped when the epoch/session no longer matches; stats refresh skips when signed out |
| R4 | MED | Sign-out left the search text, tracked jobs and taskbar from the previous account | **fixed**: sign-out clears the search box and performs a full reload to `#/login`, dropping all session-scoped UI state |
| R5 | MED | Dialogs had no accessible name and never took focus (background controls stayed keyboard-reachable) | **fixed**: `aria-labelledby` on every dialog; `openModal()` focuses the first control and traps Tab |
| R6 | LOW | Prompt input unlabelled; error box not announced | **fixed**: generated `id` + `for` on the label; `role="alert"` on the error |
| R7 | LOW | Light-theme focus outline (`#FF9933`) is 1.8:1 on paper | **fixed**: new `--focus` token (`#7A4516` light / `#FFB870` dark) used by all focus outlines |
| R8 | LOW | Light-theme `--dim` (8.5–9.5px meta) was 3.3:1 | **fixed**: darkened to `#67566D` (≈5.7:1 on paper) |
| R9 | LOW | Saved dark theme flashed light before the module ran | **fixed**: tiny CSP-safe `theme-init.js` in `<head>` applies `data-theme` before the stylesheet |
| R10 | LOW | Saira variable font declared `100 900` but ships `400..600` | **fixed**: `@font-face` range corrected to `400 600` |

Reviewer also confirmed clean: job-dialog overlay fix, SVG namespace handling,
CSP/fonts, ID wiring/null-safety, taskbar/band/z-index geometry, and the test
suite.

## 2026-11-27 — Workspace return (SSO account chip)

Follow-up to the restyle; no Python or API changes.

- **Look** — the SSO account chip's plain orange `Workspace` text link is now a
  quiet sky link (`--workspace-ink`, `#2E5E96` light / `#A9D4FF` dark) with a
  return-arrow glyph. Hover/focus lifts it into a small LCARS elbow
  (`--workspace-tint`), matching the frame's command blocks without competing
  with the amber New research button. The email still truncates first; the
  action is `flex: none`.
- **Behaviour** — clicking it returns to the console. The console spawns app
  tabs with `window.open()` and sets `tab.opener = null`, so the handle is
  normally absent and focusing it is only a best-effort branch. The button
  calls `window.close()`, which browsers honour for script-opened tabs, and
  250 ms later — if the tab is still open (a phone, or a tab opened directly) —
  navigates to `workspace_url` in the current tab. This replaces the old
  unconditional `location.assign`, which never closed the spawned tab.
- **Mobile** — ≤620 px the account email is hidden but the labelled Workspace
  action remains (it is the only exit in SSO mode). ≤360 px the link's padding
  and tracking and the clock padding tighten so brand, clock and actions stay
  on one row at 320 px.
- **Verified** — Playwright against a local SSO instance (`X-Auth-User` via a
  trusted loopback): a tab spawned from a fake console with `opener` severed
  closes on click and the console tab stays active; a directly-opened tab is
  refused by `window.close()` and lands on `workspace_url` after the fallback.
  Layout checked at 1280/980/620/390/360/320 px (no horizontal overflow;
  320 px header stays two rows), light and dark themes, hover state.
  `node --check` on `app.js`; webui pytest suites green.
