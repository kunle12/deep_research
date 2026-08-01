/* Small display helpers. */

export function formatDate(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
}

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? "" : "s"}`;
}
