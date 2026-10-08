(() => {
  'use strict';
  const article = document.querySelector('main article');
  const panel = document.getElementById('annotation-panel');
  if (!article || !panel) return;
  const key = document.documentElement.dataset.annotationKey;
  const blocks = [...article.querySelectorAll('p,li,td,th,h1,h2,h3,h4,h5,h6')];
  const status = document.getElementById('annotation-status');
  const list = document.getElementById('annotation-list');
  const detail = document.getElementById('annotation-detail');
  const quote = document.getElementById('annotation-quote');
  const note = document.getElementById('annotation-note');
  const preview = document.getElementById('annotation-preview');
  const qaPanel = document.getElementById('qa-panel');
  const qaFrame = document.getElementById('qa-frame');
  const menu = document.getElementById('selection-menu');
  const floats = document.getElementById('annotation-floats');
  const floatCards = new Map();
  const entryPanel = document.getElementById('reader-entry-panel');
  const entryBody = document.getElementById('reader-entry-body');
  const entryContinue = document.getElementById('reader-entry-continue');
  const entryStar = document.getElementById('reader-entry-star');
  const editorStar = document.getElementById('annotation-star');
  let entryId = null;
  let entryRevision = 0;
  let selectedFloatId = null;
  const qaSource = qaFrame.dataset.srcdoc;
  const qaHistoryKey = key + ':qa-history-v1';
  let qaLoaded = false;
  let selectedRange = null;
  let currentId = null;
  let records = [];
  let storageAvailable = true;
  let previewRevision = 0;
  let previewQueue = Promise.resolve();
  let previewTimer = null;
  let qaFocusText = '';
  let qaAnchor = null;
  let qaSavedEntry = null;

  const pendingNotes = new Map();
  let positionFrame = null;
  function positionCard() {
    positionFrame = null;
    positionFloats();
    if (panel.hidden || panel.dataset.mode !== 'view' || !currentId) return;
    const record = records.find(item => item.id === currentId);
    const target = article.querySelector(`mark.reader-highlight[data-annotation-id="${CSS.escape(currentId)}"]`)
      || blocks[record?.block];
    if (target) {
      panel.style.setProperty('--annotation-top', `${Math.max(8, target.getBoundingClientRect().top + window.scrollY)}px`);
      const width = panel.getBoundingClientRect().width || Math.min(380, window.innerWidth * .3);
      const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
      const left = Math.max(12, Math.min(article.getBoundingClientRect().right + 12,
        viewportWidth - width - 56));
      panel.style.setProperty('--annotation-left', `${left + window.scrollX}px`);
    }
  }
  function scheduleCardPosition() {
    if (!positionFrame) positionFrame = requestAnimationFrame(positionCard);
  }
  document.addEventListener('ensemble-reader-layout', scheduleCardPosition);
  document.addEventListener('ensemble-annotations-rendered', scheduleCardPosition);
  document.addEventListener('ensemble-reader-focus-changed', scheduleCardPosition);
  window.addEventListener('resize', scheduleCardPosition);
  window.addEventListener('scroll', scheduleCardPosition, { passive:true });
  window.addEventListener('load', scheduleCardPosition);
  document.fonts?.ready.then(scheduleCardPosition);
  if (window.ResizeObserver) new ResizeObserver(scheduleCardPosition).observe(article);
  function notePreviewNodes(value) {
    const nodes = document.createDocumentFragment();
    window.EnsembleReaderMarkdown.render(nodes, value);
    return nodes;
  }
  function positionFloats() {
    if (floats.hidden) return;
    const viewportWidth = document.documentElement.clientWidth || window.innerWidth;
    const positions = [];
    for (const [id, card] of floatCards) {
      const target = article.querySelector(`mark.reader-highlight[data-annotation-id="${CSS.escape(id)}"]`);
      card.hidden = !target;
      if (!target) continue;
      const anchor = target.getBoundingClientRect();
      const width = card.getBoundingClientRect().width || Math.min(380, viewportWidth * .3);
      card.style.top = `${Math.max(8, anchor.top + window.scrollY)}px`;
      card.style.left = `${Math.max(12, Math.min(article.getBoundingClientRect().right + 12,
        viewportWidth - width - 108)) + window.scrollX}px`;
      positions.push({card, distance:Math.abs((anchor.top + (anchor.height || 0) / 2) - window.innerHeight / 2)});
    }
    const priorityId = window.EnsembleReaderFocus?.enabled
      ? window.EnsembleReaderFocus.activeId : selectedFloatId;
    positions.sort((a, b) => Number(a.card.dataset.annotationId === priorityId)
      - Number(b.card.dataset.annotationId === priorityId) || b.distance - a.distance);
    for (let i = 0; i < positions.length; i++) {
      positions[i].card.style.zIndex = String(i + 1);
      positions[i].card.classList.toggle('annotation-foremost', i === positions.length - 1);
      const left = parseFloat(positions[i].card.style.left) - window.scrollX;
      positions[i].card.style.setProperty('--annotation-peek', `${-Math.min(18, (positions.length - 1 - i) * 6, Math.max(0, left - 12))}px`);
    }
  }
  function selectFloat(id) {
    selectedFloatId = id;
    for (const [recordId, card] of floatCards) {
      const selected = recordId === id;
      card.classList.toggle('annotation-selected', selected);
      card.querySelector('.annotation-float-actions').hidden = !selected;
    }
    scheduleCardPosition();
  }
  document.addEventListener('pointerdown', event => {
    if (!event.target.closest('.annotation-float')) selectFloat(null);
  });
  function renderFloats() {
    const saved = records.filter(record => record.note.trim());
    const ids = new Set(saved.map(record => record.id));
    for (const [id, card] of floatCards) {
      if (!ids.has(id)) { card.readerResizeObserver?.disconnect(); card.remove(); floatCards.delete(id); }
    }
    const typeset = [];
    for (const record of saved) {
      let card = floatCards.get(record.id);
      if (!card) {
        card = document.createElement('aside');
        card.className = 'annotation-float'; card.dataset.annotationId = record.id;
        card.setAttribute('aria-label', '批注');
        const content = document.createElement('div'); content.className = 'annotation-preview';
        content.tabIndex = 0; content.title = '点击显示操作；双击编辑；全文见“批注与问答”目录';
        card.addEventListener('click', event => {
          if (!event.target.closest('button,a')) selectFloat(record.id);
        });
        content.addEventListener('dblclick', () => openRecord(record.id, true));
        content.addEventListener('keydown', event => {
          if (['Enter', ' '].includes(event.key)) { event.preventDefault(); selectFloat(record.id); }
        });
        const actions = document.createElement('div'); actions.className = 'annotation-float-actions';
        actions.hidden = true;
        actions.setAttribute('role', 'group'); actions.setAttribute('aria-label', '批注操作');
        for (const [label, action] of [
          ['编辑', () => openRecord(record.id, true)],
          ['问 AI', () => openQaForRecord(records.find(item => item.id === record.id))],
          ['删除', () => deleteRecord(record.id)],
        ]) {
          const button = document.createElement('button'); button.type = 'button'; button.textContent = label;
          button.title = label === '问 AI' ? '就这条批注问 AI' : label + '批注';
          button.addEventListener('click', action); actions.appendChild(button);
        }
        const star = document.createElement('button'); star.type = 'button';
        star.className = 'annotation-star-toggle';
        star.addEventListener('click', () => toggleStar(record.id)); actions.appendChild(star);
        card.append(content, actions); floatCards.set(record.id, card); floats.appendChild(card);
        if (window.ResizeObserver) {
          const observer = new ResizeObserver(scheduleCardPosition);
          observer.observe(card); card.readerResizeObserver = observer;
        }
      }
      if (card.readerNote !== record.note) {
        const content = card.querySelector('.annotation-preview');
        window.MathJax?.typesetClear?.([content]);
        content.replaceChildren(notePreviewNodes(record.note)); card.readerNote = record.note;
        typeset.push(content);
      }
    }
    syncStarControls();
    if (typeset.length && window.MathJax?.typesetPromise) {
      previewQueue = previewQueue.catch(() => {}).then(() => window.MathJax.typesetPromise(typeset)).catch(() => {
        message('批注公式暂未能渲染；原始文字仍保留。');
      }).then(scheduleCardPosition);
    }
    document.dispatchEvent(new Event('ensemble-reader-focus-refresh'));
    window.EnsembleReaderUI.sync(); scheduleCardPosition();
  }
  function setMode(editing) {
    panel.dataset.mode = editing ? 'edit' : 'view';
    document.getElementById('annotation-editor').hidden = !editing;
    document.getElementById('annotation-save').hidden = !editing;
    document.getElementById('annotation-cancel').hidden = !editing;
    document.getElementById('annotation-edit').hidden = editing;
    document.getElementById('annotation-title').textContent = editing ? '编辑批注' : '批注';
    // Saved notes stand alone; the source remains visible at its document anchor.
    quote.hidden = !editing && qaPanel.hidden;
    document.getElementById('annotation-empty-message').hidden = !editing || Boolean(note.value.trim());
    preview.hidden = !editing && !note.value.trim();
    if (editing) note.focus();
    syncStarControls();
    window.EnsembleReaderUI.sync();
  }
  function starButton(button, record) {
    const starred = record?.starred === true;
    button.textContent = starred ? '★' : '☆';
    button.setAttribute('aria-pressed', String(starred));
    button.title = starred ? '取消重要批注星标' : '加星，标为重要批注';
    button.setAttribute('aria-label', button.title);
  }
  function syncStarControls() {
    for (const [id, card] of floatCards) starButton(card.querySelector('.annotation-star-toggle'),
      records.find(record => record.id === id));
    const editing = records.find(record => record.id === currentId);
    editorStar.hidden = !editing || (!editing.note.trim() && panel.dataset.mode !== 'edit');
    starButton(editorStar, editing);
    const entry = records.find(record => record.id === entryStar.dataset.annotationId);
    entryStar.hidden = !entry?.note.trim() || entryPanel.hidden;
    starButton(entryStar, entry);
  }
  function toggleStar(id) {
    const record = records.find(item => item.id === id);
    if (!record || (!record.note.trim() && (panel.hidden || panel.dataset.mode !== 'edit' || currentId !== id))) return;
    if (record.starred === true) delete record.starred; else record.starred = true;
    const saved = persist();
    // Metadata only: preserve text nodes, search ranges and unsaved editor text.
    for (const mark of article.querySelectorAll(`mark.reader-highlight[data-annotation-id="${CSS.escape(id)}"]`))
      mark.dataset.starred = String(record.starred === true && Boolean(record.note.trim()));
    syncStarControls(); renderList();
    document.dispatchEvent(new Event('ensemble-annotation-metadata-changed'));
    message(saved ? (record.starred ? '已标为重要批注。' : '已取消批注星标。')
      : '星标保留在当前页面；本地存储不可用，请另存带批注 HTML。');
  }
  editorStar.addEventListener('click', () => toggleStar(currentId));
  entryStar.addEventListener('click', () => toggleStar(entryStar.dataset.annotationId));
  function closePanels() {
    panel.hidden = true; qaPanel.hidden = true; currentId = null;
    entryPanel.hidden = true; entryId = null; selectFloat(null);
    menu.hidden = true; selectedRange = null;
    window.EnsembleReaderUI.sync(); renderList();
  }
  function editCurrent() {
    if (!currentId || panel.hidden) return;
    setMode(true);
  }
  document.getElementById('annotation-edit').addEventListener('click', editCurrent);
  preview.addEventListener('dblclick', editCurrent);
  preview.addEventListener('keydown', event => {
    if (event.key === 'Enter') { event.preventDefault(); editCurrent(); }
  });
  document.getElementById('annotation-cancel').addEventListener('click', () => {
    const record = records.find(item => item.id === currentId);
    if (!record) return;
    pendingNotes.delete(record.id); note.value = record.note;
    renderNotePreview(); setMode(false);
    if (qaPanel.hidden) { panel.hidden = true; window.EnsembleReaderUI.sync(); }
  });
  function renderNotePreview() {
    const revision = ++previewRevision;
    const value = note.value;
    document.getElementById('annotation-empty-message').hidden = panel.dataset.mode !== 'edit' || Boolean(value.trim());
    preview.hidden = panel.dataset.mode !== 'edit' && !value.trim();
    previewQueue = previewQueue.catch(() => {}).then(async () => {
      if (revision !== previewRevision) return;
      try {
        if (window.MathJax && window.MathJax.typesetClear) window.MathJax.typesetClear([preview]);
        preview.replaceChildren(notePreviewNodes(value));
        if (window.MathJax && window.MathJax.typesetPromise) await window.MathJax.typesetPromise([preview]);
      } catch (_) { message('批注公式暂未能预览；原始批注文字仍可保存。'); }
    });
  }

  function message(value) { status.textContent = value; }
  function validRecord(value) {
    return value && typeof value.id === 'string' && value.id.length <= 80
      && Number.isInteger(value.block) && value.block >= 0 && value.block <= 1000000
      && Number.isInteger(value.start) && value.start >= 0
      && typeof value.quote === 'string' && value.quote.length > 0 && value.quote.length <= 2000
      && typeof value.note === 'string' && value.note.length <= 4000
      && (value.starred === undefined || typeof value.starred === 'boolean')
      && (value.textParts === undefined || (Array.isArray(value.textParts)
        && value.textParts.length > 0 && value.textParts.length <= 100
        && value.textParts.every(part => Number.isInteger(part.start) && part.start >= 0
          && typeof part.quote === 'string' && part.quote.length > 0 && part.quote.length <= 2000)))
      && (value.qaThreads === undefined || (Array.isArray(value.qaThreads)
        && value.qaThreads.length <= 30 && value.qaThreads.every(validQaEntry)));
  }
  function validQaEntry(entry) {
    return entry && typeof entry.id === 'string' && entry.id.length <= 80
      && typeof entry.time === 'string' && entry.time.length <= 40
      && Array.isArray(entry.turns) && entry.turns.length > 0 && entry.turns.length <= 20
      && entry.turns.every(turn => turn && typeof turn.question === 'string' && turn.question.length <= 1000
        && typeof turn.answer === 'string' && turn.answer.length <= 12000
        && typeof turn.focus === 'string' && turn.focus.length <= 2000);
  }
  let standaloneQaHistory = [];
  const embeddedHistory = document.getElementById('embedded-qa-history')?.textContent || '[]';
  try {
    const saved = JSON.parse(localStorage.getItem(qaHistoryKey) || embeddedHistory);
    if (Array.isArray(saved)) standaloneQaHistory = saved.filter(validQaEntry).slice(-30);
  } catch (_) {
    try {
      const saved = JSON.parse(embeddedHistory);
      if (Array.isArray(saved)) standaloneQaHistory = saved.filter(validQaEntry).slice(-30);
    } catch (_) {}
  }
  const embedded = document.getElementById('embedded-annotations');
  try {
    const saved = JSON.parse(localStorage.getItem(key) || embedded?.textContent || '[]');
    if (Array.isArray(saved)) records = saved.filter(validRecord).slice(0, 500);
  } catch (_) {
    storageAvailable = false;
    try {
      const saved = JSON.parse(embedded?.textContent || '[]');
      if (Array.isArray(saved)) records = saved.filter(validRecord).slice(0, 500);
    } catch (_) {}
    message('浏览器未开放本地存储；可将标注另存为 HTML 副本。');
  }
  function persist() {
    try { localStorage.setItem(key, JSON.stringify(records)); return true; }
    catch (_) {
      storageAvailable = false;
      message('批注未能写入浏览器本地存储；请另存带批注 HTML 副本。');
      return false;
    }
  }
  function clearMarks() {
    for (const mark of article.querySelectorAll('mark.reader-highlight')) {
      const parent = mark.parentNode;
      mark.replaceWith(...mark.childNodes);
      parent.normalize();
    }
  }
  function findStart(block, record) {
    const text = block.textContent;
    if (text.slice(record.start, record.start + record.quote.length) === record.quote)
      return record.start;
    const matches = [];
    let offset = text.indexOf(record.quote);
    while (offset >= 0 && matches.length < 20) {
      matches.push(offset);
      offset = text.indexOf(record.quote, offset + 1);
    }
    return matches.length === 1 ? matches[0] : -1;
  }
  function textOutsideMath(block) {
    const walker = document.createTreeWalker(block, NodeFilter.SHOW_TEXT, {
      acceptNode: node => node.parentElement?.closest('.math')
        ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT,
    });
    const nodes = [];
    for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node);
    return nodes;
  }
  function findTextPartStart(text, part) {
    if (text.slice(part.start, part.start + part.quote.length) === part.quote) return part.start;
    const first = text.indexOf(part.quote);
    return first >= 0 && text.indexOf(part.quote, first + 1) < 0 ? first : -1;
  }
  function applyMark(block, start, length, record, textNodes = null) {
    const walker = textNodes ? null : document.createTreeWalker(block, NodeFilter.SHOW_TEXT);
    const segments = [];
    let cursor = 0;
    const nodes = textNodes || [];
    if (walker) for (let node = walker.nextNode(); node; node = walker.nextNode()) nodes.push(node);
    for (const node of nodes) {
      const end = cursor + node.length;
      if (end > start && cursor < start + length) {
        segments.push([node, Math.max(0, start - cursor), Math.min(node.length, start + length - cursor)]);
      }
      cursor = end;
    }
    for (const [node, first, last] of segments) {
      const middle = first ? node.splitText(first) : node;
      middle.splitText(last - first);
      const mark = document.createElement('mark');
      mark.className = 'reader-highlight' + (record.note ? ' has-note' : '')
        + (record.qaThreads?.length ? ' has-qa' : '');
      mark.dataset.annotationId = record.id;
      mark.dataset.starred = String(record.starred === true && Boolean(record.note.trim()));
      middle.replaceWith(mark);
      mark.appendChild(middle);
    }
  }
  function renderMarks() {
    document.dispatchEvent(new Event('ensemble-annotations-before-render'));
    clearMarks();
    let unavailable = 0;
    for (const record of records) {
      const block = blocks[record.block];
      if (block && record.textParts) {
        const text = textOutsideMath(block).map(node => node.textContent).join('');
        const positions = record.textParts.map(part => findTextPartStart(text, part));
        if (positions.some(position => position < 0)) { unavailable++; continue; }
        for (let index = 0; index < record.textParts.length; index++) {
          applyMark(block, positions[index], record.textParts[index].quote.length,
            record, textOutsideMath(block));
        }
        continue;
      }
      const start = block ? findStart(block, record) : -1;
      if (start < 0) { unavailable++; continue; }
      applyMark(block, start, record.quote.length, record);
    }
    document.dispatchEvent(new Event('ensemble-annotations-rendered'));
    renderFloats();
    if (unavailable) message(`${unavailable} 条批注未能定位；原记录仍保留，可在批注列表中查看。`);
  }
  function renderList() {
    window.MathJax?.typesetClear?.([list]);
    list.replaceChildren();
    function appendEntry(value, open, remove, current = false, starred = false) {
      const row = document.createElement('div'); row.className = 'annotation-list-row';
      row.classList.toggle('annotation-starred', starred);
      const content = document.createElement('div'); content.className = 'annotation-locate';
      content.setAttribute('role', 'button'); content.tabIndex = 0;
      content.setAttribute('aria-current', String(current));
      const summary = document.createElement('div');
      window.EnsembleReaderMarkdown.render(summary, value);
      const plain = [...summary.childNodes].map(node => node.textContent).join(' ').replace(/\s+/g, ' ').trim();
      content.textContent = plain.length > 140 ? plain.slice(0, 140) + '…' : plain;
      content.title = plain.slice(0, 300);
      if (starred) {
        const star = document.createElement('span'); star.className = 'annotation-star-icon';
        star.textContent = '★'; star.setAttribute('aria-hidden', 'true'); content.prepend(star);
        content.setAttribute('aria-label', '重要批注：' + plain.slice(0, 300));
      }
      content.addEventListener('click', event => { if (!event.target.closest('a')) open(); });
      content.addEventListener('keydown', event => {
        if (event.target !== content || !['Enter', ' '].includes(event.key)) return;
        event.preventDefault(); open();
      });
      const button = document.createElement('button');
      button.type = 'button'; button.className = 'annotation-remove';
      button.textContent = '删除'; button.title = '删除这条记录';
      button.addEventListener('click', remove);
      row.append(content, button); list.appendChild(row);
    }
    // Keep source records intact; show actual notes and saved Q&A, never plain highlights.
    const inReadingOrder = [...records].sort((left, right) =>
      left.block - right.block || left.start - right.start || left.id.localeCompare(right.id));
    for (const record of inReadingOrder) {
      if (!record.note.trim() && !record.qaThreads?.length) continue;
      if (record.note.trim()) appendEntry(record.note, () => openNoteEntry(record),
        () => deleteRecord(record.id), entryId === record.id, record.starred === true);
      for (const entry of record.qaThreads || []) appendEntry(entry.turns[0].question,
        () => openQaEntry(entry, record), () => deleteQaThread(entry.id), entryId === entry.id);
    }
    for (const entry of qaHistory().filter(entry => !records.some(record =>
      record.qaThreads?.some(thread => thread.id === entry.id)))) {
      appendEntry(entry.turns[0].question, () => openQaEntry(entry), () => deleteQaThread(entry.id), entryId === entry.id);
    }
    if (!list.childElementCount) list.textContent = '尚无批注或问答。';
  }
  function openEntry(title, value, id = null, continueAction = null, paired = false) {
    entryStar.hidden = true; delete entryStar.dataset.annotationId;
    entryId = id;
    const revision = ++entryRevision;
    panel.hidden = true; currentId = null;
    if (!paired) qaPanel.hidden = true;
    if (typeof closeReaderCards === 'function') closeReaderCards();
    document.getElementById('reader-entry-title').textContent = title;
    window.MathJax?.typesetClear?.([entryBody]);
    entryBody.replaceChildren();
    window.EnsembleReaderMarkdown.render(entryBody, value);
    entryContinue.hidden = !continueAction;
    entryContinue.onclick = continueAction;
    entryPanel.hidden = false;
    entryPanel.scrollTop = 0;
    window.EnsembleReaderUI.sync(); renderList();
    previewQueue = previewQueue.catch(() => {}).then(() => {
      if (revision === entryRevision && !entryPanel.hidden) return window.MathJax?.typesetPromise?.([entryBody]);
    }).catch(() => { message('全文中的公式暂未渲染；原始内容仍保留。'); });
  }
  function openNoteEntry(record) {
    openRecord(record.id);
    openEntry('批注全文', record.note, record.id);
    entryStar.dataset.annotationId = record.id; syncStarControls();
    entryBody.ondblclick = () => openRecord(record.id, true, false);
  }
  function openQaEntry(entry, record = null) {
    if (record) openRecord(record.id);
    const value = entry.turns.map(turn => '### ' + turn.question.replace(/\s+/g, ' ')
      + '\n\n' + turn.answer).join('\n\n---\n\n');
    openEntry('问答全文', value, entry.id, () => {
      const anchor = record ? { block:record.block, start:record.start, quote:record.quote } : null;
      if (record?.textParts) anchor.textParts = record.textParts;
      openQa(anchor, entry, true);
    });
    entryBody.ondblclick = null;
  }
  document.getElementById('reader-entry-close').addEventListener('click', () => {
    entryPanel.hidden = true; entryId = null;
    window.EnsembleReaderUI.sync(); renderList();
  });
  function openRecord(id, forceEdit = false, locate = true, showContext = false) {
    const record = records.find(item => item.id === id);
    if (!record) return;
    currentId = id;
    entryPanel.hidden = true; entryId = null;
    const target = article.querySelector(`mark.reader-highlight[data-annotation-id="${CSS.escape(id)}"]`);
    if (target && locate) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.classList.add('reader-located');
      setTimeout(() => target.classList.remove('reader-located'), 1800);
    }
    const editing = forceEdit || pendingNotes.has(id);
    if (!editing && !showContext) {
      panel.hidden = true; qaPanel.hidden = true;
      if (typeof closeReaderCards === 'function') closeReaderCards();
      window.EnsembleReaderUI.sync(); renderList();
      message(record.note.trim() ? '已定位批注原文；批注默认展开，双击可编辑。' : '已定位高亮；双击可添加批注。');
      return;
    }
    {
      quote.textContent = record.quote;
      note.value = pendingNotes.get(id) ?? record.note;
      renderNotePreview();
      detail.hidden = false;
      panel.hidden = false;
      setMode(editing);
    }
    if (typeof closeReaderCards === 'function') closeReaderCards();
    qaPanel.hidden = true;
    window.EnsembleReaderUI.sync();
    renderList();
  }
  function blockFor(node) {
    const element = node.nodeType === Node.ELEMENT_NODE ? node : node.parentElement;
    return element && element.closest('p,li,td,th,h1,h2,h3,h4,h5,h6');
  }
  function selectedTextAcrossMath(block, range, allowOverlap) {
    if (!allowOverlap && [...block.querySelectorAll('mark.reader-highlight')]
      .some(mark => range.intersectsNode(mark))) {
      message('所选普通文字与已有标注重叠；请先使用原标注或缩小选区。'); return null;
    }
    const textParts = [];
    let cursor = 0;
    for (const node of textOutsideMath(block)) {
      if (range.intersectsNode(node)) {
        const first = range.startContainer === node ? range.startOffset : 0;
        const last = range.endContainer === node ? range.endOffset : node.length;
        const quote = node.textContent.slice(first, last);
        if (quote) textParts.push({ start: cursor + first, quote });
      }
      cursor += node.length;
    }
    const selected = range.toString().trim();
    if (!textParts.some(part => part.quote.trim()) || selected.length > 2000
        || textParts.length > 100) {
      message('请在公式之外至少选中一段普通文字，且选区不超过 2000 字符。'); return null;
    }
    return { block: blocks.indexOf(block), start: textParts[0].start,
      quote: selected, textParts };
  }
  function selectedText(allowOverlap = false) {
    const selection = window.getSelection();
    const range = selection && !selection.isCollapsed && selection.rangeCount ? selection.getRangeAt(0) : selectedRange;
    if (!range || range.collapsed) { message('请先在正文同一段内选中需要标注的文字。'); return null; }
    const block = blockFor(range.startContainer);
    if (!block || block !== blockFor(range.endContainer) || !article.contains(block)) {
      message('请一次只标注同一段落、表格单元格或标题内的文字。'); return null;
    }
    if ([...block.querySelectorAll('.math')].some(math => range.intersectsNode(math)))
      return selectedTextAcrossMath(block, range, allowOverlap);
    const before = document.createRange();
    before.selectNodeContents(block);
    before.setEnd(range.startContainer, range.startOffset);
    const start = before.toString().length;
    const text = range.toString();
    if (!text.trim() || text.length > 2000 || block.textContent.slice(start, start + text.length) !== text) {
      message('所选文字无法稳定定位；请缩短选择范围后重试。'); return null;
    }
    const blockIndex = blocks.indexOf(block);
    if (!allowOverlap && records.some(item => item.block === blockIndex && start < item.start + item.quote.length && item.start < start + text.length)) {
      message('所选范围与已有标注重叠；请先删除旧标注。'); return null;
    }
    return { block: blockIndex, start, quote: text };
  }
  function addRecord(withNote) {
    menu.hidden = true;
    const anchor = selectedText();
    if (!anchor) return;
    if (records.length >= 500) { message('单份报告最多保存 500 条标注；请先导出和整理。'); return; }
    const record = { id: (window.crypto && window.crypto.randomUUID ? window.crypto.randomUUID() : String(Date.now()) + '-' + Math.random()), ...anchor, note: '' };
    records.push(record);
    persist(); renderMarks(); renderList();
    window.getSelection()?.removeAllRanges();
    selectedRange = null;
    if (withNote) { openRecord(record.id, true); note.focus(); }
    else message('高亮已保存于本机浏览器。');
  }
  document.addEventListener('selectionchange', () => {
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed && selection.rangeCount && article.contains(selection.anchorNode))
      selectedRange = selection.getRangeAt(0).cloneRange();
  });
  function selectedExcerpt() {
    const selection = window.getSelection();
    const range = selection && !selection.isCollapsed && selection.rangeCount ? selection.getRangeAt(0) : selectedRange;
    if (!range || range.collapsed || !article.contains(range.startContainer) || !article.contains(range.endContainer)) return '';
    return range.toString().trim().slice(0, 2000);
  }
  function showSelectionMenu() {
    const selection = window.getSelection();
    if (!selection || selection.isCollapsed || !selection.rangeCount || !selectedExcerpt()) { menu.hidden = true; return; }
    selectedRange = selection.getRangeAt(0).cloneRange();
    const rect = selectedRange.getBoundingClientRect();
    if (!rect.width && !rect.height) { menu.hidden = true; return; }
    menu.hidden = false;
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    menu.style.left = `${Math.max(8, Math.min(rect.left + rect.width / 2 - width / 2, window.innerWidth - width - 24))}px`;
    menu.style.top = `${rect.top > height + 12 ? rect.top - height - 8 : Math.min(window.innerHeight - height - 8, rect.bottom + 8)}px`;
  }
  article.addEventListener('pointerup', () => setTimeout(showSelectionMenu, 0));
  article.addEventListener('keyup', () => setTimeout(showSelectionMenu, 0));
  menu.addEventListener('pointerdown', event => event.preventDefault());
  document.addEventListener('pointerdown', event => { if (!menu.contains(event.target)) menu.hidden = true; });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') menu.hidden = true; });
  window.addEventListener('scroll', () => { menu.hidden = true; }, true);
  document.getElementById('annotation-highlight').addEventListener('click', () => addRecord(false));
  document.getElementById('annotation-create').addEventListener('click', () => addRecord(true));
  document.getElementById('annotation-close').addEventListener('click', () => { panel.hidden = true; currentId = null; });
  document.getElementById('annotation-collapse').addEventListener('click', () => {
    window.EnsembleReaderUI.tab('toc');
  });
  function openQa(anchor, savedEntry = null, paired = false) {
    qaAnchor = anchor;
    qaSavedEntry = savedEntry;
    qaFocusText = anchor?.quote || '';
    menu.hidden = true;
    if (!paired) { panel.hidden = true; entryPanel.hidden = true; entryId = null; currentId = null; }
    if (typeof closeReaderCards === 'function') closeReaderCards();
    qaPanel.hidden = false;
    if (paired && !panel.hidden) quote.hidden = false;
    window.EnsembleReaderUI.sync();
    if (!qaLoaded) {
      qaLoaded = true;
      qaFrame.removeAttribute('src');
      qaFrame.srcdoc = qaSource;
    } else {
      qaFrame.contentWindow.postMessage(savedEntry
        ? { type: 'ENSEMBLE_QA_OPEN_SAVED', entry: savedEntry }
        : { type: 'ENSEMBLE_QA_NEW_FOCUS', text: qaFocusText }, '*');
    }
  }
  function openQaForRecord(record, savedEntry = null) {
    openRecord(record.id, false, false, true);
    const anchor = { block: record.block, start: record.start, quote: record.quote };
    if (record.textParts) anchor.textParts = record.textParts;
    openQa(anchor, savedEntry, true);
    renderList();
  }
  document.getElementById('qa-open').addEventListener('click', () => {
    const anchor = selectedText(true);
    if (!anchor) return;
    const record = records.find(item => item.block === anchor.block && anchor.start < item.start + item.quote.length && item.start < anchor.start + anchor.quote.length);
    if (record) openQaForRecord(record, record.qaThreads?.at(-1)); else openQa(anchor);
  });
  document.getElementById('qa-open-general').addEventListener('click', () => openQa(null));
  document.getElementById('annotation-ask-ai').addEventListener('click', () => {
    const record = records.find(item => item.id === currentId);
    if (record) openQaForRecord(record, record.qaThreads?.at(-1));
  });
  qaFrame.addEventListener('load', () => {
    if (!qaPanel.hidden)
      qaFrame.contentWindow.postMessage(qaSavedEntry
        ? { type: 'ENSEMBLE_QA_OPEN_SAVED', entry: qaSavedEntry }
        : { type: 'ENSEMBLE_QA_NEW_FOCUS', text: qaFocusText }, '*');
  });
  function qaHistory() {
    const linked = records.flatMap(record => record.qaThreads || []);
    return [...new Map([...standaloneQaHistory, ...linked].map(entry => [entry.id, entry])).values()].slice(-30);
  }
  function deleteRecord(id) {
    const record = records.find(item => item.id === id);
    if (!record || !window.confirm('删除这条高亮、批注及关联问答？')) return;
    const threadIds = new Set((record.qaThreads || []).map(thread => thread.id));
    records = records.filter(item => item.id !== id);
    if (threadIds.size) removeLegacyQaThreads(threadIds);
    if (currentId === id) { currentId = null; detail.hidden = true; panel.hidden = true; }
    if (entryId === id || threadIds.has(entryId)) { entryId = null; entryPanel.hidden = true; }
    persist(); renderMarks(); renderList(); sendQaHistory();
  }
  function removeLegacyQaThreads(ids) {
    standaloneQaHistory = standaloneQaHistory.filter(entry => !ids.has(entry.id));
    try { localStorage.setItem(qaHistoryKey, JSON.stringify(standaloneQaHistory)); } catch (_) {}
  }
  function deleteQaThread(id) {
    if (typeof id !== 'string' || !id || !window.confirm('删除这条已保存的问答？')) return;
    removeLegacyQaThreads(new Set([id]));
    for (const record of records) {
      if (record.qaThreads) record.qaThreads = record.qaThreads.filter(thread => thread.id !== id);
    }
    records = records.filter(record => !(record.kind === 'qa' || record.qaOnly)
      || record.note || record.qaThreads?.length);
    if (qaSavedEntry?.id === id) qaSavedEntry = null;
    if (entryId === id) { entryId = null; entryPanel.hidden = true; }
    persist(); renderMarks(); renderList(); sendQaHistory();
    qaFrame.contentWindow.postMessage({ type: 'ENSEMBLE_QA_DELETED', id }, '*');
  }
  function sendQaHistory() {
    qaFrame.contentWindow.postMessage({ type: 'ENSEMBLE_QA_HISTORY', items: qaHistory() }, '*');
  }
  window.addEventListener('message', event => {
    if (event.source !== qaFrame.contentWindow || !event.data || typeof event.data !== 'object') return;
    if (event.data.type === 'ENSEMBLE_QA_READY') { sendQaHistory(); return; }
    if (event.data.type === 'ENSEMBLE_QA_EXPAND'
      && typeof event.data.question === 'string' && typeof event.data.answer === 'string'
      && event.data.question.length <= 1000 && event.data.answer.length <= 12000 && !qaPanel.hidden) {
      openEntry('问答全文', '### ' + event.data.question.replace(/\s+/g, ' ') + '\n\n' + event.data.answer,
        null, null, true);
      entryBody.ondblclick = null;
      return;
    }
    if (event.data.type === 'ENSEMBLE_QA_SELECT_THREAD' && typeof event.data.id === 'string') {
      const record = records.find(item => item.qaThreads?.some(thread => thread.id === event.data.id));
      if (record) openQaForRecord(record, record.qaThreads.find(thread => thread.id === event.data.id));
      else {
        const entry = qaHistory().find(item => item.id === event.data.id);
        if (entry) openQa(null, entry);
      }
      return;
    }
    if (event.data.type === 'ENSEMBLE_QA_NEW_CONTEXT') {
      qaAnchor = null; qaFocusText = ''; qaSavedEntry = null; panel.hidden = true; currentId = null;
      entryPanel.hidden = true; entryId = null;
      window.EnsembleReaderUI.sync(); renderList(); return;
    }
    if (event.data.type === 'ENSEMBLE_QA_SAVE') {
      const entry = event.data.entry;
      if (!validQaEntry(entry)) return;
      try {
        let record = records.find(item => item.qaThreads?.some(thread => thread.id === entry.id));
        if (!record && qaAnchor) record = records.find(item => item.block === qaAnchor.block
          && qaAnchor.start < item.start + item.quote.length
          && item.start < qaAnchor.start + qaAnchor.quote.length);
        if (!record && qaAnchor && records.length < 500) {
          record = { id: window.crypto?.randomUUID?.() || String(Date.now()) + '-' + Math.random(),
            ...qaAnchor, note: '', kind: 'highlight', qaOnly: true, qaThreads: [] };
          records.push(record);
        }
        if (!record && !qaAnchor) {
          const legacy = qaHistory().filter(item => item.id !== entry.id);
          standaloneQaHistory = [...legacy, entry].slice(-30);
          localStorage.setItem(qaHistoryKey, JSON.stringify(standaloneQaHistory));
          renderList(); sendQaHistory();
          qaFrame.contentWindow.postMessage({ type: 'ENSEMBLE_QA_SAVED' }, '*');
          return;
        }
        if (!record) throw new Error('问答未能关联正文，请重新选择文字');
        record.qaThreads = (record.qaThreads || []).filter(thread => thread.id !== entry.id);
        record.qaThreads.push(entry);
        record.qaThreads = record.qaThreads.slice(-30);
        if (!persist()) throw new Error('本地存储不可用');
        renderMarks(); renderList();
        sendQaHistory();
        qaFrame.contentWindow.postMessage({ type: 'ENSEMBLE_QA_SAVED' }, '*');
      } catch (_) {
        qaFrame.contentWindow.postMessage({ type: 'ENSEMBLE_QA_SAVE_FAILED' }, '*');
      }
    }
    if (event.data.type === 'ENSEMBLE_QA_DELETE') deleteQaThread(event.data.id);
    if (event.data.type === 'ENSEMBLE_QA_CLEAR_HISTORY') {
      standaloneQaHistory = [];
      if (entryContinue.onclick) { entryPanel.hidden = true; entryId = null; }
      try { localStorage.removeItem(qaHistoryKey); } catch (_) {}
      for (const record of records) delete record.qaThreads;
      records = records.filter(record => !(record.kind === 'qa' || record.qaOnly) || record.note);
      persist(); renderMarks(); renderList();
      sendQaHistory();
    }
  });
  document.getElementById('qa-close').addEventListener('click', () => {
    qaPanel.hidden = true;
    if (panel.dataset.mode !== 'edit') panel.hidden = true;
    window.EnsembleReaderUI.sync();
    qaFocusText = '';
    qaAnchor = null;
    qaSavedEntry = null;
  });
  note.addEventListener('input', () => {
    if (currentId) pendingNotes.set(currentId, note.value);
    document.getElementById('annotation-empty-message').hidden = Boolean(note.value.trim());
    clearTimeout(previewTimer);
    previewTimer = setTimeout(renderNotePreview, 180);
  });
  document.getElementById('annotation-save').addEventListener('click', () => {
    const record = records.find(item => item.id === currentId);
    if (!record) return;
    record.note = note.value.slice(0, 4000);
    const saved = persist();
    pendingNotes.delete(record.id); renderMarks(); renderList(); renderNotePreview(); setMode(false);
    if (qaPanel.hidden) { panel.hidden = true; window.EnsembleReaderUI.sync(); }
    message(saved ? '批注已保存；双击批注内容可编辑。' : '批注保留在当前页面；本地存储不可用，请另存带批注 HTML。');
  });
  document.getElementById('annotation-delete').addEventListener('click', () => {
    if (currentId) deleteRecord(currentId);
  });
  function download(content, type, suffix) {
    const url = URL.createObjectURL(new Blob([content], { type }));
    const link = document.createElement('a'); link.href = url;
    link.download = (document.title || 'ensemble-report').replace(/[\\/:*?"<>|]/g, '_').slice(0, 90) + suffix;
    link.click(); setTimeout(() => URL.revokeObjectURL(url), 30000);
  }
  function meetingOf(doc) {
    return doc.documentElement.dataset.meetingId
      || /^Project ENSEMBLE\s*·\s*(.+)$/.exec(doc.querySelector('main > .meta')?.textContent.trim() || '')?.[1]
      || '';
  }
  function canonicalText(element) {
    const copy = element.cloneNode(true);
    for (const math of copy.querySelectorAll('.math[data-tex]'))
      math.replaceWith('⟦MATH:' + math.getAttribute('data-tex') + '⟧');
    for (const node of copy.querySelectorAll('script,style')) node.remove();
    return copy.textContent.replace(/[（(]\s*(\[\d+(?:\s*[,，;；–-]\s*\d+)*\])\s*[）)]/g, '$1')
      .replace(/\s+/g, ' ').trim();
  }
  function snapshot() {
    return { report_meeting:meetingOf(document), source_text:canonicalText(article),
      source_blocks:blocks.map(canonicalText) };
  }
  function parseImport(content) {
    if (!content.trimStart().startsWith('<')) return JSON.parse(content);
    // Parse in an inert document: never run scripts or insert imported markup.
    const doc = new DOMParser().parseFromString(content, 'text/html');
    const body = doc.querySelector('main article');
    function jsonBlock(id, fallback) {
      const node = doc.getElementById(id);
      return node?.tagName === 'SCRIPT' && node.type === 'application/json'
        ? JSON.parse(node.textContent) : fallback;
    }
    return { format:'ENSEMBLE_READER_ANNOTATIONS_V1', report_key:doc.documentElement.dataset.annotationKey,
      records:jsonBlock('embedded-annotations', null), qa_history:jsonBlock('embedded-qa-history', []),
      report_meeting:meetingOf(doc), source_text:body ? canonicalText(body) : '',
      source_blocks:body ? [...body.querySelectorAll('p,li,td,th,h1,h2,h3,h4,h5,h6')].map(canonicalText) : [] };
  }
  function importAnchors(data) {
    const sameKey = data.report_key.split(':shared:')[0] === key.split(':shared:')[0];
    const context = snapshot();
    if (!sameKey && data.report_meeting && data.report_meeting !== context.report_meeting)
      throw new Error('不是同一会议的报告；无法确认批注来源，请导入旧页面保存的带批注 HTML 副本');
    const hasContext = Array.isArray(data.source_blocks) && data.source_blocks.length
      && data.source_blocks.length <= 100000 && data.source_blocks.every(text => typeof text === 'string')
      && typeof data.source_text === 'string' && data.source_text;
    if (!sameKey && !hasContext) {
      // V20 JSON exports contain only hashes and quotations. A hash mismatch
      // cannot prove that the report changed. Offer an explicit, quotation-only
      // migration, never attach an ambiguous match or infer its source identity.
      const mapped = data.records.map(record => {
        if (record.textParts) throw new Error('跨公式的旧 JSON 缺少定位上下文，请导入旧版带批注 HTML');
        const candidates = [];
        blocks.forEach((block, index) => {
          const text = block.textContent;
          let start = text.indexOf(record.quote);
          while (start >= 0 && candidates.length < 2) {
            candidates.push({block:index, start}); start = text.indexOf(record.quote, start + 1);
          }
        });
        if (candidates.length !== 1) throw new Error('旧 JSON 的批注“' + record.quote.slice(0, 35) + '”没有唯一原文匹配；请改为导入旧版带批注 HTML');
        return {...record, ...candidates[0]};
      });
      if (!mapped.length) throw new Error('旧 JSON 没有正文上下文或可定位的批注，请导入旧版带批注 HTML');
      if (!window.confirm('旧 JSON 的存储标识不同，且缺少正文／会议来源信息，无法自动确认是同一份报告。已在当前正文逐字、唯一匹配 ' + mapped.length + ' 条批注。是否按这些原文位置迁移？已有本机批注不会被覆盖。'))
        throw new Error('已取消旧 JSON 迁移；已有批注未改变');
      return mapped;
    }
    const sameBody = !sameKey && data.source_text === context.source_text;
    const positions = new Map();
    context.source_blocks.forEach((text, index) => {
      if (!positions.has(text)) positions.set(text, []);
      positions.get(text).push(index);
    });
    const incoming = data.records.map(record => {
      let index = record.block;
      if (!sameKey) {
        const source = data.source_blocks[record.block];
        const candidates = positions.get(source) || [];
        if (sameBody && context.source_blocks[index] === source) index = record.block;
        else if (candidates.length === 1) index = candidates[0];
        else throw new Error('批注“' + record.quote.slice(0, 35) + '”的原段落已改变或存在多处匹配；未修改已有批注');
      }
      const block = blocks[index];
      if (!block) throw new Error('批注“' + record.quote.slice(0, 35) + '”的原段落不存在');
      if (record.textParts) {
        const text = textOutsideMath(block).map(node => node.textContent).join('');
        const parts = record.textParts.map(part => ({...part, start:findTextPartStart(text, part)}));
        if (parts.some(part => part.start < 0)) throw new Error('跨公式批注无法完整定位；未修改已有批注');
        return {...record, block:index, start:parts[0].start, textParts:parts};
      }
      const start = findStart(block, record);
      if (start < 0) throw new Error('批注“' + record.quote.slice(0, 35) + '”的原文已改变或无法唯一定位');
      return {...record, block:index, start};
    });
    if (!sameKey && !sameBody && !window.confirm('这是同一会议的不同正文版本。全部批注已在未改变的原段落中准确定位。是否迁移这 ' + incoming.length + ' 条批注？已有本机批注不会被覆盖。'))
      throw new Error('已取消版本迁移；已有批注未改变');
    return incoming;
  }
  document.getElementById('annotation-export-data').addEventListener('click', () => {
    download(JSON.stringify({ format:'ENSEMBLE_READER_ANNOTATIONS_V1', report_key:key,
      records, qa_history:qaHistory(), ...snapshot() }, null, 2), 'application/json;charset=utf-8', '.annotations.json');
    message('已导出批注数据；新 HTML 的“批注与问答”中可导入。文件不包含 API key。');
  });
  const importFile = document.getElementById('annotation-import-file');
  const importStatus = document.getElementById('annotation-import-status');
  function importMessage(value) { importStatus.textContent = value; message(value); }
  document.getElementById('annotation-import-data').addEventListener('click', () => importFile.click());
  importFile.addEventListener('change', async () => {
    const file = importFile.files?.[0]; if (!file) return;
    importMessage('正在读取：' + file.name + '…');
    try {
      if (file.size > 20000000) throw new Error('批注文件过大');
      const content = await file.text();
      const data = parseImport(content);
      if (!data || typeof data !== 'object') throw new Error('批注数据格式无效');
      if (data.format !== 'ENSEMBLE_READER_ANNOTATIONS_V1'
          || typeof data.report_key !== 'string')
        throw new Error('缺少报告标识；请选择 ENSEMBLE 导出的批注 JSON 或带批注 HTML');
      if (!Array.isArray(data.records) || data.records.length > 500 || !data.records.every(validRecord)
          || !Array.isArray(data.qa_history) || data.qa_history.length > 30 || !data.qa_history.every(validQaEntry))
        throw new Error('批注数据格式无效');
      if (!data.records.length && !data.qa_history.length)
        throw new Error('文件内没有批注数据。浏览器里的批注不会自动写入原始 HTML；请在旧页面点击“保存带批注和问答的 HTML”后导入保存的副本');
      const incoming = importAnchors(data);
      // Merge conservatively: never overwrite an existing local record with a backup.
      const merged = [...new Map([...incoming, ...records].map(record => [record.id, record])).values()];
      if (merged.length > 500) throw new Error('合并后超过500条批注，请先整理');
      const history = [...new Map([...data.qa_history, ...qaHistory()].map(entry => [entry.id, entry])).values()].slice(-30);
      records = merged;
      standaloneQaHistory = history;
      const saved = persist();
      let historySaved = true;
      try { localStorage.setItem(qaHistoryKey, JSON.stringify(history)); }
      catch (_) { historySaved = false; }
      renderMarks(); renderList(); if (qaLoaded) sendQaHistory();
      window.EnsembleReaderUI.tab('annotations');
      importMessage(saved && historySaved ? '已导入 ' + incoming.length + ' 条标注、' + data.qa_history.length + ' 条问答；同ID的已有本机批注优先保留。' : '批注与问答已载入页面；本地存储不可用，请另存 HTML。');
    } catch (error) { importMessage('未导入：' + error.message); }
    finally { importFile.value = ''; }
  });
  document.getElementById('annotation-save-html').addEventListener('click', () => {
    const copy = document.documentElement.cloneNode(true);
    copy.dataset.annotationKey = key + ':shared:' + (window.crypto?.randomUUID?.() || String(Date.now()));
    const embeddedCopy = copy.querySelector('#embedded-annotations');
    embeddedCopy.textContent = JSON.stringify(records)
      .replace(/&/g, '\\u0026').replace(/</g, '\\u003c').replace(/>/g, '\\u003e')
      .replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
    copy.querySelector('#embedded-qa-history').textContent = JSON.stringify(qaHistory())
      .replace(/&/g, '\\u0026').replace(/</g, '\\u003c').replace(/>/g, '\\u003e')
      .replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
    for (const mark of copy.querySelectorAll('mark.reader-highlight,mark.reader-search-hit')) mark.replaceWith(...mark.childNodes);
    copy.querySelector('body').classList.remove('reader-paired');
    copy.querySelector('body').classList.remove('reader-focus-active');
    copy.querySelector('body').dataset.readerPanels = '0';
    copy.querySelector('body').style.removeProperty('--reader-panel-count');
    copy.querySelector('#reader-position-ticks').replaceChildren();
    copy.querySelector('#reader-star-rail').replaceChildren();
    for (const link of copy.querySelectorAll('#toc-panel a[aria-current]')) link.removeAttribute('aria-current');
    for (const row of copy.querySelectorAll('.reader-toc-current')) row.classList.remove('reader-toc-current');
    copy.querySelector('#annotation-floats').replaceChildren();
    copy.querySelector('#reader-search').setAttribute('value', '');
    copy.querySelector('#reader-search-status').textContent = '';
    copy.querySelector('#reader-drawer').hidden = true;
    copy.querySelector('#selection-menu').hidden = true;
    copy.querySelector('#annotation-panel').hidden = true;
    copy.querySelector('#qa-panel').hidden = true;
    copy.querySelector('#reader-entry-panel').hidden = true;
    copy.querySelector('#reader-entry-body').replaceChildren();
    const frame = copy.querySelector('#qa-frame');
    frame.removeAttribute('srcdoc'); frame.removeAttribute('src');
    copy.querySelector('#annotation-preview').replaceChildren();
    copy.querySelector('#annotation-quote').replaceChildren();
    copy.querySelector('#annotation-note').textContent = '';
    copy.querySelector('#annotation-list').replaceChildren();
    copy.querySelector('#annotation-status').textContent = '本副本包含嵌入的高亮、批注与已保存问答；不包含 API key。';
    const content = '<!doctype html>\n' + copy.outerHTML;
    const url = URL.createObjectURL(new Blob([content], { type: 'text/html;charset=utf-8' }));
    const link = document.createElement('a');
    link.href = url;
    link.download = (document.title || 'ensemble-report').replace(/[\\/:*?"<>|]/g, '_').slice(0, 90) + '.annotated.html';
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 30000);
    message('已保存带批注与问答的 HTML；请按需分享。API key 未写入该文件。');
  });
  article.addEventListener('click', event => {
    const mark = event.target.closest('mark.reader-highlight');
    if (mark) {
      event.stopPropagation();
      if (!window.getSelection()?.isCollapsed) return;
      if (window.EnsembleReaderFocus?.enabled) {
        window.EnsembleReaderFocus.pin(mark.dataset.annotationId);
        selectFloat(mark.dataset.annotationId); return;
      }
      openRecord(mark.dataset.annotationId); return;
    }
    if (!window.getSelection()?.isCollapsed || event.target.closest('.citation-trigger,.term-trigger,.note-trigger')) return;
    closePanels();
  });
  article.addEventListener('dblclick', event => {
    const mark = event.target.closest('mark.reader-highlight');
    if (!mark) return;
    event.preventDefault(); window.getSelection()?.removeAllRanges(); menu.hidden = true;
    openRecord(mark.dataset.annotationId, true, false);
  });
  article.addEventListener('contextmenu', event => {
    // Scope the override to our selection/highlight workflow. Shift keeps native behavior.
    if (event.shiftKey) return;
    const mark = event.target.closest('mark.reader-highlight');
    const selection = window.getSelection();
    if (selection && !selection.isCollapsed && article.contains(selection.anchorNode)) {
      event.preventDefault(); showSelectionMenu();
    } else if (mark) {
      event.preventDefault(); openRecord(mark.dataset.annotationId, false, false);
    }
  });
  document.getElementById('selection-copy').addEventListener('click', async () => {
    const text = selectedExcerpt();
    if (!text) return;
    try {
      if (!navigator.clipboard?.writeText) throw new Error('clipboard');
      await navigator.clipboard.writeText(text); menu.hidden = true; message('已复制选中文字。');
    } catch (_) { message('浏览器未开放剪贴板权限；请使用 Ctrl/Cmd+C 复制。'); }
  });
  document.addEventListener('ensemble-reader-card-open', closePanels);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') { closePanels(); return; }
    if (event.target.closest('input,textarea,[contenteditable="true"]')) return;
    if (!event.altKey || !event.shiftKey) return;
    const command = event.key.toLowerCase();
    if (!['h', 'n', 'a'].includes(command)) return;
    if (!selectedExcerpt() && !(command === 'a' && currentId)) return;
    event.preventDefault();
    if (command === 'h') addRecord(false);
    if (command === 'n') addRecord(true);
    if (command === 'a') {
      const record = records.find(item => item.id === currentId);
      if (record) openQaForRecord(record); else document.getElementById('qa-open').click();
    }
  });
  if (storageAvailable) message('标注仅存于本机浏览器，不写入会议文件。');
  renderMarks(); renderList();
  if (window.MathJax && window.MathJax.startup && window.MathJax.startup.promise)
    window.MathJax.startup.promise.then(() => {
      renderMarks(); if (!panel.hidden) renderNotePreview();
      if (!entryPanel.hidden) previewQueue = previewQueue.catch(() => {}).then(() =>
        window.MathJax.typesetPromise([entryBody])).catch(() => {});
    }).catch(() => {});
})();
