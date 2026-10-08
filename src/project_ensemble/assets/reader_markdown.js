// Shared, text-only Markdown renderer. Never interpret user/model HTML.
(() => {
  'use strict';
  function formula(value, display) {
    if (/\\(?:href|url|require|html\w*|style|class|cssId|includegraphics|newcommand|renewcommand|def|let|input|write)\b/i.test(value))
      return document.createTextNode((display ? '$$' : '$') + value + (display ? '$$' : '$'));
    const node = document.createElement('span');
    node.className = display ? 'math-display' : 'math-inline';
    node.textContent = display ? '\\[' + value + '\\]' : '\\(' + value + '\\)';
    return node;
  }
  function inline(container, value) {
    const token = /(`[^`\n]+`|\$\$[\s\S]*?\$\$|\\\[[\s\S]*?\\\]|(?<!\\)\$[^$\n]+\$|\\\([\s\S]*?\\\)|\*\*[^*\n]+\*\*|\*[^*\n]+\*)/g;
    let start = 0;
    for (const match of value.matchAll(token)) {
      container.appendChild(document.createTextNode(value.slice(start, match.index)));
      const raw = match[0];
      if (raw.startsWith('$$') || raw.startsWith('\\[')) container.appendChild(formula(raw.slice(2, -2), true));
      else if (raw.startsWith('\\(')) container.appendChild(formula(raw.slice(2, -2), false));
      else if (raw.startsWith('$')) container.appendChild(formula(raw.slice(1, -1), false));
      else {
        const tag = raw.startsWith('`') ? 'code' : raw.startsWith('**') ? 'strong' : 'em';
        const trim = raw.startsWith('**') ? 2 : 1;
        const node = document.createElement(tag);
        if (tag === 'code') node.textContent = raw.slice(trim, -trim);
        else inline(node, raw.slice(trim, -trim));
        container.appendChild(node);
      }
      start = match.index + raw.length;
    }
    container.appendChild(document.createTextNode(value.slice(start)));
  }
  function cells(line) {
    // Pipes in code or TeX, and escaped pipes, are cell content, not delimiters.
    const parts = [''];
    let fence = null;
    for (let i = 0; i < line.length; i++) {
      const char = line[i];
      if (char === '\\' && line[i + 1] === '|') {
        parts[parts.length - 1] += fence && fence !== '`' ? '\\|' : '|'; i++; continue;
      }
      if (char === '\\' && ['(', '['].includes(line[i + 1]) && !fence) {
        fence = line[i + 1] === '(' ? '\\)' : '\\]';
        parts[parts.length - 1] += line.slice(i, i + 2); i++; continue;
      }
      if (fence && line.startsWith(fence, i)) {
        parts[parts.length - 1] += fence; i += fence.length - 1; fence = null; continue;
      }
      if (!fence && (char === '`' || char === '$') && line[i - 1] !== '\\') {
        fence = char === '$' && line[i + 1] === '$' ? '$$' : char;
        parts[parts.length - 1] += fence; i += fence.length - 1; continue;
      }
      if (char === '|' && !fence) parts.push('');
      else parts[parts.length - 1] += char;
    }
    if (parts.length > 1 && !parts[0].trim()) parts.shift();
    if (parts.length > 1 && !parts.at(-1).trim()) parts.pop();
    return parts.map(part => part.trim());
  }
  function tableHeader(lines, index) {
    if (index + 1 >= lines.length || !lines[index].includes('|')) return null;
    const header = cells(lines[index]), divider = cells(lines[index + 1]);
    return header.length === divider.length && divider.every(cell => /^:?-{3,}:?$/.test(cell)) ? divider : null;
  }
  function render(container, value) {
    const lines = value.replace(/\r\n?/g, '\n').split('\n');
    let index = 0;
    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) { index++; continue; }
      if (/^\s*```/.test(line)) {
        const code = []; index++;
        while (index < lines.length && !/^\s*```/.test(lines[index])) code.push(lines[index++]);
        if (index < lines.length) index++;
        const pre = document.createElement('pre'), element = document.createElement('code');
        element.textContent = code.join('\n'); pre.appendChild(element); container.appendChild(pre); continue;
      }
      const divider = tableHeader(lines, index);
      if (divider) {
        const wrap = document.createElement('div'); wrap.className = 'reader-table-wrap';
        const table = document.createElement('table'), head = document.createElement('thead'), body = document.createElement('tbody');
        function row(values, tag) {
          const tr = document.createElement('tr');
          divider.forEach((align, col) => {
            const cell = document.createElement(tag);
            if (tag === 'th') cell.setAttribute('scope', 'col');
            cell.setAttribute('style', 'text-align:' + (align.startsWith(':') && align.endsWith(':') ? 'center' : align.endsWith(':') ? 'right' : 'left'));
            inline(cell, values[col] || ''); tr.appendChild(cell);
          });
          return tr;
        }
        head.appendChild(row(cells(line), 'th')); index += 2;
        while (index < lines.length && lines[index].trim() && lines[index].includes('|')) body.appendChild(row(cells(lines[index++]), 'td'));
        table.append(head, body); wrap.appendChild(table); container.appendChild(wrap); continue;
      }
      const heading = /^(#{1,6})\s+(.+)$/.exec(line);
      if (heading) { const h = document.createElement(heading[1].length === 1 ? 'h2' : 'h3'); inline(h, heading[2]); container.appendChild(h); index++; continue; }
      const bullet = /^\s*(?:[-*]|\d+[.)])\s+(.+)$/.exec(line);
      if (bullet) {
        const ordered = /^\s*\d/.test(line), list = document.createElement(ordered ? 'ol' : 'ul');
        while (index < lines.length) {
          const item = /^\s*(?:[-*]|\d+[.)])\s+(.+)$/.exec(lines[index]);
          if (!item || /^\s*\d/.test(lines[index]) !== ordered) break;
          const li = document.createElement('li'); inline(li, item[1]); list.appendChild(li); index++;
        }
        container.appendChild(list); continue;
      }
      if (/^\s*>\s?/.test(line)) {
        const quote = document.createElement('blockquote'), parts = [];
        while (index < lines.length && /^\s*>\s?/.test(lines[index])) parts.push(lines[index++].replace(/^\s*>\s?/, ''));
        inline(quote, parts.join('\n')); container.appendChild(quote); continue;
      }
      const para = [];
      while (index < lines.length && lines[index].trim() && !/^\s*(?:```|#{1,6}\s|>|[-*]\s|\d+[.)]\s)/.test(lines[index]) && !tableHeader(lines, index)) para.push(lines[index++]);
      if (!para.length) { index++; continue; }
      const p = document.createElement('p'); inline(p, para.join('\n')); container.appendChild(p);
    }
  }
  window.EnsembleReaderMarkdown = { render };
})();
