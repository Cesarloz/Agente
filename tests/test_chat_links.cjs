const {readFileSync} = require("node:fs");
const vm = require("node:vm");
const assert = require("node:assert/strict");

function node(kind, text = "") {
  return {kind, textContent: text, children: [], append(...items) {this.children.push(...items);}};
}
const context = {URL, document: {createElement: kind => node(kind), createTextNode: text => node("text", text)}};
vm.createContext(context);
vm.runInContext(readFileSync("backend/app/static/links.js", "utf8"), context);
function render(text) {
  const result = node("div"); context.appendLinkedText(result, text); return result;
}
const markdown = render("Agenda en [Bookings](https://outlook.office.com/bookwithme/).");
const link = markdown.children.find(item => item.kind === "a");
assert.equal(link.href, "https://outlook.office.com/bookwithme/");
assert.equal(link.textContent, "Bookings");
assert.equal(link.rel, "noopener noreferrer");
assert.equal(link.target, "_blank");
const plain = render("Visita https://example.com/info?x=1&y=2. Gracias.");
assert.equal(plain.children.find(item => item.kind === "a").href, "https://example.com/info?x=1&y=2");
assert.equal(plain.children.map(item => item.textContent).join(""), "Visita https://example.com/info?x=1&y=2. Gracias.");
for (const payload of ["<script>alert(1)</script>", '[abrir](javascript:alert(1))', '<img src=x onerror=alert(1)>', "https://name:password@example.com"])
  assert.equal(render(payload).children.filter(item => item.kind !== "text").length, 0);
assert.equal(render("[<img onerror=x>](https://example.com)").children.find(item => item.kind === "a").textContent, "<img onerror=x>");
console.log("7 comprobaciones de enlaces y texto seguro aprobadas.");
