/* List view: filters + paginated report cards. */

import { el, clear } from "../dom.js";
import { formatDate, plural } from "../format.js";
import { listReports, getTags, getStats } from "../api.js";

const PAGE_SIZE = 50;
const PATHS = ["quick", "deep", "academic", "url_source", "applied"];

export function renderList(root, searchInput) {
  clear(root);

  const state = {
    q: searchInput.value.trim(),
    tag: "",
    path: "",
    items: [],
    total: 0,
    offset: 0,
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
  const tagList = el("div", { class: "tag-cloud" });
  const summaryEl = el("div", { class: "filter-summary" });
  const listEl = el("div", { class: "report-list" });
  const statusEl = el("div", { class: "list-status" });
  const loadMoreEl = el("div", { class: "load-more" });
  const sentinel = el("div", { class: "sentinel" });
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
  const main = el("section", { class: "list-main" }, summaryEl, listEl, statusEl, loadMoreEl);
  root.append(el("div", { class: "layout" }, sidebar, main));

  const io = new IntersectionObserver(
    (entries) => {
      if (
        entries[0].isIntersecting &&
        !state.loading &&
        state.items.length > 0 &&
        state.items.length < state.total
      ) {
        load(false);
      }
    },
    { rootMargin: "300px" },
  );
  io.observe(sentinel);
  let requestSeq = 0;

  const onSearch = (event) => {
    state.q = event.detail;
    state.offset = 0;
    load(true);
  };
  document.addEventListener("dr:search", onSearch);

  pathSelect.addEventListener("change", () => {
    state.path = pathSelect.value;
    state.offset = 0;
    load(true);
  });

  function cleanup() {
    document.removeEventListener("dr:search", onSearch);
    io.disconnect();
  }

  function clearFilters() {
    state.q = "";
    state.tag = "";
    state.path = "";
    state.offset = 0;
    searchInput.value = "";
    pathSelect.value = "";
    renderTags();
    load(true);
  }

  async function load(reset) {
    const seq = ++requestSeq;
    const offset = reset ? 0 : state.offset;
    state.loading = true;
    state.error = "";
    render();
    try {
      const body = await listReports({
        limit: PAGE_SIZE,
        offset,
        q: state.q || undefined,
        tag: state.tag || undefined,
        path: state.path || undefined,
      });
      if (seq !== requestSeq) return; // superseded by a newer request
      state.items = reset ? body.items : [...state.items, ...body.items];
      state.total = body.total;
      state.offset = offset + body.items.length;
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
    return el(
      "article",
      {
        class: "card",
        tabindex: "0",
        role: "button",
        "aria-label": `Open report: ${item.query}`,
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
        el("h3", { class: "card-title", text: item.query || item.run_id }),
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
            state.offset = 0;
            renderTags();
            load(true);
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

  function render() {
    clear(summaryEl);
    clear(listEl);
    clear(statusEl);
    clear(loadMoreEl);

    if (state.items.length) {
      summaryEl.append(
        el("span", { class: "summary-text", text: `Showing ${state.items.length} of ${state.total} reports` }),
      );
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

    if (state.items.length < state.total) {
      loadMoreEl.append(
        el("button", {
          class: "btn",
          type: "button",
          text: state.loading ? "Loading…" : "Load more",
          disabled: state.loading,
          onclick: () => load(false),
        }),
      );
    }
    loadMoreEl.append(sentinel);
  }

  renderTags();
  renderStats();
  getTags()
    .then((tags) => {
      state.tags = tags;
      renderTags();
    })
    .catch(() => {});
  load(true);

  return cleanup;
}
