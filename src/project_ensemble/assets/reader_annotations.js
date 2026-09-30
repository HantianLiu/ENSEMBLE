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

  function escapedDollar(value, index) {
    let slashes = 0;
    for (let cursor = index - 1; cursor >= 0 && value[cursor] === '\\'; cursor--) slashes++;
    return slashes % 2 === 1;
  }
  function closingDollar(value, from, delimiter) {
    for (let index = value.indexOf(delimiter, from); index >= 0;
         index = value.indexOf(delimiter, index + delimiter.length)) {
      if (!escapedDollar(value, index) && (delimiter === '$$' || value[index + 1] !== '$'))
        return index;
    }
    return -1;
  }
  function notePreviewNodes(value) {
    const nodes = document.createDocumentFragment();
    let plainStart = 0;
    for (let index = 0; index < value.length;) {
      if (value[index] !== '$' || escapedDollar(value, index)) { index++; continue; }
      const delimiter = value[index + 1] === '$' ? '$$' : '$';
      const end = closingDollar(value, index + delimiter.length, delimiter);
      if (end < 0 || (delimiter === '$' && value.slice(index, end).includes('\n'))) {
        index += delimiter.length;
        continue;
      }
      const expression = value.slice(index + delimiter.length, end);
      if (!expression.trim()) { index = end + delimiter.length; continue; }
      nodes.appendChild(document.createTextNode(value.slice(plainStart, index)));
      // Keep the preview text-only: do not allow annotations to inject HTML
      // or load MathJax extensions, links, external assets, or new macros.
      if (/\\(?:href|url|require|html\w*|includegraphics|newcommand|renewcommand|def|let|input|write)\b/i.test(expression)) {
        nodes.appendChild(document.createTextNode(value.slice(index, end + delimiter.length)));
      } else {
        const formula = document.createElement(delimiter === '$$' ? 'div' : 'span');
        formula.className = delimiter === '$$' ? 'annotation-math-display' : 'annotation-math-inline';
        formula.textContent = delimiter === '$$' ? '\\[' + expression + '\\]' : '\\(' + expression + '\\)';
        nodes.appendChild(formula);
      }
      index = end + delimiter.length;
      plainStart = index;
    }
    nodes.appendChild(document.createTextNode(value.slice(plainStart)));
    return nodes;
  }
  function renderNotePreview() {
    const revision = ++previewRevision;
    const value = note.value;
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
      && Number.isInteger(value.block) && value.block >= 0 && value.block < blocks.length
      && Number.isInteger(value.start) && value.start >= 0
      && typeof value.quote === 'string' && value.quote.length > 0 && value.quote.length <= 2000
      && typeof value.note === 'string' && value.note.length <= 4000
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
      middle.replaceWith(mark);
      mark.appendChild(middle);
    }
  }
  function renderMarks() {
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
    if (unavailable) message(`${unavailable} 条批注未能定位；原记录仍保留，可在批注列表中查看。`);
  }
  function renderList() {
    list.replaceChildren();
    // Keep the saved records intact, but navigate highlights in reading order.
    const inReadingOrder = [...records].sort((left, right) =>
      left.block - right.block || left.start - right.start || left.id.localeCompare(right.id));
    for (const record of inReadingOrder) {
      const row = document.createElement('div');
      row.className = 'annotation-list-row';
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'annotation-locate';
      const qaCount = record.qaThreads?.length || 0;
      button.textContent = (record.note ? '批注' : '高亮')
        + (qaCount ? `＋问答 ${qaCount}` : '') + ' · ' + record.quote.slice(0, 80);
      button.title = record.quote;
      button.setAttribute('aria-current', currentId === record.id ? 'true' : 'false');
      button.addEventListener('click', () => {
        if (currentId === record.id) {
          panel.hidden = true;
          currentId = null;
          renderList();
        } else openRecord(record.id);
      });
      row.appendChild(button);
      if (!record.note) {
        const edit = document.createElement('button');
        edit.type = 'button'; edit.className = 'annotation-add-note';
        edit.textContent = '批注'; edit.title = '为这条高亮添加批注';
        edit.addEventListener('click', () => openRecord(record.id, true));
        row.appendChild(edit);
      }
      const ask = document.createElement('button');
      ask.type = 'button'; ask.className = 'annotation-ask-ai';
      ask.textContent = '问 AI'; ask.title = '就这段文字提问或继续已保存的问答';
      ask.addEventListener('click', () => openQaForRecord(record, record.qaThreads?.at(-1)));
      row.appendChild(ask);
      const remove = document.createElement('button');
      remove.type = 'button'; remove.className = 'annotation-remove';
      remove.textContent = '删除'; remove.title = '删除这条高亮、批注和关联问答';
      remove.addEventListener('click', () => deleteRecord(record.id));
      row.appendChild(remove);
      list.appendChild(row);
    }
    if (!records.length) list.textContent = '尚无高亮、批注或问答。';
  }
  function openRecord(id, forceEdit = false) {
    const record = records.find(item => item.id === id);
    if (!record) return;
    currentId = id;
    const target = article.querySelector(`mark.reader-highlight[data-annotation-id="${CSS.escape(id)}"]`);
    if (target) {
      target.scrollIntoView({ behavior: 'smooth', block: 'center' });
      target.classList.add('reader-located');
      setTimeout(() => target.classList.remove('reader-located'), 1800);
    }
    if (record.note || forceEdit || record.qaThreads?.length) {
      quote.textContent = record.quote;
      note.value = record.note;
      renderNotePreview();
      detail.hidden = false;
      panel.hidden = false;
    } else {
      panel.hidden = true;
      message('已定位高亮；点击左侧“批注”可为它添加说明。');
    }
    if (typeof closeReaderCards === 'function') closeReaderCards();
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
    menu.style.left = `${Math.max(8, Math.min(rect.left + rect.width / 2 - width / 2, window.innerWidth - width - 8))}px`;
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
    document.getElementById('annotation-tools').open = false;
  });
  function openQa(anchor, savedEntry = null) {
    qaAnchor = anchor;
    qaSavedEntry = savedEntry;
    qaFocusText = anchor.quote;
    menu.hidden = true;
    panel.hidden = true;
    if (typeof closeReaderCards === 'function') closeReaderCards();
    qaPanel.hidden = false;
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
    currentId = record.id;
    openQa({ block: record.block, start: record.start, quote: record.quote }, savedEntry);
    renderList();
  }
  document.getElementById('qa-open').addEventListener('click', () => {
    const anchor = selectedText(true);
    if (anchor) openQa(anchor);
  });
  document.getElementById('annotation-ask-ai').addEventListener('click', () => {
    const record = records.find(item => item.id === currentId);
    if (record) openQaForRecord(record, record.qaThreads?.at(-1));
  });
  qaFrame.addEventListener('load', () => {
    if (!qaPanel.hidden && qaFocusText)
      qaFrame.contentWindow.postMessage(qaSavedEntry
        ? { type: 'ENSEMBLE_QA_OPEN_SAVED', entry: qaSavedEntry }
        : { type: 'ENSEMBLE_QA_NEW_FOCUS', text: qaFocusText }, '*');
  });
  function qaHistory() {
    try {
      const value = JSON.parse(localStorage.getItem(qaHistoryKey) || '[]');
      const legacy = Array.isArray(value) ? value : [];
      const linked = records.flatMap(record => record.qaThreads || []);
      return [...new Map([...legacy, ...linked].map(entry => [entry.id, entry])).values()].slice(-30);
    } catch (_) { return records.flatMap(record => record.qaThreads || []).slice(-30); }
  }
  function deleteRecord(id) {
    const record = records.find(item => item.id === id);
    if (!record || !window.confirm('删除这条高亮、批注及关联问答？')) return;
    const threadIds = new Set((record.qaThreads || []).map(thread => thread.id));
    records = records.filter(item => item.id !== id);
    if (threadIds.size) removeLegacyQaThreads(threadIds);
    if (currentId === id) { currentId = null; detail.hidden = true; panel.hidden = true; }
    persist(); renderMarks(); renderList(); sendQaHistory();
  }
  function removeLegacyQaThreads(ids) {
    try {
      const value = JSON.parse(localStorage.getItem(qaHistoryKey) || '[]');
      if (Array.isArray(value)) localStorage.setItem(qaHistoryKey,
        JSON.stringify(value.filter(entry => !ids.has(entry?.id))));
    } catch (_) {}
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
    persist(); renderMarks(); renderList(); sendQaHistory();
    qaFrame.contentWindow.postMessage({ type: 'ENSEMBLE_QA_DELETED', id }, '*');
  }
  function sendQaHistory() {
    qaFrame.contentWindow.postMessage({ type: 'ENSEMBLE_QA_HISTORY', items: qaHistory() }, '*');
  }
  window.addEventListener('message', event => {
    if (event.source !== qaFrame.contentWindow || !event.data || typeof event.data !== 'object') return;
    if (event.data.type === 'ENSEMBLE_QA_READY') { sendQaHistory(); return; }
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
      try { localStorage.removeItem(qaHistoryKey); } catch (_) {}
      for (const record of records) delete record.qaThreads;
      records = records.filter(record => !(record.kind === 'qa' || record.qaOnly) || record.note);
      persist(); renderMarks(); renderList();
      sendQaHistory();
    }
  });
  document.getElementById('qa-close').addEventListener('click', () => {
    qaPanel.hidden = true;
    qaFocusText = '';
    qaAnchor = null;
    qaSavedEntry = null;
  });
  note.addEventListener('input', () => {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(renderNotePreview, 180);
  });
  document.getElementById('annotation-save').addEventListener('click', () => {
    const record = records.find(item => item.id === currentId);
    if (!record) return;
    record.note = note.value.slice(0, 4000);
    persist(); renderMarks(); renderList(); message('批注已保存于本机浏览器。');
  });
  document.getElementById('annotation-delete').addEventListener('click', () => {
    if (currentId) deleteRecord(currentId);
  });
  document.getElementById('annotation-save-html').addEventListener('click', () => {
    const copy = document.documentElement.cloneNode(true);
    copy.dataset.annotationKey = key + ':shared:' + (window.crypto?.randomUUID?.() || String(Date.now()));
    const embeddedCopy = copy.querySelector('#embedded-annotations');
    embeddedCopy.textContent = JSON.stringify(records)
      .replace(/&/g, '\\u0026').replace(/</g, '\\u003c').replace(/>/g, '\\u003e')
      .replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
    for (const mark of copy.querySelectorAll('mark.reader-highlight')) mark.replaceWith(...mark.childNodes);
    copy.querySelector('#selection-menu').hidden = true;
    copy.querySelector('#annotation-panel').hidden = true;
    copy.querySelector('#qa-panel').hidden = true;
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
      openRecord(mark.dataset.annotationId); return;
    }
    if (!panel.hidden) panel.hidden = true;
  });
  if (storageAvailable) message('标注仅存于本机浏览器，不写入会议文件。');
  renderMarks(); renderList();
  if (window.MathJax && window.MathJax.startup && window.MathJax.startup.promise)
    window.MathJax.startup.promise.then(renderMarks).catch(() => {});
})();
