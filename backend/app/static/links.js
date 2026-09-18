"use strict";
// Build DOM nodes only: model messages never become HTML, even when they contain links.
function appendLinkedText(container, text) {
  const pattern = /\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)|https?:\/\/[^\s<>]+/gi;
  let start = 0;
  for (const match of text.matchAll(pattern)) {
    container.append(document.createTextNode(text.slice(start, match.index)));
    const raw = match[0], markdown = !!match[2];
    const href = markdown ? match[2] : raw.replace(/[.,;!?)]+$/, "");
    const suffix = markdown ? "" : raw.slice(href.length);
    let url;
    try { url = new URL(href); } catch { /* Keep malformed URLs as plain text. */ }
    if (url && ["http:", "https:"].includes(url.protocol) && !url.username && !url.password) {
      const link = document.createElement("a"); link.href = href; link.textContent = markdown ? match[1] : href;
      link.target = "_blank"; link.rel = "noopener noreferrer"; container.append(link);
      if (suffix) container.append(document.createTextNode(suffix));
    } else { container.append(document.createTextNode(raw)); }
    start = match.index + raw.length;
  }
  container.append(document.createTextNode(text.slice(start)));
}
