/* Dependency-free, safe markdown renderer.
 *
 * parse(source) / parseInline(text) produce a pure AST — no DOM, no globals —
 * so the parser can be unit-tested under Node.
 * renderMarkdown(source) turns the AST into DOM nodes. Text is only ever
 * inserted via textContent / createTextNode; raw markdown is never fed to
 * innerHTML, and links are restricted to safe protocols.
 */

import { el } from "./dom.js";

const URL_RE = /https?:\/\/[^\s<>()]+/g;
const SAFE_PROTOCOLS = new Set(["http:", "https:", "mailto:"]);

export function safeUrl(url) {
  try {
    const parsed = new URL(url, window.location.origin);
    if (SAFE_PROTOCOLS.has(parsed.protocol)) return parsed.href;
  } catch {
    /* fall through */
  }
  return "#";
}

function slugify(text) {
  const slug = text
    .toLowerCase()
    .trim()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 80);
  return slug || "section";
}

function splitUrls(text) {
  const parts = [];
  let last = 0;
  URL_RE.lastIndex = 0;
  let match;
  while ((match = URL_RE.exec(text)) !== null) {
    if (match.index > last) parts.push({ type: "text", text: text.slice(last, match.index) });
    const url = match[0].replace(/[.,;:!?)]+$/, "");
    parts.push({ type: "link", label: [{ type: "text", text: url }], url });
    last = match.index + match[0].length;
  }
  if (last < text.length) parts.push({ type: "text", text: text.slice(last) });
  if (!parts.length && text) parts.push({ type: "text", text });
  return parts;
}

function expandAutolinks(tokens) {
  const out = [];
  for (const token of tokens) {
    if (token.type !== "text") {
      out.push(token);
      continue;
    }
    for (const part of splitUrls(token.text)) out.push(part);
  }
  const merged = [];
  for (const token of out) {
    const last = merged[merged.length - 1];
    if (last && last.type === "text" && token.type === "text") last.text += token.text;
    else merged.push(token);
  }
  return merged;
}

export function parseInline(text) {
  const tokens = [];
  let i = 0;
  const n = text.length;
  while (i < n) {
    const ch = text[i];

    if (ch === "`") {
      const end = text.indexOf("`", i + 1);
      if (end !== -1) {
        tokens.push({ type: "code", text: text.slice(i + 1, end) });
        i = end + 1;
        continue;
      }
    }

    if (ch === "$") {
      const display = text.startsWith("$$", i);
      const delim = display ? "$$" : "$";
      const end = text.indexOf(delim, i + delim.length);
      if (end !== -1) {
        tokens.push({ type: "math", text: text.slice(i + delim.length, end), display });
        i = end + delim.length;
        continue;
      }
    }

    if (text.startsWith("**", i) || text.startsWith("__", i)) {
      const delim = text.slice(i, i + 2);
      const end = text.indexOf(delim, i + 2);
      if (end !== -1) {
        tokens.push({ type: "strong", children: parseInline(text.slice(i + 2, end)) });
        i = end + 2;
        continue;
      }
    }

    if (text.startsWith("~~", i)) {
      const end = text.indexOf("~~", i + 2);
      if (end !== -1) {
        tokens.push({ type: "del", children: parseInline(text.slice(i + 2, end)) });
        i = end + 2;
        continue;
      }
    }

    if (ch === "*" || ch === "_") {
      const end = text.indexOf(ch, i + 1);
      if (end !== -1) {
        tokens.push({ type: "em", children: parseInline(text.slice(i + 1, end)) });
        i = end + 1;
        continue;
      }
    }

    if (text.startsWith("![", i)) {
      const close = text.indexOf("]", i + 2);
      if (close !== -1 && text[close + 1] === "(") {
        const end = text.indexOf(")", close + 2);
        if (end !== -1) {
          tokens.push({
            type: "image",
            alt: text.slice(i + 2, close),
            url: text.slice(close + 2, end).trim(),
          });
          i = end + 1;
          continue;
        }
      }
    }

    if (ch === "[") {
      const close = text.indexOf("]", i + 1);
      if (close !== -1 && text[close + 1] === "(") {
        const end = text.indexOf(")", close + 2);
        if (end !== -1) {
          const label = text.slice(i + 1, close);
          const url = text.slice(close + 2, end).trim();
          tokens.push({
            type: "link",
            label: label ? parseInline(label) : [{ type: "text", text: url }],
            url,
          });
          i = end + 1;
          continue;
        }
      }
    }

    let j = i + 1;
    while (j < n && !"`$*_~[!".includes(text[j])) j++;
    tokens.push({ type: "text", text: text.slice(i, j) });
    i = j;
  }
  return expandAutolinks(tokens);
}

function textOf(tokens) {
  let out = "";
  for (const token of tokens) {
    if (token.type === "text") out += token.text;
    else if (token.children) out += textOf(token.children);
    else if (token.label) out += textOf(token.label);
  }
  return out;
}

function indentOf(line) {
  const m = line.match(/^[ \t]*/);
  return m ? m[0].length : 0;
}

function isBlockStart(line) {
  const t = line.trim();
  if (!t) return true;
  if (/^#{1,6}\s/.test(t)) return true;
  if (/^ {0,3}(```|~~~)/.test(line)) return true;
  if (/^(\s*)([-*+]|\d+\.)\s+/.test(line)) return true;
  if (/^ {0,3}((\* *){3,}|(- *){3,}|(_ *){3,})$/.test(line)) return true;
  if (t.startsWith(">")) return true;
  return false;
}

function parseList(lines, start, kind) {
  const baseIndent = indentOf(lines[start]);
  const items = [];
  let i = start;
  const itemRe = kind === "ordered" ? /^(\s*)\d+\.\s+(.*)$/ : /^(\s*)[-*+]\s+(.*)$/;
  while (i < lines.length) {
    const m = lines[i].match(itemRe);
    if (!m) break;
    const indent = m[1].length;
    if (indent < baseIndent || indent > baseIndent) break;
    const itemLines = [m[2]];
    i++;
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) {
        itemLines.push("");
        i++;
        continue;
      }
      const sub = l.match(/^(\s*)([-*+]|\d+\.)\s+/);
      if (sub) {
        if (sub[1].length > baseIndent) {
          itemLines.push(l);
          i++;
          continue;
        }
        break;
      }
      if (indentOf(l) > baseIndent) {
        itemLines.push(l);
        i++;
        continue;
      }
      break;
    }
    items.push({ blocks: parse(itemLines.join("\n")) });
  }
  return { block: { type: "list", kind, items }, nextIndex: i };
}

function splitRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

export function parse(source) {
  const lines = String(source ?? "").replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let i = 0;

  while (i < lines.length) {
    const raw = lines[i];
    const line = raw.trim();
    if (!line) {
      i++;
      continue;
    }

    const fence = raw.match(/^ {0,3}(`{3,}|~{3,})(.*)$/);
    if (fence) {
      const marker = fence[1][0];
      const buf = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(marker)) {
        buf.push(lines[i]);
        i++;
      }
      if (i < lines.length) i++;
      blocks.push({ type: "code", lang: fence[2].trim(), text: buf.join("\n") });
      continue;
    }

    const heading = raw.match(/^ {0,3}(#{1,6})\s+(.*)$/);
    if (heading) {
      blocks.push({
        type: "heading",
        level: heading[1].length,
        tokens: parseInline(heading[2]),
      });
      i++;
      continue;
    }

    if (/^ {0,3}((\* *){3,}|(- *){3,}|(_ *){3,})$/.test(raw)) {
      blocks.push({ type: "hr" });
      i++;
      continue;
    }

    if (line.startsWith(">")) {
      const buf = [];
      while (i < lines.length && lines[i].trim().startsWith(">")) {
        buf.push(lines[i].trim().replace(/^>\s?/, ""));
        i++;
      }
      blocks.push({ type: "blockquote", blocks: parse(buf.join("\n")) });
      continue;
    }

    if (line.includes("|") && i + 1 < lines.length) {
      const sep = lines[i + 1].trim();
      if (/^\|?[\s:|-]+\|?\s*$/.test(sep) && sep.includes("-")) {
        const rows = [];
        while (i < lines.length && lines[i].trim().includes("|")) {
          rows.push(splitRow(lines[i]));
          i++;
        }
        blocks.push({ type: "table", headers: rows[0], rows: rows.slice(2) });
        continue;
      }
    }

    const ul = raw.match(/^(\s*)[-*+]\s+(.*)$/);
    const ol = raw.match(/^(\s*)\d+\.\s+(.*)$/);
    if (ul || ol) {
      const parsed = parseList(lines, i, ol ? "ordered" : "unordered");
      blocks.push(parsed.block);
      i = parsed.nextIndex;
      continue;
    }

    const para = [];
    while (i < lines.length) {
      const l = lines[i];
      if (!l.trim()) break;
      if (
        para.length === 1 &&
        /^ {0,3}(=+|-+)\s*$/.test(l) &&
        !/^(\s*)([-*+]|\d+\.)\s/.test(para[0])
      ) {
        blocks.push({
          type: "heading",
          level: l.trim()[0] === "=" ? 1 : 2,
          tokens: parseInline(para[0].trim()),
        });
        i++;
        para.length = 0;
        break;
      }
      if (isBlockStart(l)) break;
      para.push(l);
      i++;
    }
    if (para.length) {
      blocks.push({ type: "paragraph", tokens: parseInline(para.join(" ").trim()) });
    }
  }
  return blocks;
}

function renderToken(token) {
  switch (token.type) {
    case "text":
      return document.createTextNode(token.text);
    case "strong":
      return el("strong", {}, ...renderTokens(token.children));
    case "em":
      return el("em", {}, ...renderTokens(token.children));
    case "del":
      return el("del", {}, ...renderTokens(token.children));
    case "code":
      return el("code", { class: "md-inline-code", text: token.text });
    case "math":
      return el("code", {
        class: token.display ? "md-math md-math-display" : "md-math",
        text: token.text,
      });
    case "link":
      return el(
        "a",
        { href: safeUrl(token.url), target: "_blank", rel: "noopener noreferrer" },
        ...renderTokens(token.label),
      );
    case "image":
      return el(
        "a",
        {
          href: safeUrl(token.url),
          target: "_blank",
          rel: "noopener noreferrer",
          title: token.alt || undefined,
        },
        el("span", { class: "md-image", text: `[image] ${token.alt || "image"}` }),
      );
    default:
      return document.createTextNode("");
  }
}

function renderTokens(tokens) {
  return tokens.map(renderToken);
}

function renderBlock(block) {
  switch (block.type) {
    case "heading": {
      const level = Math.min(block.level, 6);
      return el(
        `h${level}`,
        { class: "md-heading", id: slugify(textOf(block.tokens)), dataset: { heading: "true" } },
        ...renderTokens(block.tokens),
      );
    }
    case "paragraph":
      return el("p", {}, ...renderTokens(block.tokens));
    case "hr":
      return el("hr", {});
    case "code":
      return el(
        "pre",
        { class: "md-code" },
        el("button", {
          class: "code-copy",
          type: "button",
          text: "Copy",
          onclick: async (event) => {
            try {
              await navigator.clipboard.writeText(block.text);
              event.target.textContent = "Copied!";
              setTimeout(() => {
                event.target.textContent = "Copy";
              }, 1500);
            } catch {
              /* clipboard unavailable — no-op */
            }
          },
        }),
        el("code", { text: block.text }),
      );
    case "blockquote":
      return el("blockquote", {}, ...renderBlocks(block.blocks));
    case "list": {
      const tag = block.kind === "ordered" ? "ol" : "ul";
      const items = block.items.map((item) => {
        const children = [];
        const first = item.blocks[0];
        if (first && first.type === "paragraph") {
          children.push(...renderTokens(first.tokens));
          for (const rest of item.blocks.slice(1)) children.push(renderBlock(rest));
        } else {
          for (const rest of item.blocks) children.push(renderBlock(rest));
        }
        return el("li", {}, ...children);
      });
      return el(tag, {}, ...items);
    }
    case "table": {
      const table = el("table", { class: "md-table" });
      table.append(
        el(
          "thead",
          {},
          el(
            "tr",
            {},
            ...block.headers.map((h) => el("th", {}, ...renderTokens(parseInline(h)))),
          ),
        ),
      );
      const tbody = el("tbody", {});
      for (const row of block.rows) {
        tbody.append(
          el(
            "tr",
            {},
            ...row.map((cell) => el("td", {}, ...renderTokens(parseInline(cell)))),
          ),
        );
      }
      table.append(tbody);
      return table;
    }
    default:
      return null;
  }
}

export function renderBlocks(blocks) {
  return blocks.map(renderBlock).filter(Boolean);
}

export function renderMarkdown(source) {
  return renderBlocks(parse(source));
}
