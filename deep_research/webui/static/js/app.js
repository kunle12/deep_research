/* App bootstrap: theme, routing, keyboard shortcuts, header search. */

import { el, clear } from "./dom.js";
import { renderList } from "./views/list.js";
import { renderReport } from "./views/report.js";
import { openResearchModal } from "./views/research.js";
import { initTaskbar } from "./views/taskbar.js";
import { restoreJobs } from "./jobs.js";
import { getStats } from "./api.js";

const app = document.getElementById("app");
const searchInput = document.getElementById("global-search");
const themeBtn = document.getElementById("theme-toggle");
const statsEl = document.getElementById("stats");
const newResearchBtn = document.getElementById("new-research");

let currentCleanup = null;
let currentRoute = { name: "list" };

function routeFromHash() {
  const hash = location.hash.replace(/^#/, "") || "/";
  const m = hash.match(/^\/report\/([^/]+)/);
  if (m) return { name: "report", reportId: decodeURIComponent(m[1]) };
  return { name: "list" };
}

function render() {
  if (currentCleanup) {
    currentCleanup();
    currentCleanup = null;
  }
  clear(app);
  const route = routeFromHash();
  currentRoute = route;
  if (route.name === "report") {
    currentCleanup = renderReport(app, route.reportId);
  } else {
    currentCleanup = renderList(app, searchInput);
  }
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
    let changed = false;
    for (const job of list) {
      const old = prev.get(job.job_id);
      if (old && old !== job.status && TERMINAL.has(job.status)) changed = true;
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

window.addEventListener("hashchange", render);
initTheme();
initSearch();
initKeyboard();
initStats();
initStatsRefresh();
initNewResearch();
initTaskbar();
restoreJobs();
render();
