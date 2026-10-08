(() => {
  'use strict';
  const article = document.querySelector('main article');
  const floats = document.getElementById('annotation-floats');
  const toggle = document.getElementById('reader-focus-toggle');
  const palette = document.getElementById('reader-palette');
  const status = document.getElementById('reader-focus-status');
  if (!article || !floats || !toggle || !palette) return;
  const preferencesKey = 'ensemble-reader:appearance-v1';
  let enabled = document.body.dataset.readerFocus === 'on';
  let hoveredId = null, pinnedId = null, activeId = null, frame = null;
  const marks = id => [...article.querySelectorAll('mark.reader-highlight')]
    .filter(mark => mark.dataset.annotationId === id);
  function valid(id) { return id && marks(id).length ? id : null; }
  function nearestVisible() {
    let nearest = null, distance = Infinity;
    const ids = new Set([...floats.querySelectorAll('.annotation-float')].map(card => card.dataset.annotationId));
    for (const mark of article.querySelectorAll('mark.reader-highlight')) {
      const id = mark.dataset.annotationId;
      if (!ids.has(id)) continue;
      const rect = mark.getBoundingClientRect();
      if (rect.top + rect.height <= 0 || rect.top >= window.innerHeight) continue;
      const candidate = Math.abs(rect.top + rect.height / 2 - window.innerHeight / 2);
      if (candidate < distance) { nearest = id; distance = candidate; }
    }
    return nearest;
  }
  function refresh() {
    frame = null;
    pinnedId = valid(pinnedId); hoveredId = valid(hoveredId);
    const next = enabled ? hoveredId || pinnedId || nearestVisible() : null;
    const changed = next !== activeId;
    activeId = next;
    document.body.dataset.readerFocus = enabled ? 'on' : 'off';
    document.body.classList.toggle('reader-focus-active', Boolean(activeId));
    toggle.setAttribute('aria-pressed', String(enabled));
    toggle.textContent = enabled ? '聚焦模式：开' : '聚焦模式：关';
    for (const mark of article.querySelectorAll('mark.reader-highlight'))
      mark.classList.toggle('reader-focus-target', mark.dataset.annotationId === activeId);
    for (const card of floats.querySelectorAll('.annotation-float'))
      card.classList.toggle('annotation-focused', card.dataset.annotationId === activeId);
    status.textContent = !enabled ? '' : pinnedId ? '已锁定；点击空白处或按 Esc 解除' :
      '指向预览，单击锁定；未指向时跟随页面中线';
    if (changed) document.dispatchEvent(new Event('ensemble-reader-focus-changed'));
  }
  function schedule() { if (!frame) frame = requestAnimationFrame(refresh); }
  function persist() {
    try { localStorage.setItem(preferencesKey, JSON.stringify({focus:enabled, palette:palette.value})); }
    catch (_) { /* Appearance remains usable when browser storage is unavailable. */ }
  }
  function setPalette(value) {
    palette.value = value === 'butter' ? 'butter' : 'white';
    document.documentElement.dataset.readerPalette = palette.value;
    document.getElementById('qa-frame')?.contentWindow?.postMessage(
      {type:'ENSEMBLE_QA_APPEARANCE', palette:palette.value}, '*');
  }
  function pin(id) { if (!enabled) return; pinnedId = valid(id); refresh(); }
  function pointerTarget(event) {
    const mark = event.target.closest('mark.reader-highlight');
    if (mark && article.contains(mark)) return mark.dataset.annotationId;
    const card = event.target.closest('.annotation-float');
    if (card && floats.contains(card)) return card.dataset.annotationId;
    // A paragraph/cell containing one annotation can also be pointed at.
    // Do not silently pick one when several different annotations share a block.
    const block = event.target.closest('p,li,td,th,h1,h2,h3,h4,h5,h6');
    if (!block || !article.contains(block) || event.target.closest('a,button,input,textarea')) return null;
    const ids = new Set([...block.querySelectorAll('mark.reader-highlight')].map(mark => mark.dataset.annotationId));
    return ids.size === 1 ? [...ids][0] : null;
  }
  window.EnsembleReaderFocus = {get enabled() { return enabled; }, get activeId() { return activeId; }, pin};
  document.addEventListener('pointerover', event => {
    if (!enabled || event.pointerType === 'touch' || !window.getSelection()?.isCollapsed) return;
    const id = pointerTarget(event);
    if (id !== hoveredId) { hoveredId = id; refresh(); }
  });
  document.addEventListener('pointerout', event => {
    if (!enabled || !hoveredId) return;
    const related = event.relatedTarget;
    const id = related instanceof Element ? pointerTarget({target:related}) : null;
    if (id !== hoveredId) { hoveredId = id; refresh(); }
  });
  document.addEventListener('click', event => {
    if (!enabled || !window.getSelection()?.isCollapsed ||
        event.target.closest('button,a,input,textarea,[contenteditable="true"]')) return;
    const id = pointerTarget(event);
    if (id) pin(id);
    else if (event.target.closest('main,.shell') || event.target === document.body) {
      pinnedId = null; hoveredId = null; refresh();
    }
  }, true);
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') { hoveredId = null; pinnedId = null; refresh(); }
  });
  floats.addEventListener('focusin', event => {
    if (enabled) { hoveredId = pointerTarget(event); refresh(); }
  });
  floats.addEventListener('focusout', event => {
    if (!floats.contains(event.relatedTarget)) { hoveredId = null; refresh(); }
  });
  floats.addEventListener('keydown', event => {
    if (enabled && ['Enter', ' '].includes(event.key) && !event.target.closest('button,a,input,textarea')) {
      event.preventDefault(); pin(pointerTarget(event));
    }
  });
  toggle.addEventListener('click', () => {
    enabled = !enabled; hoveredId = null; pinnedId = null; refresh(); persist();
  });
  palette.addEventListener('change', () => { setPalette(palette.value); persist(); });
  document.getElementById('qa-frame')?.addEventListener('load', () => setPalette(palette.value));
  for (const name of ['ensemble-reader-layout', 'ensemble-annotations-rendered',
                      'ensemble-reader-focus-refresh'])
    document.addEventListener(name, schedule);
  window.addEventListener('scroll', schedule, {passive:true});
  window.addEventListener('resize', schedule);
  window.addEventListener('load', schedule);
  // Search fallback marks and asynchronous MathJax rendering may replace spans.
  new MutationObserver(schedule).observe(article, {childList:true, subtree:true});
  try {
    const saved = JSON.parse(localStorage.getItem(preferencesKey) || 'null');
    enabled = typeof saved?.focus === 'boolean' ? saved.focus : document.body.dataset.readerFocus === 'on';
    setPalette(saved?.palette || document.documentElement.dataset.readerPalette);
  } catch (_) { setPalette(document.documentElement.dataset.readerPalette); }
  refresh();
})();
