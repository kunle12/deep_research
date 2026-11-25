/* Shared-with-me views: the list of reports shared with this account, and a
 * read-only report rendering for one of them (also used by secret links). */

import { el, clear } from "../dom.js";
import { formatDate } from "../format.js";
import { getShareByToken, getSharedReport, listSharedWithMe } from "../api.js";
import { renderReport } from "./report.js";

export function renderSharedList(root) {
  clear(root);
  const wrap = el("div", { class: "shared-wrap" });
  root.append(wrap);
  wrap.append(el("h1", { class: "shared-title", text: "Shared with me" }));
  const listEl = el("div", { class: "report-list" });
  wrap.append(listEl);

  listSharedWithMe()
    .then((items) => {
      clear(listEl);
      if (!items.length) {
        listEl.append(
          el("div", { class: "placeholder" },
            el("p", { text: "Nothing shared with you yet. When someone grants you access to a report, it appears here." })),
        );
        return;
      }
      for (const item of items) {
        listEl.append(
          el(
            "a",
            { class: "card", href: `#/shared/${encodeURIComponent(item.share_id)}` },
            el("div", { class: "card-title-row" },
              el("h3", { class: "card-title", text: item.title || item.run_id }),
              el("span", { class: "badge", text: "shared" })),
            el("div", { class: "card-meta" },
              el("span", { text: `by ${item.owner_email}` }),
              el("span", { text: formatDate(item.created_at) })),
          ),
        );
      }
    })
    .catch((err) => {
      clear(listEl);
      listEl.append(
        el("div", { class: "placeholder" },
          el("p", { class: "error", text: String(err.message || err) }),
          el("a", { href: "#/", text: "← Back to library" })),
      );
    });
  return null;
}

export function renderSharedByGrant(root, shareId) {
  const loader = getSharedReport(shareId).then(async (report) => {
    const items = await listSharedWithMe().catch(() => []);
    const mine = items.find((i) => i.share_id === shareId);
    return { report, banner: `Shared by ${mine ? mine.owner_email : "another account"} — read only`, back: { href: "#/shared", label: "← Shared with me" } };
  });
  return renderReport(root, null, { loader });
}

export function renderSharedByToken(root, token) {
  const loader = getShareByToken(token).then((report) => ({
    report,
    banner: "Shared link — read only",
    back: null,
  }));
  return renderReport(root, null, { loader });
}
