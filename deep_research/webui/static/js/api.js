/* Thin fetch wrapper for the library API. */

export async function request(path, { params = {}, method = "GET", body, headers = {} } = {}) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    url.searchParams.set(key, String(value));
  }
  const res = await fetch(url, {
    method,
    headers: { Accept: "application/json", ...headers },
    body,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && body.detail) detail = body.detail;
    } catch {
      /* keep statusText */
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  return res.json();
}

export const getJSON = request;

export function listReports(params) {
  return request("/api/reports", { params });
}

export function getTags(params) {
  return request("/api/tags", { params });
}

export function getStats() {
  return request("/api/stats");
}

export function getReport(runId) {
  return request(`/api/reports/${encodeURIComponent(runId)}`);
}

export function addReportTag(runId, tag) {
  return request(`/api/reports/${encodeURIComponent(runId)}/tags`, {
    method: "POST",
    body: JSON.stringify({ tag }),
    headers: { "Content-Type": "application/json" },
  });
}

export function removeReportTag(runId, tag) {
  return request(`/api/reports/${encodeURIComponent(runId)}/tags`, {
    method: "DELETE",
    params: { tag },
  });
}

export function deleteReport(runId) {
  return request(`/api/reports/${encodeURIComponent(runId)}`, {
    method: "DELETE",
    params: { confirm: "true" },
  });
}

export function renameReport(runId, query) {
  return request(`/api/reports/${encodeURIComponent(runId)}`, {
    method: "PATCH",
    body: JSON.stringify({ query }),
    headers: { "Content-Type": "application/json" },
  });
}

export function mergeReports(runId, otherRunIds, name, deleteSources) {
  return request(`/api/reports/${encodeURIComponent(runId)}/merge`, {
    method: "POST",
    body: JSON.stringify({
      other_run_ids: otherRunIds,
      name: name || null,
      delete_sources: Boolean(deleteSources),
    }),
    headers: { "Content-Type": "application/json" },
  });
}

export function startResearch(query, pathOverride, attachToRunId) {
  return request("/api/research", {
    method: "POST",
    body: JSON.stringify({
      query,
      path_override: pathOverride || null,
      attach_to_run_id: attachToRunId || null,
    }),
    headers: { "Content-Type": "application/json" },
  });
}

export function getJob(jobId) {
  return request(`/api/research/jobs/${encodeURIComponent(jobId)}`);
}

export function listJobs() {
  return request("/api/research/jobs");
}

export function fetchArxivPdf(arxivId) {
  return request("/api/arxiv/pdf", {
    method: "POST",
    body: JSON.stringify({ arxiv_id: arxivId }),
    headers: { "Content-Type": "application/json" },
  });
}

export function cancelJob(jobId) {
  return request(`/api/research/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
  });
}

export function pauseJob(jobId) {
  return request(`/api/research/jobs/${encodeURIComponent(jobId)}/pause`, {
    method: "POST",
  });
}

export function resumeJob(jobId) {
  return request(`/api/research/jobs/${encodeURIComponent(jobId)}/resume`, {
    method: "POST",
  });
}

export function abandonJob(jobId) {
  return request(`/api/research/jobs/${encodeURIComponent(jobId)}/abandon`, {
    method: "POST",
  });
}

export function researchStreamUrl(jobId) {
  return `/api/research/jobs/${encodeURIComponent(jobId)}/stream`;
}
