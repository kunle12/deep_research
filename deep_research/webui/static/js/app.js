/* App bootstrap: theme, routing, keyboard shortcuts, header search, account. */

import { el, clear } from "./dom.js";
import { renderList } from "./views/list.js";
import { renderReport } from "./views/report.js";
import { renderAuth } from "./views/login.js";
import { renderSharedByGrant, renderSharedByToken, renderSharedList } from "./views/shared.js";
import { openResearchModal } from "./views/research.js";
import { initTaskbar } from "./views/taskbar.js";
import { restoreJobs } from "./jobs.js";
import { getStats, getAuthStatus, logout } from "./api.js";
import { closeAllModals } from "./modal.js";

const app = document.getElementById("app");
const searchInput = document.getElementById("global-search");
const themeBtn = document.getElementById("theme-toggle");
const statsEl = document.getElementById("stats");
const newResearchBtn = document.getElementById("new-research");
const accountEl = document.getElementById("account");

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
  const m = path.match(/^\/report\/([^/]+)/);
  if (m) return { name: "report", reportId: decodeURIComponent(m[1]) };
  const sh = path.match(/^\/shared\/([^/]+)/);
  if (sh) return { name: "sharedReport", shareId: decodeURIComponent(sh[1]) };
  const sl = path.match(/^\/share\/([^/]+)/);
  if (sl) return { name: "share", token: decodeURIComponent(sl[1]) };
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
  if (authed === true && AUTH_ROUTES.has(route.name)) {
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
}

function renderAuthView(name, params) {
  if (currentCleanup) {
    currentCleanup();
    currentCleanup = null;
  }
  closeAllModals();
  clear(app);
  currentRoute = { name, params };
  currentCleanup = renderAuth(app, name, params);
}

// Re-check auth state and re-render (login, logout, verification, 401 bounce).
async function refreshAuth() {
  const was = authed;
  const status = await getAuthStatus().catch(() => ({ authenticated: false }));
  authed = Boolean(status.authenticated);
  drawAccountChip(status.user || null);
  if (authed && !was) {
    // First sign-in this page load: pull the pieces boot() skipped.
    initStats();
    restoreJobs();
  }
  render();
}

function initTheme() {
  const saved = localStorage.getItem("dr-theme");
  if (saved === "light" || saved === "dark") {
    document.documentElement.dataset.theme = saved;
  }
  themeBtn.addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("dr-theme", next);
  });
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

function initStats() {
  getStats()
    .then((s) => {
      statsEl.textContent = `${s.reports} reports · ${s.artifacts} artifacts`;
    })
    .catch(() => {});
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
      getStats()
        .then((s) => {
          statsEl.textContent = `${s.reports} reports · ${s.artifacts} artifacts`;
        })
        .catch(() => {});
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
              // Drop the authed flag BEFORE navigating: the hashchange render
              // must not fetch library data with a dead session (would 401).
              authed = false;
              statsEl.textContent = "";
              if (window.location.hash !== "#/login") window.location.hash = "#/login";
              // refreshAuth re-probes /api/auth/status: clears the chip and
              // renders the login view.
              refreshAuth();
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
    restoreJobs();
  }
  render();
}

boot();
