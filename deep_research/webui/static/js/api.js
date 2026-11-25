/* Thin fetch wrapper for the library API. */

// Routes that may legitimately return 401 without triggering a login redirect.
const AUTH_ROUTES = /^#\/(login|verify|reset)\b/;
const PUBLIC_API = /^\/api\/auth\//;

export async function request(path, { params = {}, method = "GET", body, headers = {} } = {}) {
  const url = new URL(path, window.location.origin);
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    url.searchParams.set(key, String(value));
  }
  const res = await fetch(url, {
    method,
    // X-Requested-With is the CSRF signal the server checks on every
    // state-changing call for an authenticated session (cross-site forms and
    // fetches cannot set a custom header).
    headers: { Accept: "application/json", "X-Requested-With": "XMLHttpRequest", ...headers },
    body,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const errBody = await res.json();
      if (errBody && errBody.detail) detail = errBody.detail;
    } catch {
      /* keep statusText */
    }
    // Session expired / revoked: drop the app-level authed flag and move to
    // the login view. dispatching `dr:auth` makes app.js re-probe
    // /api/auth/status — without it `authed` stays true and render() bounces
    // the hash back to "#/", producing an infinite redirect loop.
    if (
      res.status === 401 &&
      !PUBLIC_API.test(path) &&
      !AUTH_ROUTES.test(window.location.hash)
    ) {
      window.location.hash = "#/login";
      document.dispatchEvent(new CustomEvent("dr:auth"));
    }
    throw new Error(`${res.status}: ${detail}`);
  }
  // 204 / empty bodies carry no JSON; return null so callers don't get a
  // confusing SyntaxError from res.json().
  return res.status === 204 ? null : res.json();
}

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

export function deleteReportReference(runId, url, arxivId) {
  return request(`/api/reports/${encodeURIComponent(runId)}/references`, {
    method: "DELETE",
    params: { confirm: "true" },
    body: JSON.stringify({ url, arxiv_id: arxivId || null }),
    headers: { "Content-Type": "application/json" },
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

/* ---------- Account / auth ---------- */

export function getMe() {
  return request("/api/auth/me");
}

/* Always-200 probe used by the app boot sequence (no 401 console noise). */
export function getAuthStatus() {
  return request("/api/auth/status");
}

export function login(email, password) {
  return request("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ email, password }),
    headers: { "Content-Type": "application/json" },
  });
}

export function register(email, password, displayName) {
  return request("/api/auth/register", {
    method: "POST",
    body: JSON.stringify({ email, password, display_name: displayName || null }),
    headers: { "Content-Type": "application/json" },
  });
}

export function verifyEmail(email, token) {
  return request("/api/auth/verify", {
    method: "POST",
    body: JSON.stringify({ email, token }),
    headers: { "Content-Type": "application/json" },
  });
}

export function resendVerification(email) {
  return request("/api/auth/resend-verification", {
    method: "POST",
    body: JSON.stringify({ email }),
    headers: { "Content-Type": "application/json" },
  });
}

export function logout() {
  return request("/api/auth/logout", { method: "POST" });
}

export function requestPasswordReset(email) {
  return request("/api/auth/request-password-reset", {
    method: "POST",
    body: JSON.stringify({ email }),
    headers: { "Content-Type": "application/json" },
  });
}

export function resetPassword(email, token, newPassword) {
  return request("/api/auth/reset", {
    method: "POST",
    body: JSON.stringify({ email, token, new_password: newPassword }),
    headers: { "Content-Type": "application/json" },
  });
}

/* ---------- Sharing (view-only report shares) ---------- */

export function createShare(runId, withEmails, expiresInDays) {
  return request("/api/shares", {
    method: "POST",
    body: JSON.stringify({
      run_id: runId,
      with_emails: withEmails || [],
      expires_in_days: expiresInDays || null,
    }),
    headers: { "Content-Type": "application/json" },
  });
}

export function listMyShares() {
  return request("/api/shares");
}

export function revokeShare(shareId) {
  return request(`/api/shares/${encodeURIComponent(shareId)}`, { method: "DELETE" });
}

export function rotateShare(shareId) {
  return request(`/api/shares/${encodeURIComponent(shareId)}/rotate`, { method: "POST" });
}

export function addShareGrants(shareId, emails) {
  return request(`/api/shares/${encodeURIComponent(shareId)}/grants`, {
    method: "POST",
    body: JSON.stringify({ emails }),
    headers: { "Content-Type": "application/json" },
  });
}

export function removeShareGrant(shareId, granteeUserId) {
  return request(
    `/api/shares/${encodeURIComponent(shareId)}/grants/${encodeURIComponent(granteeUserId)}`,
    { method: "DELETE" },
  );
}

export function listSharedWithMe() {
  return request("/api/shared");
}

export function getSharedReport(shareId) {
  return request(`/api/shared/${encodeURIComponent(shareId)}`);
}

export function getShareByToken(token) {
  return request(`/api/share/${encodeURIComponent(token)}`);
}
