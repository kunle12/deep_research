/* App bootstrap: theme, routing, keyboard shortcuts, header search, account. */

import { el, clear } from "./dom.js";
import { renderList } from "./views/list.js";
import { renderReport } from "./views/report.js";
import { renderAuth } from "./views/login.js";
import { renderSharedByGrant, renderSharedByToken, renderSharedList } from "./views/shared.js";
import { openResearchModal } from "./views/research.js";
import { initTaskbar } from "./views/taskbar.js";
import { restoreJobs } from "./jobs.js";
import { getStats, getAuthStatus, listSharedWithMe, logout } from "./api.js";
import { closeAllModals } from "./modal.js";

const app = document.getElementById("app");
const searchInput = document.getElementById("global-search");
const themeBtn = document.getElementById("theme-toggle");
const statsEl = document.getElementById("stats");
const newResearchBtn = document.getElementById("new-research");
const accountEl = document.getElementById("account");
// Frame readouts (top-bar shared badge + bottom telemetry band).
const sharedCount = document.getElementById("shared-count");
const telemetryTags = document.getElementById("telemetry-tags");
const telemetryMode = document.getElementById("telemetry-mode");
let authUser = null;
// Increments whenever the session identity changes; guards late responses.
let authEpoch = 0;

let currentCleanup = null;
let currentRoute = { name: "list" };
// null = boot check in flight; false = unauthenticated (login wall);
// true = signed in. Set before the first render by boot().
let authed = null;
// Routes reachable WITHOUT a session: the auth screens and secret share
// links (a share token IS the capability).
const AUTH_ROUTES = new Set(["login", "verify", "reset", "share"]);

function routeFromHash() {
  const raw = location.hash.replace(/^#/, "") || "/";
  const [path, query = ""] = raw.split("?");
  const params = Object.fromEntries(new URLSearchParams(query));
  // decodeURIComponent can throw on malformed escapes (a mangled paste) —
  // fall back to the raw segment so one bad hash can't blank the app.
  const decode = (s) => {
    try {
      return decodeURIComponent(s);
    } catch {
      return s;
    }
  };
  const m = path.match(/^\/report\/([^/]+)/);
  if (m) return { name: "report", reportId: decode(m[1]) };
  const sh = path.match(/^\/shared\/([^/]+)/);
  if (sh) return { name: "sharedReport", shareId: decode(sh[1]) };
  const sl = path.match(/^\/share\/([^/]+)/);
  if (sl) return { name: "share", token: decode(sl[1]) };
  if (path === "/login") return { name: "login", params };
  if (path === "/verify") return { name: "verify", params };
  if (path === "/reset") return { name: "reset", params };
  if (path === "/shared") return { name: "shared" };
  return { name: "list" };
}

function render() {
  if (authed === null) return; // boot() renders once auth state is known
  const route = routeFromHash();
  // Login wall: unauthenticated visitors only ever see the auth views;
  // signed-in users skip them. Checked BEFORE touching any data fetch so a
  // logged-out page load produces no failed requests in the console.
  if (authed === false && !AUTH_ROUTES.has(route.name)) {
    if (currentRoute.name !== "login") window.location.hash = "#/login";
    else renderAuthView("login", {});
    return;
  }
  if (authed === true && route.name !== "share" && AUTH_ROUTES.has(route.name)) {
    // login/verify/reset are pointless once signed in — but a share link is a
    // capability its recipient may well open while signed in (F2): never bounce.
    window.location.hash = "#/";
    return;
  }
  if (currentCleanup) {
    currentCleanup();
    currentCleanup = null;
  }
  // Navigate away from any open modal (confirm/job-dialog/research) so the new
  // route never renders underneath a stale overlay.
  closeAllModals();
  clear(app);
  currentRoute = route;
  setNavActive(route.name);
  applyAuthChrome(route.name);
  if (route.name === "report") {
    currentCleanup = renderReport(app, route.reportId);
  } else if (route.name === "shared") {
    currentCleanup = renderSharedList(app);
  } else if (route.name === "sharedReport") {
    currentCleanup = renderSharedByGrant(app, route.shareId);
  } else if (route.name === "share") {
    currentCleanup = renderSharedByToken(app, route.token);
  } else if (AUTH_ROUTES.has(route.name)) {
    currentCleanup = renderAuth(app, route.name, route.params || {});
  } else {
    currentCleanup = renderList(app, searchInput);
  }
  // Keep the shared badge fresh after visiting the shared area (a grant may
  // have arrived, or a share may have been revoked elsewhere).
  if (route.name === "shared" || route.name === "sharedReport") refreshSharedCount();
}

function renderAuthView(name, params) {
  if (currentCleanup) {
    currentCleanup();
    currentCleanup = null;
  }
  closeAllModals();
  clear(app);
  currentRoute = { name, params };
  setNavActive(name);
  applyAuthChrome(name);
  currentCleanup = renderAuth(app, name, params);
}

// Re-check auth state and re-render (login, logout, verification, 401 bounce).
async function refreshAuth() {
  const was = authed;
  const status = await getAuthStatus().catch(() => ({ authenticated: false }));
  authed = Boolean(status.authenticated);
  if (authed !== was) authEpoch += 1;
  drawAccountChip(status.user || null);
  if (authed && !was) {
    // First sign-in this page load: pull the pieces boot() skipped.
    initStats();
    refreshSharedCount();
    restoreJobs();
  }
  render();
}

function initTheme() {
  // Light is the product default (LCARS mockups reviewed light-first); the
  // toggle persists an explicit choice.
  const saved = localStorage.getItem("dr-theme");
  document.documentElement.dataset.theme = saved === "dark" ? "dark" : "light";
  themeBtn.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("dr-theme", next);
  });
}

function initClock() {
  const clock = document.getElementById("top-clock");
  if (!clock) return;
  const tick = () => {
    const now = new Date();
    clock.textContent = now.toLocaleTimeString([], {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    clock.title = now.toLocaleString();
  };
  tick();
  setInterval(tick, 30000);
}

function initSearch() {
  let debounce;
  searchInput.addEventListener("input", (event) => {
    clearTimeout(debounce);
    debounce = setTimeout(() => {
      document.dispatchEvent(new CustomEvent("dr:search", { detail: event.target.value.trim() }));
    }, 250);
  });
}

function initKeyboard() {
  document.addEventListener("keydown", (event) => {
    const target = event.target;
    const inField = target.matches && target.matches("input, select, textarea");
    if (event.key === "/" && !inField) {
      event.preventDefault();
      searchInput.focus();
      return;
    }
    if (event.key === "Escape") {
      if (document.activeElement === searchInput) {
        searchInput.blur();
      } else if (!document.querySelector(".modal-overlay") && currentRoute.name === "report") {
        // An open modal/dialog owns Escape; don't navigate away underneath it.
        window.location.hash = "#/";
      }
      return;
    }
    if (inField) return;
    const cards = [...document.querySelectorAll(".card")];
    if (!cards.length) return;
    if (event.key === "j") {
      event.preventDefault();
      focusCard(cards, 1);
    } else if (event.key === "k") {
      event.preventDefault();
      focusCard(cards, -1);
    }
  });
}

function focusCard(cards, direction) {
  const index = cards.indexOf(document.activeElement);
  const next = index === -1 ? (direction > 0 ? 0 : cards.length - 1) : index + direction;
  cards[Math.max(0, Math.min(cards.length - 1, next))].focus();
}

/* Frame chrome helpers: bottom-band telemetry and top-bar nav state. */

function renderStats(s) {
  if (!authed) return; // a late response must not repaint the signed-out chrome
  statsEl.textContent = `${s.reports} reports · ${s.artifacts} artifacts`;
  if (telemetryTags) telemetryTags.textContent = String(s.tags);
}

function setMode(me) {
  const label = !me
    ? "AWAITING SIGN-IN"
    : me.auth_mode === "single_user"
      ? "SINGLE-USER · LOCAL"
      : "MULTI-USER · SECURE";
  if (telemetryMode) telemetryMode.textContent = label;
}

function setNavActive(routeName) {
  const shared = routeName === "shared" || routeName === "sharedReport";
  const sharedLink = document.getElementById("nav-shared");
  if (sharedLink) sharedLink.classList.toggle("nav-shared-active", shared);
}

// Header/band state that depends on auth + route. Anonymous share readers see
// a capability notice instead of this library's — irrelevant — counts.
function applyAuthChrome(routeName) {
  if (newResearchBtn) newResearchBtn.hidden = authed === false;
  const searchWrap = document.querySelector(".search-wrap");
  if (searchWrap) searchWrap.hidden = authed === false;
  if (authed === false) {
    statsEl.textContent = routeName === "share" ? "shared view · read only" : "library · signed out";
    if (telemetryTags) telemetryTags.textContent = "—";
  }
}

// Shared badge: how many reports are shared with this account. Signed-out and
// single-user sessions never fetch (no 401 noise in the console).
function refreshSharedCount() {
  const sharedLink = document.getElementById("nav-shared");
  if (!authed || !authUser || authUser.auth_mode === "single_user") return Promise.resolve();
  const epoch = authEpoch;
  return listSharedWithMe()
    .then((items) => {
      if (!authed || epoch !== authEpoch) return; // signed out/switched mid-flight
      if (sharedLink && sharedCount) {
        sharedCount.textContent = String(items.length);
        sharedLink.hidden = false;
      }
    })
    .catch(() => {});
}

function initStats() {
  getStats().then(renderStats).catch(() => {});
}

const TERMINAL = new Set(["done", "failed", "cancelled", "lost"]);
// Refresh the header counts when the library changes — either a research job
// reaches a terminal state (e.g. a completed report changes the totals) or a
// report is deleted / merged / renamed from the report page (dr:library). The
// "N reports · M artifacts" readout must not stay stale until a reload.
function initStatsRefresh() {
  const prev = new Map(); // job_id -> status
  let timer = null;
  const refresh = () => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      if (!authed) return;
      getStats().then(renderStats).catch(() => {});
    }, 250);
  };
  document.addEventListener("dr:jobs", (event) => {
    const list = (event.detail && event.detail.jobs) || [];
    const ids = new Set(list.map((j) => j.job_id));
    for (const id of [...prev.keys()]) if (!ids.has(id)) prev.delete(id);
    let changed = false;
    for (const job of list) {
      const old = prev.get(job.job_id);
      // Refresh on first sight of a terminal job too, not just transitions, so
      // a job that appears already finished still updates the header counts.
      if (TERMINAL.has(job.status) && (old === undefined || old !== job.status)) {
        changed = true;
      }
      prev.set(job.job_id, job.status);
    }
    if (changed) refresh();
  });
  document.addEventListener("dr:library", refresh);
}

function initNewResearch() {
  newResearchBtn.addEventListener("click", () => openResearchModal());
  // Deep link: /?new=1 opens the modal on load (also used by smoke tests).
  if (new URLSearchParams(window.location.search).has("new")) {
    openResearchModal();
  }
}

/* Account chip: shows the signed-in email + logout (multi_user), or the
 * owner's identity in single_user mode. Empty while unauthenticated. */
function initAccount() {
  const drawChip = (me) => {
    authUser = me;
    setMode(me);
    const sharedLink = document.getElementById("nav-shared");
    if (sharedLink) sharedLink.hidden = !me || me.auth_mode === "single_user";
    if (!me) {
      accountEl.replaceChildren();
      return;
    }
    if (me.auth_mode === "single_user") {
      accountEl.replaceChildren(el("span", { class: "account-email", text: me.email, title: "single-user mode" }));
      return;
    }
    accountEl.replaceChildren(
      el("span", { class: "account-email", text: me.email }),
      el("button", {
        class: "linklike",
        type: "button",
        text: "Sign out",
        onclick: () => {
          logout()
            .catch(() => {})
            .finally(() => {
              // Full reload is deliberate: tracked jobs and the taskbar live
              // outside #app (and the search box persists), so a reload is the
              // only way to guarantee the next account can't inherit any of
              // the previous user's session-scoped UI state.
              authEpoch += 1;
              authed = false;
              searchInput.value = "";
              window.location.hash = "#/login";
              window.location.reload();
            });
        },
      }),
    );
  };
  document.addEventListener("dr:auth", refreshAuth);
  return drawChip;
}

window.addEventListener("hashchange", render);
initTheme();
initClock();
initSearch();
initKeyboard();
initStatsRefresh();
initNewResearch();
initTaskbar();
const drawAccountChip = initAccount();

// Boot: resolve auth state FIRST (one /api/auth/status probe — never 401s),
// then render the right view — no library data is fetched while logged out,
// so the console stays clean on the login screen.
async function boot() {
  const status = await getAuthStatus().catch(() => ({ authenticated: false }));
  authed = Boolean(status.authenticated);
  drawAccountChip(status.user || null);
  if (authed) {
    initStats();
    refreshSharedCount();
    restoreJobs();
  }
  render();
}

boot();
