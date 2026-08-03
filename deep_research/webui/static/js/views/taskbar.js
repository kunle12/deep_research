/* Bottom taskbar: compact live progress for research jobs.
 *
 * One row per visible job. Clicking a row opens the detail dialog; the ✕
 * cancels an active job or dismisses a finished one. Closing the dialog (or
 * navigating) never stops the job — the taskbar keeps updating.
 *
 * Rows are updated in place (not rebuilt per event) so entrance/state
 * animations play once instead of flickering on every SSE update.
 */

import { el, clear } from "../dom.js";
import {
  abandonTrackedJob,
  cancelTrackedJob,
  dismissJob,
  fmtElapsed,
  getTrackedJob,
  isActiveStatus,
  visibleJobs,
} from "../jobs.js";
import { openJobDialog } from "./jobDialog.js";

const STATUS_LABEL = {
  running: "Researching",
  cancelling: "Cancelling",
  paused: "Paused",
  done: "Complete",
  failed: "Failed",
  cancelled: "Cancelled",
  lost: "Lost",
};

const STATUS_ICON = {
  paused: "❚❚",
  done: "✓",
  failed: "✕",
  cancelled: "–",
  lost: "!",
};

let bar = null;
let ticker = null;
/** @type {Map<string, object>} job_id -> live row entry */
const rows = new Map();

export function initTaskbar() {
  bar = el("div", { class: "taskbar", hidden: true });
  document.body.append(bar);
  document.addEventListener("dr:jobs", render);
  render();
}

function subText(job) {
  if (job.status === "done") {
    return job.archived
      ? "Report saved to the library — click for details"
      : "Report finished (not archived) — click for details";
  }
  if (job.status === "failed") return job.error || "Unknown error";
  if (job.status === "cancelled") return "The job was cancelled";
  if (job.status === "lost") return "Connection lost — the job is no longer available";
  const label = job.step || job.phase;
  if (!label) return "Starting…";
  return job.detail ? `${label} — ${job.detail}` : label;
}

function makeRow(job) {
  const entry = { job_id: job.job_id, active: null, iconState: null, status: null, sub: null };

  entry.iconHolder = el("span", { class: "taskbar-iconslot", "aria-hidden": "true" });
  entry.statusEl = el("span", { class: "taskbar-status" });
  entry.line2El = el("div", { class: "taskbar-line2" });
  entry.elapsedEl = el("span", { class: "taskbar-elapsed" });
  entry.closeBtn = el("button", {
    class: "taskbar-close",
    type: "button",
    text: "✕",
    onclick: (event) => {
      event.stopPropagation();
      const current = getTrackedJob(entry.job_id);
      if (!current) return;
      if (isActiveStatus(current.status)) cancelTrackedJob(current.job_id);
      else if (current.status === "paused") abandonTrackedJob(current.job_id);
      else dismissJob(current.job_id);
    },
  });

  entry.row = el(
    "div",
    {
      class: "taskbar-row",
      role: "button",
      tabindex: 0,
      dataset: { jobId: job.job_id },
      "aria-label": `Research progress: ${job.query || job.job_id}`,
      onclick: () => openJobDialog(entry.job_id),
      onkeydown: (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openJobDialog(entry.job_id);
        }
      },
    },
    entry.iconHolder,
    el(
      "div",
      { class: "taskbar-text" },
      el("div", { class: "taskbar-line1" }, entry.statusEl, el("span", { class: "taskbar-query", text: job.query || job.job_id })),
      entry.line2El,
    ),
    entry.elapsedEl,
    entry.closeBtn,
  );
  return entry;
}

function updateRow(entry, job) {
  const active = isActiveStatus(job.status);
  if (active !== entry.active) {
    entry.active = active;
    entry.row.classList.toggle("taskbar-row-active", active);
    const closeTitle =
      active ? "Cancel research" : job.status === "paused" ? "Discard" : "Dismiss";
    entry.closeBtn.title = closeTitle;
    entry.closeBtn.setAttribute("aria-label", closeTitle);
  }

  const iconState = active ? "spinner" : job.status;
  if (iconState !== entry.iconState) {
    entry.iconState = iconState;
    clear(entry.iconHolder);
    if (active) {
      entry.iconHolder.append(el("span", { class: "taskbar-spinner" }));
    } else {
      entry.iconHolder.append(
        el("span", {
          class: `taskbar-icon taskbar-icon-${job.status}`,
          text: STATUS_ICON[job.status] || "•",
        }),
      );
    }
  }

  if (job.status !== entry.status) {
    entry.status = job.status;
    entry.statusEl.className = `taskbar-status taskbar-status-${job.status}`;
    entry.statusEl.textContent = STATUS_LABEL[job.status] || job.status;
  }

  const sub = subText(job);
  if (sub !== entry.sub) {
    entry.sub = sub;
    entry.line2El.textContent = sub;
  }

  entry.elapsedEl.textContent = fmtElapsed(job);
}

function startTicker() {
  if (ticker) return;
  ticker = setInterval(() => {
    for (const entry of rows.values()) {
      const job = getTrackedJob(entry.job_id);
      if (job) entry.elapsedEl.textContent = fmtElapsed(job);
    }
  }, 1000);
}

function stopTicker() {
  if (ticker) {
    clearInterval(ticker);
    ticker = null;
  }
}

function render() {
  if (!bar) return;
  const list = visibleJobs();
  if (!list.length) {
    bar.hidden = true;
    document.body.classList.remove("taskbar-open");
    clear(bar);
    rows.clear();
    stopTicker();
    return;
  }
  bar.hidden = false;
  document.body.classList.add("taskbar-open");

  const seen = new Set();
  list.forEach((job, index) => {
    seen.add(job.job_id);
    let entry = rows.get(job.job_id);
    if (!entry) {
      entry = makeRow(job);
      entry.row.classList.add("taskbar-row-enter");
      rows.set(job.job_id, entry);
    }
    updateRow(entry, job);
    const expected = bar.children[index];
    if (expected !== entry.row) bar.insertBefore(entry.row, expected || null);
  });
  for (const [jobId, entry] of [...rows]) {
    if (!seen.has(jobId)) {
      entry.row.remove();
      rows.delete(jobId);
    }
  }

  // Reserve exactly enough bottom padding for however many rows are visible.
  document.body.style.setProperty("--taskbar-pad", `${bar.offsetHeight + 24}px`);

  if (list.some((j) => isActiveStatus(j.status))) startTicker();
  else stopTicker();
}
