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

export function startResearch(query, pathOverride) {
  return request("/api/research", {
    method: "POST",
    body: JSON.stringify({ query, path_override: pathOverride || null }),
    headers: { "Content-Type": "application/json" },
  });
}

export function getJob(jobId) {
  return request(`/api/research/jobs/${encodeURIComponent(jobId)}`);
}

export function cancelJob(jobId) {
  return request(`/api/research/jobs/${encodeURIComponent(jobId)}/cancel`, {
    method: "POST",
  });
}

export function researchStreamUrl(jobId) {
  return `/api/research/jobs/${encodeURIComponent(jobId)}/stream`;
}
