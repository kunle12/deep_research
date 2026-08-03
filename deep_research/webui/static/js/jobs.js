/* Global research-job tracker.
 *
 * Single source of truth for every research job the UI knows about. It owns
 * one EventSource per running job, keeps a replayable event feed per job, and
 * survives navigation + reloads (server replays each job's event log on
 * subscribe). The taskbar and the detail dialog both render from this state.
 */

import { cancelJob, getJob, listJobs, researchStreamUrl } from "./api.js";

const TERMINAL = new Set(["done", "error", "cancelled"]);
const ACTIVE = new Set(["running", "cancelling"]);
const STORE_KEY = "dr.dismissedJobs";

/** @type {Map<string, object>} job_id -> job record */
const jobs = new Map();
/** @type {Map<string, EventSource>} job_id -> open stream */
const streams = new Map();
/** @type {Set<string>} job_ids the user dismissed from the taskbar */
let dismissed = loadDismissed();

function loadDismissed() {
  try {
    const raw = sessionStorage.getItem(STORE_KEY);
    return new Set(raw ? JSON.parse(raw) : []);
  } catch {
    return new Set();
  }
}

function saveDismissed() {
  try {
    sessionStorage.setItem(STORE_KEY, JSON.stringify([...dismissed]));
  } catch {
    /* storage unavailable — dismissal is best-effort */
  }
}

function notify() {
  document.dispatchEvent(new CustomEvent("dr:jobs", { detail: { jobs: visibleJobs() } }));
}

/** Jobs shown in the taskbar: everything not dismissed, running first. */
export function visibleJobs() {
  return [...jobs.values()]
    .filter((j) => !dismissed.has(j.job_id))
    .sort((a, b) => {
      const ar = ACTIVE.has(a.status) ? 0 : 1;
      const br = ACTIVE.has(b.status) ? 0 : 1;
      if (ar !== br) return ar - br;
      return (b.started_at || 0) - (a.started_at || 0);
    });
}

export function getTrackedJob(jobId) {
  return jobs.get(jobId) || null;
}

export function hasActiveJob() {
  return [...jobs.values()].some((j) => ACTIVE.has(j.status));
}

export function isActiveStatus(status) {
  return ACTIVE.has(status);
}

/** Human elapsed since start (or until completion), e.g. "2m 04s". */
export function fmtElapsed(job) {
  const end = job.completed_at || Date.now() / 1000;
  let s = Math.max(0, Math.floor(end - (job.started_at || end)));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  s %= 60;
  if (h) return `${h}h ${String(m).padStart(2, "0")}m`;
  if (m) return `${m}m ${String(s).padStart(2, "0")}s`;
  return `${s}s`;
}

function blankJob(jobId, query) {
  return {
    job_id: jobId,
    query: query || "",
    status: "running",
    phase: "",
    step: "",
    detail: "",
    started_at: null,
    completed_at: null,
    run_id: null,
    archived: false,
    error: null,
    events: [],
  };
}

function applyEvent(job, event) {
  if (event.ts && !job.started_at) job.started_at = event.ts;
  switch (event.type) {
    case "status":
      job.status = event.status;
      job.phase = event.phase || job.phase;
      job.step = event.step || job.step;
      job.detail = event.detail || job.detail;
      job.run_id = event.run_id ?? job.run_id;
      job.archived = event.archived ?? job.archived;
      job.error = event.error ?? job.error;
      return; // snapshot only — not a feed line
    case "phase":
      job.phase = event.phase;
      job.detail = event.detail || "";
      break;
    case "step":
      job.step = event.step;
      job.detail = event.detail || "";
      break;
    case "done":
      job.status = "done";
      job.run_id = event.run_id ?? job.run_id;
      job.archived = event.archived ?? job.archived;
      job.completed_at = job.completed_at || Date.now() / 1000;
      break;
    case "error":
      job.status = "failed";
      job.error = event.error || "Unknown error";
      job.completed_at = job.completed_at || Date.now() / 1000;
      break;
    case "cancelled":
      job.status = "cancelled";
      job.completed_at = job.completed_at || Date.now() / 1000;
      break;
    case "cancelling":
      job.status = "cancelling";
      return; // status transition only — not a feed line
    default:
      return;
  }
  job.events.push({ ts: event.ts || Date.now() / 1000, ...event });
}

function attachStream(jobId) {
  if (streams.has(jobId)) return;
  const job = jobs.get(jobId);
  if (!job) return;
  const es = new EventSource(researchStreamUrl(jobId));
  streams.set(jobId, es);

  es.onmessage = (msg) => {
    let event;
    try {
      event = JSON.parse(msg.data);
    } catch {
      return;
    }
    applyEvent(job, event);
    notify();
    if (TERMINAL.has(event.type)) {
      closeStream(jobId);
    }
  };
  es.onerror = () => {
    if (!streams.has(jobId)) return; // stream already closed (terminal event raced)
    // Runs on every error; EventSource auto-reconnects on its own. A 404 means
    // the server dropped the job (restart/prune) — mark it lost instead of
    // retrying a dead URL forever.
    getJob(jobId)
      .then((status) => {
        if (!TERMINAL.has(status.status === "failed" ? "error" : status.status)) return;
        applyEvent(job, {
          type: status.status === "done" ? "done" : status.status === "failed" ? "error" : "cancelled",
          run_id: status.run_id,
          archived: status.archived,
          error: status.error,
        });
        notify();
        closeStream(jobId);
      })
      .catch((err) => {
        if (!String(err.message).startsWith("404")) return; // transient — keep retrying
        closeStream(jobId);
        if (!TERMINAL.has(job.status)) {
          job.status = "lost";
          job.events.push({ ts: Date.now() / 1000, type: "lost" });
          notify();
        }
      });
  };
}

function closeStream(jobId) {
  const es = streams.get(jobId);
  if (es) {
    es.close();
    streams.delete(jobId);
  }
}

/** Start tracking a freshly created job (from the research modal). */
export function trackJob(jobId, query) {
  const job = blankJob(jobId, query);
  jobs.set(jobId, job);
  dismissed.delete(jobId);
  notify();
  attachStream(jobId);
  return job;
}

/** Restore jobs after a reload: fetch server state, re-stream running ones. */
export async function restoreJobs() {
  let items;
  try {
    items = await listJobs();
  } catch {
    return; // server unavailable — nothing to restore
  }
  for (const item of items) {
    if (jobs.has(item.job_id) || dismissed.has(item.job_id)) continue;
    const job = blankJob(item.job_id, item.query);
    job.status = item.status;
    job.phase = item.phase;
    job.step = item.step;
    job.detail = item.detail;
    job.started_at = item.started_at || job.started_at;
    job.completed_at = item.completed_at;
    job.run_id = item.run_id;
    job.archived = item.archived;
    job.error = item.error;
    jobs.set(item.job_id, job);
    if (ACTIVE.has(item.status)) {
      // Subscribe replays the event log, rebuilding the feed.
      attachStream(item.job_id);
    }
  }
  notify();
}

export async function cancelTrackedJob(jobId) {
  try {
    await cancelJob(jobId);
  } catch {
    /* the SSE stream / status poll surfaces the outcome */
  }
}

export function dismissJob(jobId) {
  dismissed.add(jobId);
  saveDismissed();
  closeStream(jobId);
  jobs.delete(jobId);
  notify();
}
