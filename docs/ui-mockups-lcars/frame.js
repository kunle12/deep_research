/* Shared mockup chrome: applies ?theme=light|dark (light default) and injects
   the LCARS frame (top bar with Shared nav / bottom telemetry band).
   Dialog pages pass data-frame="none" to render bare. */

(function () {
  const q = new URLSearchParams(location.search);
  const theme = q.get("theme") === "dark" ? "dark" : "light";
  document.documentElement.dataset.theme = theme;

  function seg(text, cls) { const s = document.createElement("div"); s.className = "seg " + (cls || ""); s.textContent = text; return s; }

  function buildFrame() {
    const d = document.body.dataset;
    const reports = d.reports || "12";
    const artifacts = d.artifacts || "91";
    const tags = d.tags || "23";
    const shared = d.shared || "3";
    const who = d.who || "";
    const brand = d.brand || "XUN · DEEP RESEARCH";

    // Live local time occupies the small sky pill (was an empty stub).
    const now = new Date();
    const hh = String(now.getHours()).padStart(2, "0");
    const mm = String(now.getMinutes()).padStart(2, "0");

    const top = document.createElement("div");
    top.className = "top";
    top.append(seg(brand, "brand"), seg(d.s2 || "PDL 1.0", "s2"),
      seg(d.section || "LIB", "s3"), seg(hh + ":" + mm, "top-clock"));
    const spacer = document.createElement("div"); spacer.className = "spacer"; top.append(spacer);

    const ro = document.createElement("div"); ro.className = "readout";
    ro.innerHTML = '<i class="led"></i>' + reports + " REPORTS · " + artifacts + " ARTIFACTS" +
      (who ? ' · <span class="who">' + who + "</span>" : "");
    top.append(ro);

    const bottom = document.createElement("div"); bottom.className = "bottom";
    bottom.append(
      seg("Library online · " + reports + " reports", "b1"),
      seg("Artifacts " + artifacts, "b2"),
      seg("Tags " + tags, "b3"),
      seg("Shared " + shared, "b4"),
    );

    const consoleEl = document.createElement("div"); consoleEl.className = "console";
    consoleEl.append(top, bottom);
    document.body.append(consoleEl);
  }

  document.addEventListener("DOMContentLoaded", () => {
    if (document.body.dataset.frame !== "none") buildFrame();
  });
})();
