/* List view: filters + paginated report cards (classic page navigation). */

import { el, clear } from "../dom.js";
import { formatDate, plural } from "../format.js";
import { listReports, getTags, getStats } from "../api.js";

const DEFAULT_PAGE_SIZE = 30;
const PAGE_SIZES = [10, 30, 50, 100];
const PATHS = ["quick", "deep", "academic", "url_source", "applied", "merged"];
const MAX_PAGE_BUTTONS = 7;

export function renderList(root, searchInput) {
  clear(root);

  const state = {
    q: searchInput.value.trim(),
    tag: "",
    path: "",
    items: [],
    total: 0,
    page: 1,
    pageSize: DEFAULT_PAGE_SIZE,
    loading: false,
    error: "",
    tags: [],
  };

  const pathSelect = el(
    "select",
    { class: "select", "aria-label": "Filter by research path" },
    el("option", { value: "", text: "All paths" }),
    ...PATHS.map((p) => el("option", { value: p, text: p })),
  );
  const pageSizeSelect = el(
    "select",
    { class: "select select-sm", "aria-label": "Reports per page" },
    ...PAGE_SIZES.map((n) => el("option", { value: String(n), text: `${n} / page`, selected: n === DEFAULT_PAGE_SIZE })),
  );
  const tagList = el("div", { class: "tag-cloud" });
  const summaryEl = el("div", { class: "filter-summary" });
  const listEl = el("div", { class: "report-list" });
  const statusEl = el("div", { class: "list-status" });
  const pagerEl = el("div", { class: "pager" });
  const statsEl = el("div", { class: "sidebar-stats" });

  const sidebar = el(
    "aside",
    { class: "sidebar" },
    el("h2", { class: "panel-title", text: "Filters" }),
    el("label", { class: "field-label", text: "Path" }),
    pathSelect,
    el("label", { class: "field-label", text: "Tags" }),
    tagList,
    el("hr", { class: "sidebar-divider" }),
    el("h2", { class: "panel-title", text: "Library" }),
    statsEl,
  );
  const main = el(
    "section",
    { class: "list-main" },
    el(
      "div",
      { class: "list-toolbar" },
      summaryEl,
      el("label", { class: "page-size-row" }, el("span", { class: "page-size-label", text: "Per page" }), pageSizeSelect),
    ),
    listEl,
    statusEl,
    pagerEl,
  );
  root.append(el("div", { class: "layout" }, sidebar, main));

  let requestSeq = 0;

  function resetAndLoad() {
    state.page = 1;
    load();
  }

  const onSearch = (event) => {
    state.q = event.detail;
    resetAndLoad();
  };
  document.addEventListener("dr:search", onSearch);

  pathSelect.addEventListener("change", () => {
    state.path = pathSelect.value;
    resetAndLoad();
  });

  pageSizeSelect.addEventListener("change", () => {
    state.pageSize = Number(pageSizeSelect.value) || DEFAULT_PAGE_SIZE;
    resetAndLoad();
  });

  function cleanup() {
    document.removeEventListener("dr:search", onSearch);
  }

  function clearFilters() {
    state.q = "";
    state.tag = "";
    state.path = "";
    state.page = 1;
    searchInput.value = "";
    pathSelect.value = "";
    renderTags();
    load();
  }

  function goToPage(page) {
    const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
    const target = Math.min(Math.max(1, page), totalPages);
    if (target === state.page && state.items.length) return;
    state.page = target;
    load();
  }

  async function load() {
    const seq = ++requestSeq;
    const offset = (state.page - 1) * state.pageSize;
    state.loading = true;
    state.error = "";
    render();
    try {
      const body = await listReports({
        limit: state.pageSize,
        offset,
        q: state.q || undefined,
        tag: state.tag || undefined,
        path: state.path || undefined,
      });
      if (seq !== requestSeq) return; // superseded by a newer request
      state.items = body.items;
      state.total = body.total;
    } catch (err) {
      if (seq !== requestSeq) return;
      state.error = err.message;
    } finally {
      if (seq === requestSeq) {
        state.loading = false;
        render();
      }
    }
  }

  function cardEl(item) {
    const badge = el("span", { class: `badge badge-${item.path}`, text: item.path || "unknown" });
    const tags = item.tags.map((t) => el("span", { class: "tag", text: t }));
    const meta = el(
      "div",
      { class: "card-meta" },
      el("span", { text: formatDate(item.started_at) }),
      el("span", { text: plural(item.citation_count, "citation") }),
      item.has_pdf ? el("span", { class: "pdf-mark", text: "PDF" }) : null,
    );
    const title = item.title || item.query || item.run_id;
    return el(
      "article",
      {
        class: "card",
        tabindex: "0",
        role: "button",
        "aria-label": `Open report: ${title}`,
        onclick: () => {
          window.location.hash = `#/report/${encodeURIComponent(item.run_id)}`;
        },
        onkeydown: (e) => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            window.location.hash = `#/report/${encodeURIComponent(item.run_id)}`;
          }
        },
      },
      el(
        "div",
        { class: "card-title-row" },
        el("h3", { class: "card-title", text: title }),
        badge,
      ),
      meta,
      item.snippet ? el("p", { class: "card-snippet", text: item.snippet }) : null,
      tags.length ? el("div", { class: "card-tags" }, ...tags) : null,
    );
  }

  function renderTags() {
    clear(tagList);
    if (!state.tags.length) {
      tagList.append(el("p", { class: "hint", text: "No tags yet." }));
      return;
    }
    for (const { tag, count } of state.tags) {
      const active = state.tag === tag;
      const btn = el(
        "button",
        {
          class: active ? "chip chip-active" : "chip",
          type: "button",
          "aria-pressed": active ? "true" : "false",
          onclick: () => {
            state.tag = active ? "" : tag;
            renderTags();
            resetAndLoad();
          },
        },
        tag,
        el("span", { class: "chip-count", text: String(count) }),
      );
      tagList.append(btn);
    }
  }

  function statRow(label, value) {
    return el(
      "div",
      { class: "stat-row" },
      el("span", { text: label }),
      el("b", { text: String(value) }),
    );
  }

  function renderStats() {
    getStats()
      .then((s) => {
        clear(statsEl);
        statsEl.append(
          statRow("Reports", s.reports),
          statRow("Artifacts", s.artifacts),
          statRow("Tags", s.tags),
        );
      })
      .catch(() => {});
  }

  function pageList(totalPages) {
    // Returns an array of page numbers to show, with 0 as an ellipsis marker.
    if (totalPages <= MAX_PAGE_BUTTONS) {
      return Array.from({ length: totalPages }, (_, i) => i + 1);
    }
    const current = state.page;
    const out = [1];
    if (current > 3) out.push(0); // leading ellipsis
    const start = Math.max(2, current - 1);
    const end = Math.min(totalPages - 1, current + 1);
    for (let p = start; p <= end; p++) out.push(p);
    if (current < totalPages - 2) out.push(0); // trailing ellipsis
    out.push(totalPages);
    return out;
  }

  function renderPager() {
    clear(pagerEl);
    if (!state.items.length) return;
    const totalPages = Math.max(1, Math.ceil(state.total / state.pageSize));
    if (totalPages < 2) return;

    pagerEl.append(
      el("button", {
        class: "pager-btn",
        type: "button",
        text: "‹ Prev",
        disabled: state.loading || state.page === 1,
        onclick: () => goToPage(state.page - 1),
      }),
    );
    for (const p of pageList(totalPages)) {
      if (p === 0) {
        pagerEl.append(el("span", { class: "pager-ellipsis", text: "…" }));
        continue;
      }
      pagerEl.append(
        el("button", {
          class: p === state.page ? "pager-btn pager-btn-active" : "pager-btn",
          type: "button",
          "aria-current": p === state.page ? "page" : undefined,
          text: String(p),
          disabled: state.loading,
          onclick: () => goToPage(p),
        }),
      );
    }
    pagerEl.append(
      el("button", {
        class: "pager-btn",
        type: "button",
        text: "Next ›",
        disabled: state.loading || state.page === totalPages,
        onclick: () => goToPage(state.page + 1),
      }),
    );
  }

  function render() {
    clear(summaryEl);
    clear(listEl);
    clear(statusEl);
    clear(pagerEl);

    if (state.items.length) {
      const first = (state.page - 1) * state.pageSize + 1;
      const last = Math.min(state.total, state.page * state.pageSize);
      summaryEl.append(
        el("span", { class: "summary-text", text: `Showing ${first}–${last} of ${state.total} reports` }),
      );
    } else if (!state.loading && state.total) {
      // The requested page is out of range (e.g. filters changed elsewhere);
      // clamp back to the last valid page — but only when it actually differs,
      // otherwise this branch would re-trigger load() forever.
      const clamped = Math.max(1, Math.ceil(state.total / state.pageSize));
      if (clamped !== state.page) {
        state.page = clamped;
        load();
      }
      return;
    }
    if (state.q || state.tag || state.path) {
      summaryEl.append(el("button", { class: "link-btn", type: "button", text: "Clear filters", onclick: clearFilters }));
    }

    if (state.error) {
      statusEl.append(el("p", { class: "error", text: state.error }));
    } else if (state.loading && !state.items.length) {
      statusEl.append(el("p", { class: "hint", text: "Loading…" }));
    } else if (!state.items.length) {
      statusEl.append(el("p", { class: "hint", text: "No reports match your filters." }));
    }

    for (const item of state.items) listEl.append(cardEl(item));

    renderPager();
  }

  renderTags();
  renderStats();
  getTags()
    .then((tags) => {
      state.tags = tags;
      renderTags();
    })
    .catch(() => {});
  load();

  return cleanup;
}
