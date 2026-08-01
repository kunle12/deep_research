/* Unit tests for the dependency-free markdown parser (pure AST). */

import test from "node:test";
import assert from "node:assert/strict";

import { parse, parseInline } from "../../deep_research/webui/static/js/markdown.js";

test("headings", () => {
  const blocks = parse("# Title\n\n## Sub\n\n### Deep");
  assert.deepEqual(
    blocks.map((b) => [b.type, b.level]),
    [
      ["heading", 1],
      ["heading", 2],
      ["heading", 3],
    ],
  );
});

test("setext headings", () => {
  const h1 = parse("Big Title\n=====\n");
  assert.equal(h1[0].type, "heading");
  assert.equal(h1[0].level, 1);
  const h2 = parse("Sub Title\n---\n");
  assert.equal(h2[0].type, "heading");
  assert.equal(h2[0].level, 2);
});

test("paragraph with inline formatting", () => {
  const blocks = parse("Hello **world** and *em* and `code` and ~~gone~~.");
  assert.equal(blocks[0].type, "paragraph");
  const types = blocks[0].tokens.map((t) => t.type);
  assert.ok(types.includes("strong"));
  assert.ok(types.includes("em"));
  assert.ok(types.includes("code"));
  assert.ok(types.includes("del"));
  const strong = blocks[0].tokens.find((t) => t.type === "strong");
  assert.equal(strong.children[0].text, "world");
});

test("explicit link", () => {
  const tokens = parseInline("see [paper](https://arxiv.org/abs/2401.00001) now");
  const link = tokens.find((t) => t.type === "link");
  assert.ok(link);
  assert.equal(link.url, "https://arxiv.org/abs/2401.00001");
  assert.equal(link.label[0].text, "paper");
});

test("bare URLs are autolinked", () => {
  const tokens = parseInline("see https://arxiv.org/abs/2401.00001 now");
  const link = tokens.find((t) => t.type === "link");
  assert.ok(link);
  assert.ok(link.url.startsWith("https://arxiv.org"));
});

test("math spans are captured", () => {
  const inlineTokens = parseInline("cost $x^2$ total");
  assert.ok(inlineTokens.some((t) => t.type === "math" && t.text === "x^2" && !t.display));
  const displayTokens = parseInline("$$\\max_a Q(a)$$");
  assert.ok(displayTokens.some((t) => t.type === "math" && t.display));
});

test("fenced code block", () => {
  const blocks = parse("```python\nprint(1)\n```\n\nafter");
  assert.equal(blocks[0].type, "code");
  assert.equal(blocks[0].lang, "python");
  assert.equal(blocks[0].text, "print(1)");
  assert.equal(blocks[1].type, "paragraph");
});

test("unordered and ordered lists", () => {
  const blocks = parse("- a\n- b\n\n1. one\n2. two");
  assert.equal(blocks[0].type, "list");
  assert.equal(blocks[0].kind, "unordered");
  assert.equal(blocks[0].items.length, 2);
  assert.equal(blocks[1].type, "list");
  assert.equal(blocks[1].kind, "ordered");
  assert.equal(blocks[1].items.length, 2);
});

test("nested lists", () => {
  const blocks = parse("- a\n  - b\n  - c\n- d");
  const outer = blocks[0];
  assert.equal(outer.items.length, 2);
  const inner = outer.items[0].blocks.find((b) => b.type === "list");
  assert.ok(inner);
  assert.equal(inner.items.length, 2);
  assert.equal(inner.items[0].blocks[0].tokens[0].text, "b");
});

test("table", () => {
  const blocks = parse("| A | B |\n|---|---|\n| 1 | 2 |");
  assert.equal(blocks[0].type, "table");
  assert.deepEqual(blocks[0].headers, ["A", "B"]);
  assert.deepEqual(blocks[0].rows, [["1", "2"]]);
});

test("blockquote", () => {
  const blocks = parse("> quoted text\n> more text");
  assert.equal(blocks[0].type, "blockquote");
  assert.equal(blocks[0].blocks[0].type, "paragraph");
  assert.equal(blocks[0].blocks[0].tokens[0].text, "quoted text more text");
});

test("horizontal rule", () => {
  assert.equal(parse("---")[0].type, "hr");
  assert.equal(parse("***")[0].type, "hr");
});

test("empty input", () => {
  assert.deepEqual(parse(""), []);
  assert.deepEqual(parseInline(""), []);
});

test("no javascript: links leak through parse", () => {
  const tokens = parseInline("[bad](javascript:alert(1))");
  const link = tokens.find((t) => t.type === "link");
  assert.ok(link);
  assert.notEqual(link.url, "javascript:alert(1)");
});
