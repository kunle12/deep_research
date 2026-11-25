# PDL web UI — LCARS restyle proposals

Static, high-fidelity mockups transferring the **LCARS (Okudagram)** design language
from `~/dev/kunle_workspace/docs/mockups/*` onto **every current screen and dialog**
of the Deep Research personal digital library (PDL).

Open [`index.html`](index.html) for the visual gallery, or any `*.html` directly.
Each page accepts `?theme=dark` (default) or `?theme=light`.

Rendered review copies live in [`shots/`](shots/) (`<page>-<theme>.png`).

> Status: **proposal only** — no application code has been changed yet.
> Fonts are the same OFL typefaces as the source mockups (Saira / Saira Extra
> Condensed / IBM Plex Mono), self-hosted in `fonts/`.

---

## 1 · Coverage matrix

Every route, panel and dialog of `deep_research/webui/static` is represented.

### Screens

| PDL UI (current) | Route / trigger | Mockup | Notes |
|---|---|---|---|
| Login card (sign-in step) | `#/login` | `login.html` | Access-panel dialog centred in the LCARS frame; email/password + SSO-ready tab slot (`SSO · soon`) |
| Register step | login → “Create account” | `register.html` | Same panel, `ACCESS PANEL · 02` |
| Email verification step | `#/verify?email=…&token=…` | `verify.html` | Locked email + token + resend; server-log fallback called out in a notice box |
| Password reset | `#/reset` | `reset.html` | Two-stage: request token → token + new password |
| Library list | `#/` | `library.html` | LCARS frame chrome; filter sidebar (paths, tags, stats); report cards with path-coloured accent bar + badge; pager; **live job strip above the bottom bar** |
| Report view (owner) | `#/report/<id>` | `report.html` | TOC · article · right rail (references, glossary, tags, merge, danger zone); action pills; open query popover |
| Report shared by grant | `#/shared/<id>` | `report-shared.html` | `SHARED BY … — READ ONLY` strip; back to “Shared with me”; mutation panels absent |
| Shared-with-me list | `#/shared` | `shared.html` | Owner attribution + `SHARED` badge |
| Secret share link (anonymous) | `#/share/<token>` | `share-anon.html` | Capability banner, no back link; “view only · owner can rotate/revoke” strip |

### Dialog boxes

| PDL UI (current) | Trigger | Mockup | Notes |
|---|---|---|---|
| New research modal | `#new-research`, `?new=1` | `dialog-research.html` | Query + path stepper (Auto/Quick/Deep/Academic/URL source/Applied) |
| Research progress dialog (running) | taskbar row | `dialog-progress.html` | Status badge, elapsed, current step, live event feed, Cancel/Pause/Close |
| Research progress dialog (done) | taskbar row | `dialog-progress-done.html` | Open report / Dismiss / Close |
| Share dialog | report → Share | `dialog-share.html` | Create link + named grants, expiry stepper, shown-once link, rotate/revoke, per-grantee removal |
| Confirm dialog (destructive) | delete report, merge, delete reference | `dialog-confirm.html` | Red cap; the same component covers the non-destructive confirm variant (amber/peach cap, primary confirm) |
| Rename | report → Rename | `dialog-rename.html` | **New** in-theme modal — replaces `window.prompt()` |
| Add source | report → Add source | `dialog-add-source.html` | **New** in-theme modal — replaces `window.prompt()` + `window.alert()` validation |

Also covered implicitly: empty / error states use the `.empty`, `.strip` and
`.noticebox` primitives shown across the pages; taskbar states (running, paused,
done, failed) use the status badge system shown in `library.html` and the two
progress dialogs.

---

## 2 · Design language extracted from the source mockups

### Palette (identical values in both themes, different contexts)

| Token | Value | Use |
|---|---|---|
| peach | `#FFCC99` | primary chrome, secondary buttons, selected chips |
| amber | `#FF9933` | primary action, progress, current emphasis |
| lilac | `#CC99CC` | secondary panels, status neutral, shared badges |
| sky | `#99CCFF` | informational, links/PDF actions, references |
| marigold | `#FFCC66` | warnings / verification |
| mauve | `#C08BC0` | academic-path accent |
| red | `#CC6666` | destructive / deny |
| green | `#63C08A` | “nominal” LEDs / done state |

Light theme: paper `#F2ECE3`, screen `#F8FBFF → #E8EEF8`, ink `#2A2630`.
Dark theme: paper `#0E0F12`, screen `#16181D → #0F1114`, ink `#EDE7DF`.

### Type & metrics

- **Display** — Saira Extra Condensed 300/400, uppercase, letter-spacing `.10–.15em`
  (headlines, card titles, dialog titles).
- **UI** — Saira (variable) for body copy and form values.
- **Instrument** — IBM Plex Mono 400/500, 8.5–10.5 px, uppercase, tracking `.14–.34em`
  (kickers, labels, meta, badges, readouts, taskbar).
- **Shape** — LCARS elbows: `border-radius: 0 17px 17px 0` pills (and mirrored
  `17px 0 0 17px` on the right rail); screen panel `26px`; cards `16px`;
  dialog cards `20px`.
- **Frame** — top brand strip + segment chain, 88 px side rails of numbered
  stubs, bottom status band; content sits in a large rounded “screen” panel.

### PDL-specific mappings (decisions to review)

| Element | Current app | Proposal |
|---|---|---|
| Path badges | colour-per-path | quick→sky · deep→lilac · academic→mauve · url_source→peach · applied→marigold · merged→neutral |
| Secondary nav | top-bar links | left rail: `01 LIBRARY` active + `02 SHARED`; remaining numbered stubs decorative (as in the source mockups) |
| Account | top-right chip | moved into the top readout: `12 REPORTS · 91 ARTIFACTS · you@… · SIGN OUT` |
| Search | header input | same position, mono placeholder `SEARCH / · PRESS / TO FOCUS` |
| Taskbar | floating bottom rows | slim LCARS job strips stacked **above** the bottom status band; one segment per job (LED/spinner, query, phase, elapsed, ✕) |
| Read-only shares | banner + hidden controls | sky “SHARED … — READ ONLY” strip; right rail keeps references/glossary only |
| Destructive actions | red buttons | red pills + red dialog cap + `THIS CANNOT BE UNDONE` footer segment |

---

## 3 · Implementation path (for after approval)

1. **Tokens + base layer** — port `lcars.css` into `static/styles.css` as CSS
   custom properties per `[data-theme]` (the app already has a theme toggle and
   `localStorage` persistence — no JS change needed there).
2. **Chrome** — replace the current top bar/taskbar markup with the LCARS frame
   (static markup in `index.html` + `views/taskbar.js` styling). Keep existing
   IDs (`#global-search`, `#new-research`, `#account`, `#theme-toggle`,
   `#nav-shared`, `#stats`) so `app.js` bindings survive.
3. **Components** — restyle existing class names (`.btn`, `.card`, `.panel`,
   `.modal`, `.chip`, `.badge`, `.pager-btn`, `.text-input`) rather than
   renaming them; this keeps every view module untouched except where noted.
4. **Native dialogs → in-theme modals** — `window.prompt` in
   `views/report.js` (rename, add-source) and `window.alert` validation paths
   become `promptDialog()` built on the existing `openModal()` helper;
   `confirmDialog()` only needs the new shell.
5. **Fonts** — copy `fonts/` + `fonts.css` into `static/fonts/`; CSP already
   allows same-origin assets (`default-src 'self'`).
6. **Mobile** — the source mockups include mobile variants; responsive rails
   collapse (top/bottom only) at <900 px. Not mocked up yet — flag for a
   follow-up pass if wanted.
7. **Regression risk** — the share/read-only contract is enforced by
   `opts.mutate`; the restyle must not reintroduce mutation controls into the
   shared views. The mockups deliberately show their absence.

---

## 4 · Decisions (approved 2026-11-25)

1. **Rails** — removed entirely (revised after review: they duplicated the
   bottom-band telemetry and consumed width). The bottom band's last segment
   carries `Shared <count>` (replacing the static `PDL` label); the old empty
   blue top-bar stub now shows the live local time.
2. **Light first** — light is the product default; the toggle persists dark.
3. **Real telemetry** — the bottom band shows live `reports · artifacts`,
   `tags`, auth mode and the PDL tag instead of LCARS filler strings.
4. **SSO tab** — the `SSO · soon` slot stays in the access panel; the custom
   SSO integration lands after this restyle.
5. **Native prompts** — replaced by in-theme `promptDialog` / `alertDialog`
   (rename, add source, errors) built on the existing modal scaffolding.
6. **Mobile** — responsive layouts are part of the deliverable: the rail turns
   into a horizontal nav bar, filters collapse into a compact strip, report
   columns stack, dialogs go full-width. See `shots/*-mobile.png`.

The production implementation follows these decisions — see
`docs/UI_RESTYLE.md`.
