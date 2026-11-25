/* Share dialog: manage view-only shares for one report.
 *
 * - Create a secret share link (URL shown once; rotate to invalidate).
 * - Grant named accounts by email (they see it under "Shared with me").
 * - Revoke the share or remove individual grantees.
 */

import { el, clear } from "../dom.js";
import {
  addShareGrants,
  appHref,
  createShare,
  listMyShares,
  removeShareGrant,
  revokeShare,
  rotateShare,
} from "../api.js";
import { openModal } from "../modal.js";
import { formatDate } from "../format.js";

export function openShareDialog(report) {
  const { overlay, close } = openModal({});
  const box = el("div", { class: "modal share-modal", role: "dialog", "aria-label": "Share this research" });
  overlay.append(box);

  const err = el("div", { class: "auth-error", hidden: true });
  function showErr(msg) {
    err.textContent = msg;
    err.hidden = false;
  }
  function hideErr() {
    err.hidden = true;
  }

  async function guard(action) {
    hideErr();
    try {
      await action();
    } catch (e) {
      showErr(String(e.message || e).replace(/^\d+:\s*/, ""));
    }
  }

  // The shares list lives across redraws (draw() and drawWithNewLink()).
  const listWrap = el("div", { class: "share-list" });

  function draw() {
    clear(box);
    box.append(
      el("div", { class: "modal-head" },
        el("h2", { class: "modal-title", text: "Share this research" }),
        el("button", { class: "icon-btn", type: "button", text: "×", "aria-label": "Close", onclick: close })),
      el("p", { class: "share-intro", text: `“${(report.title || report.query || report.run_id).slice(0, 90)}” — read-only. Recipients see the report, its references and the archived PDF; they cannot edit your library.` }),
      err,
    );

    const createSection = el("div", { class: "share-create" });
    const emailInput = el("input", {
      class: "text-input",
      type: "text",
      placeholder: "colleague@example.com (optional, comma-separated)",
      autocomplete: "off",
    });
    const expiry = el(
      "select",
      { class: "select select-sm", "aria-label": "Link expiry" },
      el("option", { value: "", text: "No expiry" }),
      el("option", { value: "7", text: "Expires in 7 days" }),
      el("option", { value: "30", text: "Expires in 30 days" }),
      el("option", { value: "90", text: "Expires in 90 days" }),
    );
    const createBtn = el("button", {
      class: "btn btn-primary",
      type: "button",
      text: "Create share link",
      onclick: () =>
        guard(async () => {
          const emails = emailInput.value
            .split(/[,\s]+/)
            .map((s) => s.trim())
            .filter(Boolean);
          const days = expiry.value ? Number(expiry.value) : null;
          const created = await createShare(report.run_id, emails, days);
          emailInput.value = "";
          drawWithNewLink(created);
        }),
    });
    createSection.append(
      el("div", { class: "field-label", text: "Share with people (optional)" }),
      emailInput,
      el("div", { class: "share-create-row" }, expiry, createBtn),
    );
    box.append(createSection);
    box.append(listWrap);
    drawWithNewLink(null);
  }

  function drawWithNewLink(created) {
    // Re-render the list area, flashing the fresh link at the top when given.
    guard(async () => {
      const shares = (await listMyShares()).filter((s) => s.run_id === report.run_id);
      renderList(listWrap, shares, created);
      if (!shares.length) {
        listWrap.append(el("p", { class: "share-empty", text: "Not shared yet." }));
      }
    });
  }

  function renderList(listWrap, shares, created) {
    clear(listWrap);
    if (created) {
      listWrap.append(
        el(
          "div",
          { class: "share-link-box" },
          el("div", { class: "field-label", text: "Share link (copy it now — shown once; you can rotate it later):" }),
          el("div", { class: "share-link-row" },
            el("input", { class: "text-input", type: "text", readonly: true, value: appHref(created.url) }),
            el("button", {
              class: "btn",
              type: "button",
              text: "Copy",
              onclick: (event) => {
                navigator.clipboard
                  ?.writeText(appHref(created.url))
                  .then(() => { event.target.textContent = "Copied!"; })
                  .catch(() => {});
              },
            })),
        ),
      );
    }
    for (const s of shares) {
      listWrap.append(shareRow(s));
    }
  }

  function shareRow(s) {
    const grantsEl = el("div", { class: "share-grants" });
    for (const g of s.grants) {
      grantsEl.append(
        el("span", { class: "tag" },
          g.email,
          el("button", {
            class: "linklike",
            type: "button",
            text: " ×",
            title: `Remove access for ${g.email}`,
            onclick: () => guard(async () => {
              await removeShareGrant(s.share_id, g.user_id);
              drawWithNewLink(null);
            }),
          })),
      );
    }
    if (!s.grants.length) {
      grantsEl.append(el("span", { text: "Link only" }));
    }
    const addInput = el("input", {
      class: "text-input",
      type: "text",
      placeholder: "Add people by email, comma-separated",
      autocomplete: "off",
    });
    const addRow = el(
      "div",
      { class: "share-add-row" },
      addInput,
      el("button", {
        class: "btn",
        type: "button",
        text: "Add",
        onclick: () => guard(async () => {
          const emails = addInput.value
            .split(/[,\s]+/)
            .map((x) => x.trim())
            .filter(Boolean);
          if (!emails.length) return;
          await addShareGrants(s.share_id, emails);
          addInput.value = "";
          drawWithNewLink(null);
        }),
      }),
    );
    return el(
      "div",
      { class: "share-row" },
      el(
        "div",
        { class: "share-row-main" },
        el("div", { class: "share-row-meta" },
          el("span", { text: `Created ${formatDate(s.created_at)}` }),
          s.expires_at ? el("span", { text: ` · expires ${formatDate(s.expires_at)}` }) : null,
        ),
        grantsEl,
        addRow,
      ),
      el(
        "div",
        { class: "share-row-actions" },
        el("button", {
          class: "btn",
          type: "button",
          text: "New link",
          title: "Invalidate the current link and mint a fresh one",
          onclick: () => guard(async () => {
            const rotated = await rotateShare(s.share_id);
            drawWithNewLink(rotated);
          }),
        }),
        el("button", {
          class: "btn btn-danger",
          type: "button",
          text: "Revoke",
          onclick: () => guard(async () => {
            await revokeShare(s.share_id);
            drawWithNewLink(null);
          }),
        }),
      ),
    );
  }

  draw();
}
