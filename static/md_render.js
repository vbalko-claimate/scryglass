// md_render.js — the ONE markdown renderer for LLM guide text (task #20).
//
// DOM-BUILDING, no HTML strings: model output goes through createElement /
// textContent only, so there is no escaping step to get wrong and no
// innerHTML sink. This file is a VERBATIM extraction of the renderer inside
// glass-cloud's app.html (which stays inline there for CSP reasons);
// glass-cloud/ui-tests/test_parity.js asserts the two copies stay identical
// and test_markdown.js holds the behavioral suite (58 assertions).
//
// Exposes window.renderMarkdown(src[, doc]) -> DocumentFragment.
(function () {
// --- Markdown for the deck guides -------------------------------------
//
// This builds DOM NODES and never an HTML string. That is the entire
// design: with no serialisation step there is no escaping to get wrong,
// and no way for a later pass to re-read markup an earlier pass emitted.
//
// The string-based version this replaces had exactly that bug (found in
// review): the italic regex ran over an already-emitted anchor, so
// "[x_](https://e.test/_)" rewrote target="_blank" into target="<em>blank"
// and destroyed a security attribute. Text now only ever arrives through
// textContent, and attributes only through setAttribute on a value that
// has already been validated.

// Link allowlist: absolute http(s) only, decided by the URL parser rather
// than a regex. Control and bidi-formatting characters are rejected
// outright -- in a URL they serve only to disguise where it points.
function mdSafeUrl(raw) {
  const u = String(raw == null ? '' : raw).trim();
  if (!/^https?:\/\//i.test(u)) return null;
  const CTRLRE = /[\u0000-\u001F\u007F\u200B-\u200F\u202A-\u202E\u2066-\u2069]/;
  if (CTRLRE.test(u)) return null;
  try {
    const parsed = new URL(u);
    if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') return null;
    return parsed.href;
  } catch (e) {
    return null;
  }
}

// Constructs that must be treated as one indivisible unit when scanning
// for an emphasis closer.
function mdAtom(rest) {
  return /^(`+)([\s\S]*?)\1/.exec(rest)
      || /^!\[([^\]]*)\]\([^)]*\)/.exec(rest)
      || /^\[([^\]]*)\]\(([^)]*)\)/.exec(rest);
}

// Index of the closing delimiter for emphasis, skipping atoms so that a
// "*" or "_" inside a URL or a code span is never mistaken for the closer.
// Returns -1 if there is no valid closer (the delimiter stays literal).
//
// CommonMark's flanking rule is enforced: a closer may not be preceded by
// whitespace, which is what keeps "2 * 3 * 4" from becoming emphasis. A
// candidate that fails it is skipped rather than abandoning the search, so
// "*a * b*" still closes on the final delimiter.
function mdFindClose(s, from, delim) {
  let i = from;
  while (i < s.length) {
    const ch = s.charAt(i);
    if (ch === '\n') return -1;
    if (ch === '`' || ch === '[' || ch === '!') {
      const a = mdAtom(s.slice(i));
      if (a) { i += a[0].length; continue; }
    }
    if (s.startsWith(delim, i)) {
      if (i > from && !/\s/.test(s.charAt(i - 1))) return i;
      i += delim.length;
      continue;
    }
    i++;
  }
  return -1;
}

// An opener may not be followed by whitespace (also CommonMark flanking).
function mdOpens(s, at) {
  const nxt = s.charAt(at);
  return nxt !== '' && !/\s/.test(nxt);
}

// Inline scanner: returns an array of Nodes. Recurses for emphasis, so
// "**bold *both* end**" nests properly.
function mdInline(src, doc) {
  doc = doc || document;
  const nodes = [];
  let text = '';
  function flush() {
    if (text) { nodes.push(doc.createTextNode(text)); text = ''; }
  }
  function wrap(tag, inner) {
    flush();
    const el = doc.createElement(tag);
    mdInline(inner, doc).forEach(function (n) { el.appendChild(n); });
    nodes.push(el);
  }
  const s = String(src == null ? '' : src);
  let i = 0;
  while (i < s.length) {
    const rest = s.slice(i);
    const ch = s.charAt(i);
    let m;

    // Code spans first: contents are literal and never re-scanned.
    // Multi-backtick delimiters are supported, so ``a`b`` works.
    if (ch === '`' && (m = /^(`+)([\s\S]*?)\1/.exec(rest))) {
      flush();
      const code = doc.createElement('code');
      code.textContent = m[2];
      nodes.push(code);
      i += m[0].length;
      continue;
    }
    // An image degrades to its alt text: no element, so no remote fetch.
    if (ch === '!' && (m = /^!\[([^\]]*)\]\([^)]*\)/.exec(rest))) {
      text += m[1];
      i += m[0].length;
      continue;
    }
    // A link whose URL is rejected degrades to its label.
    if (ch === '[' && (m = /^\[([^\]]*)\]\(([^)]*)\)/.exec(rest))) {
      const href = mdSafeUrl(m[2]);
      flush();
      if (href) {
        const a = doc.createElement('a');
        a.setAttribute('href', href);
        a.setAttribute('target', '_blank');
        a.setAttribute('rel', 'noopener noreferrer nofollow');
        mdInline(m[1], doc).forEach(function (n) { a.appendChild(n); });
        nodes.push(a);
      } else {
        mdInline(m[1], doc).forEach(function (n) { nodes.push(n); });
      }
      i += m[0].length;
      continue;
    }
    for (const pair of [['**', 'strong'], ['~~', 's']]) {
      if (rest.slice(0, 2) === pair[0] && mdOpens(s, i + 2)) {
        const close = mdFindClose(s, i + 2, pair[0]);
        if (close > i + 2) { wrap(pair[1], s.slice(i + 2, close)); i = close + 2; m = true; break; }
      }
    }
    if (m === true) { m = null; continue; }

    // Single * or _ only when it opens at a word boundary, so
    // snake_case_names and "2 * 3 * 4" stay literal.
    const prev = i === 0 ? '' : s.charAt(i - 1);
    if (((ch === '*' && !/[\w*]/.test(prev)) || (ch === '_' && !/[\w_]/.test(prev)))
        && mdOpens(s, i + 1)) {
      const close = mdFindClose(s, i + 1, ch);
      // For "_" the closer must not sit inside a word either.
      const okClose = close > i + 1
        && (ch === '*' || !/[\w_]/.test(s.charAt(close + 1) || ''));
      if (okClose) { wrap('em', s.slice(i + 1, close)); i = close + 1; continue; }
    }
    text += ch;
    i++;
  }
  flush();
  return nodes;
}

function renderMarkdown(src, doc) {
  doc = doc || document;
  const frag = doc.createDocumentFragment();
  const lines = String(src == null ? '' : src).replace(/\r\n?/g, '\n').split('\n');
  const lists = [];     // [{tag, el, li}], innermost last
  let para = [];        // raw lines of the paragraph being accumulated

  function appendInline(target, textSrc) {
    mdInline(textSrc, doc).forEach(function (n) { target.appendChild(n); });
  }
  function closeLists(depth) { while (lists.length > depth) lists.pop(); }
  function flushPara() {
    if (!para.length) return;
    const p = doc.createElement('p');
    para.forEach(function (ln, idx) {
      appendInline(p, ln.replace(/\s+$/, ''));
      if (idx < para.length - 1) {
        // Two or more trailing spaces is a markdown hard break; a single
        // newline is a soft wrap and joins with a space.
        if (/\s{2,}$/.test(ln)) p.appendChild(doc.createElement('br'));
        else p.appendChild(doc.createTextNode(' '));
      }
    });
    frag.appendChild(p);
    para = [];
  }
  function isTableSep(l) {
    // Must itself contain a pipe. Without this, prose like
    // "Cast Helix | then attack" followed by a "---" divider was read as a
    // table header + separator: the <hr> vanished and an empty-bodied
    // table appeared in its place.
    return l.indexOf('|') !== -1 && /-{2,}/.test(l)
      && /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(l);
  }
  // Cell split that honours escaped pipes and pipes inside code spans, so
  // "| `a|b` |" and "| Fire \| Ice |" stay single cells.
  function splitCells(line) {
    const out = [];
    let cur = '', tick = 0;
    for (let i = 0; i < line.length; i++) {
      const c = line.charAt(i);
      if (c === '\\' && line.charAt(i + 1) === '|') { cur += '|'; i++; continue; }
      if (c === '`') {
        let n = 0;
        while (line.charAt(i + n) === '`') n++;
        if (tick === 0) tick = n; else if (tick === n) tick = 0;
        cur += line.substr(i, n);
        i += n - 1;
        continue;
      }
      if (c === '|' && tick === 0) { out.push(cur); cur = ''; continue; }
      cur += c;
    }
    out.push(cur);
    if (out.length && !out[0].trim()) out.shift();
    if (out.length && !out[out.length - 1].trim()) out.pop();
    return out.map(function (c) { return c.trim(); });
  }

  for (let i = 0; i < lines.length; i++) {
    const line = lines[i];

    // Fenced code. The closer must be at least as long as the opener, so a
    // ``` line inside a ```` block does not end it early.
    const fence = /^\s*(`{3,}|~{3,})\s*[\w+#.-]*\s*$/.exec(line);
    if (fence) {
      flushPara(); closeLists(0);
      const mark = fence[1].charAt(0) === '`' ? '`' : '~';
      const closer = new RegExp('^\\s*' + mark + '{' + fence[1].length + ',}\\s*$');
      const buf = [];
      i++;
      while (i < lines.length && !closer.test(lines[i])) { buf.push(lines[i]); i++; }
      const pre = doc.createElement('pre');
      const code = doc.createElement('code');
      code.textContent = buf.join('\n');
      pre.appendChild(code);
      frag.appendChild(pre);
      continue;
    }

    if (!line.trim()) { flushPara(); closeLists(0); continue; }

    // Rule, before lists so "---" is never read as a bullet.
    if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(line)) {
      flushPara(); closeLists(0);
      frag.appendChild(doc.createElement('hr'));
      continue;
    }

    const head = /^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/.exec(line);
    if (head) {
      flushPara(); closeLists(0);
      // Mapped into h3..h6: the page owns h1 (the account) and its section
      // titles, so a guide heading must not outrank them.
      const h = doc.createElement('h' + Math.min(6, head[1].length + 2));
      appendInline(h, head[2]);
      frag.appendChild(h);
      continue;
    }

    const qre = /^\s{0,3}>\s?(.*)$/;
    const q = qre.exec(line);
    if (q) {
      flushPara(); closeLists(0);
      const buf = [q[1]];
      while (i + 1 < lines.length && qre.test(lines[i + 1])) {
        buf.push(qre.exec(lines[++i])[1]);
      }
      // Recurse on the quote body: a blockquote can legitimately contain
      // lists, paragraphs, code and headings, and "> - item" must be a
      // list rather than a literal "- ".
      const bq = doc.createElement('blockquote');
      bq.appendChild(renderMarkdown(buf.join('\n'), doc));
      frag.appendChild(bq);
      continue;
    }

    // Pipe table: a header row followed by a |---|---| separator.
    if (line.indexOf('|') !== -1 && i + 1 < lines.length && isTableSep(lines[i + 1])) {
      flushPara(); closeLists(0);
      const hdr = splitCells(line);
      i += 2;
      const table = doc.createElement('table');
      const thead = doc.createElement('thead');
      const hrow = doc.createElement('tr');
      hdr.forEach(function (c) {
        const th = doc.createElement('th');
        appendInline(th, c);
        hrow.appendChild(th);
      });
      thead.appendChild(hrow);
      table.appendChild(thead);
      const tbody = doc.createElement('tbody');
      while (i < lines.length && lines[i].indexOf('|') !== -1 && lines[i].trim()) {
        const row = splitCells(lines[i]);
        const tr = doc.createElement('tr');
        // Normalised to the header width: a ragged row would otherwise
        // render a table with uneven columns.
        for (let c = 0; c < hdr.length; c++) {
          const td = doc.createElement('td');
          if (c < row.length) appendInline(td, row[c]);
          tr.appendChild(td);
        }
        tbody.appendChild(tr);
        i++;
      }
      i--;
      table.appendChild(tbody);
      frag.appendChild(table);
      continue;
    }

    const item = /^(\s*)([-*+]|(\d+)[.)])\s+(.*)$/.exec(line);
    if (item) {
      flushPara();
      const indent = item[1].replace(/\t/g, '  ').length;
      const kind = item[3] ? 'ol' : 'ul';
      // Clamped: markdown may jump indent levels, HTML may not. A list can
      // only open one level deeper than the one already open, which is what
      // stops an indent jump emitting <ul><ul> with no item between.
      const depth = Math.min(Math.floor(indent / 2) + 1, lists.length + 1);
      closeLists(depth);
      if (lists.length === depth && lists[depth - 1].tag !== kind) closeLists(depth - 1);
      while (lists.length < depth) {
        const el = doc.createElement(kind);
        // Respect an ordered list that does not start at 1.
        if (kind === 'ol' && item[3] && Number(item[3]) !== 1 && lists.length + 1 === depth) {
          el.setAttribute('start', String(Number(item[3])));
        }
        const parent = lists.length
          ? (lists[lists.length - 1].li || lists[lists.length - 1].el)
          : frag;
        parent.appendChild(el);
        lists.push({ tag: kind, el: el, li: null });
      }
      const li = doc.createElement('li');
      appendInline(li, item[4]);
      lists[lists.length - 1].el.appendChild(li);
      lists[lists.length - 1].li = li;
      continue;
    }

    // Lazy continuation: a wrapped list item, indented or not. A blank
    // line is what ends a list (handled above), so any prose line reached
    // while a list is open belongs to the current item. Without this a
    // wrapped bullet split the list -- and a split <ol> restarted at 1.
    if (lists.length && lists[lists.length - 1].li) {
      const li = lists[lists.length - 1].li;
      li.appendChild(doc.createTextNode(' '));
      appendInline(li, line.trim());
      continue;
    }

    // Anything else is prose.
    if (lists.length) closeLists(0);
    para.push(line);
  }
  flushPara(); closeLists(0);
  return frag;
}
window.renderMarkdown = renderMarkdown;
})();
