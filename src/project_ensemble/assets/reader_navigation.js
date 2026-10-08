(() => {
  'use strict';
  const article = document.querySelector('main article');
  const input = document.getElementById('reader-search');
  const searchStatus = document.getElementById('reader-search-status');
  const rail = document.getElementById('reader-position-rail');
  const ticks = document.getElementById('reader-position-ticks');
  const viewport = document.getElementById('reader-position-viewport');
  const stars = document.getElementById('reader-star-rail');
  const tocPanel = document.getElementById('toc-panel');
  const tocEntries = [...tocPanel.querySelectorAll('a[href^="#"]')]
    .map(link => ({link, heading:document.getElementById(link.getAttribute('href').slice(1))}))
    .filter(item => item.heading && article.contains(item.heading));
  let currentToc = null;
  function revealToc(link) {
    if (tocPanel.hidden || !link) return;
    const panelRect = tocPanel.getBoundingClientRect(), rect = link.getBoundingClientRect();
    if (panelRect.height <= 0) return;
    if (rect.top < panelRect.top + 12 || rect.bottom > panelRect.bottom - 12) {
      const top = Math.max(0, tocPanel.scrollTop + rect.top - panelRect.top - panelRect.height / 2 + rect.height / 2);
      const behavior = window.matchMedia?.('(prefers-reduced-motion:reduce)').matches ? 'auto' : 'smooth';
      if (tocPanel.scrollTo) tocPanel.scrollTo({top, behavior});
      else tocPanel.scrollTop = top;
    }
  }
  function updateToc() {
    if (!tocEntries.length) return;
    const readingLine = Math.min(140, window.innerHeight * .25);
    let active = tocEntries[0];
    for (const item of tocEntries) {
      if (item.heading.getBoundingClientRect().top <= readingLine) active = item;
      else break;
    }
    if (window.scrollY > 0 && window.scrollY + window.innerHeight >= document.documentElement.scrollHeight - 2)
      active = tocEntries[tocEntries.length - 1];
    if (active.link === currentToc) return;
    for (const item of tocEntries) {
      item.link.parentElement.classList.toggle('reader-toc-current', item === active);
      if (item === active) item.link.setAttribute('aria-current', 'location');
      else item.link.removeAttribute('aria-current');
    }
    currentToc = active.link;
    revealToc(currentToc);
  }
  const annotation = document.getElementById('annotation-panel');
  const qa = document.getElementById('qa-panel');
  const reference = document.getElementById('reader-drawer');
  const entry = document.getElementById('reader-entry-panel');
  let matches = [], current = -1, searchTimer, railFrame, railDirty = true;
  function tab(name) {
    document.body.dataset.readerNavigation = name;
    for (const item of ['toc', 'annotations']) {
      const active = item === name;
      const button = document.getElementById('nav-' + item);
      button.setAttribute('aria-selected', String(active)); button.tabIndex = active ? 0 : -1;
      document.getElementById(item === 'toc' ? 'toc-panel' : 'annotation-tools').hidden = !active;
    }
    if (name === 'toc') revealToc(currentToc);
  }
  for (const name of ['toc', 'annotations']) {
    const button = document.getElementById('nav-' + name);
    button.addEventListener('click', () => tab(name));
    button.addEventListener('keydown', event => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        const next = event.key === 'Home' ? 'toc' : event.key === 'End' ? 'annotations' : name === 'toc' ? 'annotations' : 'toc';
        tab(next); document.getElementById('nav-' + next).focus();
      }
    });
  }
  function sync() {
    const quote = document.getElementById('annotation-quote');
    if (quote) quote.hidden = annotation.dataset.mode !== 'edit' && qa.hidden;
    const paired = (!annotation.hidden || !entry.hidden) && !qa.hidden;
    const floats = document.getElementById('annotation-floats');
    floats.hidden = !annotation.hidden || !entry.hidden || !qa.hidden || !reference.hidden;
    document.body.classList.toggle('reader-paired', paired);
    const count = paired ? 2 : !annotation.hidden || !entry.hidden || !qa.hidden || !reference.hidden ? 1 : 0;
    document.body.dataset.readerPanels = String(count);
    document.body.style.setProperty('--reader-panel-count', String(count));
    document.dispatchEvent(new Event('ensemble-reader-layout'));
    scheduleRail();
  }
  // Also observe the pre-existing citation/terminology drawer controller.
  const observer = new MutationObserver(sync);
  for (const pane of [annotation, qa, reference, entry]) observer.observe(pane, { attributes:true, attributeFilter:['hidden', 'data-mode'] });
  window.EnsembleReaderUI = { tab, sync };
  function clearSearch() {
    if (window.CSS?.highlights) { CSS.highlights.delete('reader-search'); CSS.highlights.delete('reader-search-current'); }
    for (const mark of article.querySelectorAll('mark.reader-search-hit')) {
      const parent = mark.parentNode; mark.replaceWith(...mark.childNodes); parent.normalize();
    }
    matches = []; current = -1;
  }
  function status() { searchStatus.textContent = matches.length ? `${current + 1}/${matches.length}${matches.length === 500 ? '（最多显示500处）' : ''}` : input.value.trim() ? '无匹配' : ''; }
  function activate(index, scroll = true) {
    if (!matches.length) return;
    current = (index + matches.length) % matches.length;
    if (window.CSS?.highlights && window.Highlight) CSS.highlights.set('reader-search-current', new Highlight(matches[current]));
    for (const mark of article.querySelectorAll('mark.reader-search-hit')) mark.classList.toggle('reader-search-current', Number(mark.dataset.searchIndex) === current);
    if (scroll) {
      const rect = matches[current].getBoundingClientRect();
      window.scrollTo({ top: Math.max(0, rect.top + window.scrollY - window.innerHeight / 2), behavior:'smooth' });
    }
    status(); scheduleRail();
  }
  function search() {
    clearSearch();
    const query = input.value.trim().slice(0, 200);
    if (query) {
      // Build a text index per paragraph/cell. Do not search MathJax's generated
      // accessibility/source DOM or accidentally join unrelated paragraphs.
      for (const block of article.querySelectorAll('p,li,td,th,h1,h2,h3,h4,h5,h6')) {
        const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, { acceptNode: node =>
          node.parentElement.closest('.math,mjx-container,script,style') || node.parentElement.closest('p,li,td,th,h1,h2,h3,h4,h5,h6') !== block
            ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT });
        const nodes = []; let text = '';
        for (let node = walker.nextNode(); node; node = walker.nextNode()) { nodes.push({ node, start:text.length }); text += node.textContent; }
        const expression = new RegExp(query.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'), 'giu');
        for (const match of text.matchAll(expression)) {
          const start = match.index, end = start + match[0].length;
          const first = nodes.find(part => part.start + part.node.length > start);
          const last = nodes.find(part => part.start + part.node.length >= end);
          if (!first || !last) continue;
          const range = document.createRange();
          range.setStart(first.node, start - first.start); range.setEnd(last.node, end - last.start);
          matches.push(range); if (matches.length === 500) break;
        }
        if (matches.length === 500) break;
      }
      if (window.CSS?.highlights && window.Highlight) CSS.highlights.set('reader-search', new Highlight(...matches));
      else {
        // Reverse-order text splitting preserves nested inline markup and existing annotations.
        const segments = [];
        matches.forEach((range, index) => {
          const walker = document.createTreeWalker(range.commonAncestorContainer.nodeType === Node.TEXT_NODE ? range.commonAncestorContainer.parentNode : range.commonAncestorContainer, NodeFilter.SHOW_TEXT);
          for (let node = walker.nextNode(); node; node = walker.nextNode()) if (range.intersectsNode(node)) {
            const start = node === range.startContainer ? range.startOffset : 0;
            const end = node === range.endContainer ? range.endOffset : node.length;
            if (end > start) segments.push({ node, start, end, index });
          }
        });
        for (const part of segments.reverse()) {
          const middle = part.start ? part.node.splitText(part.start) : part.node; middle.splitText(part.end - part.start);
          const mark = document.createElement('mark'); mark.className = 'reader-search-hit'; mark.dataset.searchIndex = String(part.index);
          middle.replaceWith(mark); mark.appendChild(middle);
        }
        matches = matches.map((_, index) => {
          const marks = article.querySelectorAll(`mark.reader-search-hit[data-search-index="${index}"]`);
          const range = document.createRange(); range.setStartBefore(marks[0]); range.setEndAfter(marks[marks.length - 1]); return range;
        });
      }
    }
    if (matches.length) activate(0, false); else status();
    scheduleRail();
  }
  function updateRail() {
    railFrame = null;
    updateToc();
    const height = Math.max(document.documentElement.scrollHeight, window.innerHeight);
    viewport.style.top = `${100 * window.scrollY / height}%`;
    viewport.style.height = `${100 * window.innerHeight / height}%`;
    if (!railDirty) return;
    railDirty = false; ticks.replaceChildren(); stars.replaceChildren();
    function tick(rect, className, title, onClick, container = ticks) {
      const node = document.createElement('button'); node.type = 'button'; node.className = 'reader-position-tick ' + className;
      node.style.top = `${Math.max(0, Math.min(100, 100 * (rect.top + window.scrollY) / height))}%`;
      node.title = title; node.setAttribute('aria-label', title); node.addEventListener('click', event => { event.stopPropagation(); onClick(); }); container.appendChild(node);
    }
    matches.forEach((range, index) => tick(range.getBoundingClientRect(), index === current ? 'search-current' : 'search-match', `搜索结果 ${index + 1}`, () => activate(index)));
    const ids = new Set();
    for (const mark of article.querySelectorAll('mark.reader-highlight')) {
      if (ids.has(mark.dataset.annotationId)) continue; ids.add(mark.dataset.annotationId);
      const starred = mark.dataset.starred === 'true';
      const locate = () => { mark.scrollIntoView({ block:'center', behavior:'smooth' }); mark.click(); };
      tick(mark.getBoundingClientRect(), 'annotation-location', '高亮／批注：' + mark.textContent.slice(0, 60), locate);
      if (starred) tick(mark.getBoundingClientRect(), 'annotation-important',
        '重要批注：' + mark.textContent.slice(0, 60), locate, stars);
    }
  }
  function scheduleRail(rebuild = true) {
    railDirty = railDirty || Boolean(rebuild);
    if (!railFrame) railFrame = requestAnimationFrame(updateRail);
  }
  rail.addEventListener('click', event => {
    if (event.target.closest('button')) return;
    const rect = rail.getBoundingClientRect();
    window.scrollTo({ top: (event.clientY - rect.top) / rect.height * (document.documentElement.scrollHeight - window.innerHeight), behavior:'smooth' });
  });
  input.addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(search, 160); });
  document.addEventListener('keydown', event => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 'f') {
      event.preventDefault(); input.focus(); input.select();
    }
  });
  input.addEventListener('keydown', event => {
    if (event.key !== 'Enter') return;
    event.preventDefault(); clearTimeout(searchTimer);
    if (!matches.length) { search(); activate(event.shiftKey ? matches.length - 1 : 0); }
    else activate(current + (event.shiftKey ? -1 : 1));
  });
  document.getElementById('search-next').addEventListener('click', () => activate(current + 1));
  document.getElementById('search-prev').addEventListener('click', () => activate(current - 1));
  document.getElementById('search-clear').addEventListener('click', () => { input.value = ''; clearTimeout(searchTimer); search(); input.focus(); });
  document.addEventListener('ensemble-annotations-before-render', clearSearch);
  document.addEventListener('ensemble-annotations-rendered', search);
  document.addEventListener('ensemble-annotation-metadata-changed', scheduleRail);
  window.addEventListener('scroll', () => scheduleRail(false), { passive:true });
  window.addEventListener('resize', scheduleRail);
  window.addEventListener('load', scheduleRail);
  window.addEventListener('hashchange', () => scheduleRail(false));
  document.fonts?.ready.then(() => scheduleRail());
  if (window.ResizeObserver) new ResizeObserver(scheduleRail).observe(article);
  tab('toc'); sync();
})();
