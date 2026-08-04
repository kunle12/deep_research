/* Research progress dialog — the dismissable detail view behind a taskbar row.
 *
 * Shows everything the taskbar row knows (query, status, live phase/step,
 * full event feed) plus actions. Closing the dialog never affects the job.
 *
 * Dynamic parts update in place (badge/elapsed/current line) and the action
 * buttons only rebuild when the job's status changes, so a focused or
 * about-to-be-clicked button is never swapped out from under the user.
 */

import { el, clear } from "../dom.js";
import {
  abandonTrackedJob,
  cancelTrackedJob,
  dismissJob,
  fmtElapsed,
  getTrackedJob,
  isActiveStatus,
  pauseTrackedJob,
  resumeTrackedJob,
} from "../jobs.js";
import { feedLine } from "./feed.js";

const STATUS_LABEL = {
  running: "Researching",
  cancelling: "Cancelling…",
  paused: "Paused",
  done: "Complete",
  failed: "Failed",
  cancelled: "Cancelled",
  lost: "Lost",
};

let current = null; // close() of the open dialog, if any

export function openJobDialog(jobId) {
  const job = getTrackedJob(jobId);
  if (!job) return;
  if (current) current.close();

  const opener = document.activeElement;
  const overlay = el("div", { class: "modal-overlay jobdialog-overlay" });
  const badgeEl = el("span", { class: "badge" });
  const elapsedEl = el("span", { class: "jobdialog-elapsed" });
  const metaEl = el("div", { class: "jobdialog-meta" }, badgeEl, elapsedEl);
  const currentEl = el("div", { class: "jobdialog-current", role: "status", "aria-live": "polite" });
  const feedEl = el("div", { class: "research-feed jobdialog-feed", hidden: "" });
  const actionsEl = el("div", { class: "modal-actions" });

  const dialog = el(
    "div",
    { class: "modal", role: "dialog", "aria-modal": "true", "aria-labelledby": "jobdialog-title" },
    el("h2", { id: "jobdialog-title", class: "modal-title", text: "Research progress" }),
    el("p", { class: "jobdialog-query", text: job.query || job.job_id }),
    metaEl,
    currentEl,
    feedEl,
    actionsEl,
  );
  overlay.append(dialog);
  document.body.append(overlay);

  let closed = false;
  let rendered = 0;
  let ticker = null;
  let lastStatus = null;
  let lastCurrentSig = null;

  function close() {
    if (closed) return;
    closed = true;
    if (ticker) clearInterval(ticker);
    document.removeEventListener("keydown", onKey);
    document.removeEventListener("dr:jobs", onJobs);
    overlay.remove();
    if (opener && opener.isConnected && opener.focus) opener.focus();
    if (current && current.close === close) current = null;
  }

  function onKey(event) {
    if (event.key === "Escape") close();
  }

  function nearBottom() {
    return feedEl.scrollTop + feedEl.clientHeight >= feedEl.scrollHeight - 24;
  }

  function appendFeed(from) {
    const stick = nearBottom();
    for (let i = from; i < job.events.length; i += 1) {
      const line = feedLine(job.events[i]);
      if (line) feedEl.append(line);
    }
    rendered = job.events.length;
    // A fresh job has no events yet — don't leave a useless empty box above the
    // action buttons. Show the feed only once there are lines to display.
    feedEl.hidden = job.events.length === 0;
    if (stick) feedEl.scrollTop = feedEl.scrollHeight;
  }

  function renderActions() {
    clear(actionsEl);
    if (job.status === "done" && job.run_id) {
      actionsEl.append(
        el("button", {
          class: "btn btn-primary",
          type: "button",
          text: "Open report",
          onclick: () => {
            close();
            window.location.hash = `#/report/${encodeURIComponent(job.run_id)}`;
          },
        }),
      );
    }
    if (isActiveStatus(job.status)) {
      actionsEl.append(
        el("button", {
          class: "btn btn-danger",
          type: "button",
          text: job.status === "cancelling" ? "Cancelling…" : "Cancel research",
          disabled: job.status === "cancelling",
          onclick: () => cancelTrackedJob(job.job_id),
        }),
      );
      actionsEl.append(
        el("button", {
          class: "btn",
          type: "button",
          text: "Pause",
          disabled: job.status !== "running",
          onclick: () => pauseTrackedJob(job.job_id),
        }),
      );
    } else if (job.status === "paused") {
      actionsEl.append(
        el("button", {
          class: "btn btn-primary",
          type: "button",
          text: "Resume",
          onclick: () => resumeTrackedJob(job.job_id),
        }),
      );
      actionsEl.append(
        el("button", {
          class: "btn btn-danger",
          type: "button",
          text: "Discard",
          onclick: () => {
            abandonTrackedJob(job.job_id);
            close();
          },
        }),
      );
    } else {
      actionsEl.append(
        el("button", {
          class: "btn",
          type: "button",
          text: "Dismiss",
          onclick: () => {
            dismissJob(job.job_id);
            close();
          },
        }),
      );
    }
    actionsEl.append(el("button", { class: "btn", type: "button", text: "Close", onclick: close }));
  }

  function updateMeta() {
    if (job.status !== lastStatus) {
      lastStatus = job.status;
      badgeEl.className = `badge jobdialog-badge-${job.status}`;
      badgeEl.textContent = STATUS_LABEL[job.status] || job.status;
      renderActions();
    }
    elapsedEl.textContent = `elapsed ${fmtElapsed(job)}`;
  }

  function updateCurrent() {
    const sig = [job.status, job.phase, job.step, job.detail, job.error].join("|");
    if (sig === lastCurrentSig) return;
    lastCurrentSig = sig;
    clear(currentEl);
    if (job.status === "done") {
      currentEl.append(
        el("span", {
          text: job.archived
            ? "Report saved to the library."
            : "Report finished, but not archived (is pdl.enabled set?).",
        }),
      );
    } else if (job.status === "failed") {
      currentEl.append(el("span", { class: "feed-error", text: job.error || "Unknown error" }));
    } else if (job.status === "cancelled") {
      currentEl.append(el("span", { text: "The job was cancelled." }));
    } else if (job.status === "paused") {
      currentEl.append(
        el(
          "span",
          { class: "feed-paused" },
          "Paused — resume from the last checkpoint. Pausing is per-iteration: on resume it re-runs from the last completed research round.",
        ),
      );
    } else if (job.status === "lost") {
      currentEl.append(
        el("span", { text: "Connection lost — the job is no longer available (server restarted?)." }),
      );
    } else {
      const label = job.step || job.phase;
      currentEl.append(
        el(
          "span",
          {},
          el("span", { class: "feed-phase", text: label || "Starting…" }),
          job.detail ? el("span", { class: "feed-detail", text: ` — ${job.detail}` }) : null,
        ),
      );
    }
  }

  function onJobs() {
    if (closed) return;
    if (!getTrackedJob(jobId)) {
      close(); // dismissed elsewhere
      return;
    }
    updateMeta();
    updateCurrent();
    appendFeed(rendered);
    if (isActiveStatus(job.status) && !ticker) {
      ticker = setInterval(() => {
        elapsedEl.textContent = `elapsed ${fmtElapsed(job)}`;
      }, 1000);
    } else if (!isActiveStatus(job.status) && ticker) {
      clearInterval(ticker);
      ticker = null;
    }
  }

  overlay.addEventListener("click", (event) => {
    if (event.target === overlay) close();
  });
  document.addEventListener("keydown", onKey);
  document.addEventListener("dr:jobs", onJobs);

  current = { close };
  updateMeta();
  updateCurrent();
  appendFeed(0);
  feedEl.scrollTop = feedEl.scrollHeight;
  return close;
}
