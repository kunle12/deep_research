/* Tiny DOM helpers — the only "framework" the UI uses. */

// Inline icons are SVG; they must be created in the SVG namespace or the
// browser treats them as unknown HTML elements and paints nothing.
const SVG_NS = "http://www.w3.org/2000/svg";
const SVG_TAGS = new Set([
  "svg", "g", "path", "circle", "ellipse", "rect", "line",
  "polyline", "polygon", "text", "defs", "use",
]);

export function el(tag, attrs = {}, ...children) {
  const isSvg = SVG_TAGS.has(tag);
  const node = isSvg ? document.createElementNS(SVG_NS, tag) : document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") {
      if (isSvg) node.setAttribute("class", value);
      else node.className = value;
    }
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (key === "dataset") Object.assign(node.dataset, value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    node.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}
