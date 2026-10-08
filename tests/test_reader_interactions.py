"""DOM-level regressions; optional jsdom is a test tool, not a runtime dependency."""

import hashlib
import shutil
import subprocess

import pytest

from project_ensemble.orchestration.academic_html import render_academic_review_html


def _has_dom_tool():
    return bool(shutil.which("node")) and subprocess.run(
        ["node", "-e", "require('jsdom')"], capture_output=True,
    ).returncode == 0


pytestmark = pytest.mark.skipif(not _has_dom_tool(), reason="Optional Node/jsdom DOM test tool unavailable")

BOOT = r"""
const assert = require('assert/strict');
const {JSDOM} = require('jsdom');
const html = require('fs').readFileSync(0, 'utf8');
const dom = new JSDOM(html, {url:'https://reader.example/report.html', runScripts:'outside-only', pretendToBeVisual:true});
const w = dom.window, d = w.document, get = id => d.getElementById(id);
w.CSS = {escape: value => value};
w.HTMLElement.prototype.scrollIntoView = function() {};
w.Range.prototype.getBoundingClientRect = () => ({top:100,bottom:120,left:100,right:200,width:100,height:20});
w.scrollTo = () => {};
w.confirm = () => true;
const original = {id:'legacy-one',block:1,start:0,quote:'Alpha beta',note:'旧批注 $x^2$'};
const key = d.documentElement.dataset.annotationKey;
w.localStorage.setItem(key, JSON.stringify([original]));
for (const script of d.querySelectorAll('script:not([type="application/json"])')) if (!script.src) w.eval(script.textContent);
w.MathJax = {typesetClear(){},typesetPromise(){return Promise.resolve();}};
const flush = () => new Promise(resolve => setTimeout(resolve, 20));
const pane = get('annotation-panel'), qa = get('qa-panel');
function askFromNote() {
  const card=d.querySelector('.annotation-float'); card.click();
  card.querySelector('.annotation-float-actions').querySelectorAll('button')[1].click();
}
function send(type, extra={}) {
  w.dispatchEvent(new w.MessageEvent('message', {source:get('qa-frame').contentWindow, data:{type,...extra}}));
}
(async () => {
"""


def run_dom(script, before_boot="", markdown="# Report\n\nAlpha beta and Alpha gamma.\n\nDelta beta.\n"):
    html = render_academic_review_html(
        markdown, meeting_id="LR-OLD",
    )
    result = subprocess.run(
        ["node", "-e", BOOT.replace("for (const script of d.querySelectorAll", before_boot + "\nfor (const script of d.querySelectorAll") + script + "\n dom.window.close();\n})().catch(error => {console.error(error); process.exit(1);});"],
        input=html, text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr


def test_saved_annotation_view_double_click_edit_and_legacy_storage():
    run_dom(r"""
assert.equal(JSON.parse(w.localStorage.getItem(key))[0].note, original.note);
get('annotation-list').querySelector('.annotation-locate').click(); await flush();
assert.equal(pane.hidden, true);
assert.equal(get('reader-entry-panel').hidden,false);
assert.equal(get('reader-entry-body').querySelector('.math-inline').textContent,'\\(x^2\\)');
get('reader-entry-close').click(); await flush();
const floating = d.querySelector('.annotation-float');
assert.equal(floating.hidden, false);
assert.equal(get('annotation-quote').hidden, true);
assert.equal(get('annotation-editor').hidden, true);
assert.equal(get('annotation-save').hidden, true);
assert.equal(floating.querySelector('.math-inline').textContent, '\\(x^2\\)');
floating.querySelector('.annotation-preview').dispatchEvent(new w.MouseEvent('dblclick',{bubbles:true}));
assert.equal(pane.dataset.mode,'edit'); assert.equal(get('annotation-editor').hidden,false);
get('annotation-note').value = '**新批注** $y$\n\n|项|值|\n|---|---:|\n|A|2|';
get('annotation-note').dispatchEvent(new w.Event('input'));
get('annotation-save').click(); await flush();
assert.equal(pane.dataset.mode,'view'); assert.equal(get('annotation-editor').hidden,true);
assert.equal(pane.hidden,true);
assert.equal(floating.querySelector('strong').textContent,'新批注');
assert.equal(floating.querySelectorAll('table tbody tr').length,1);
const saved = JSON.parse(w.localStorage.getItem(key));
assert.equal(saved[0].note,get('annotation-note').value);
assert.deepEqual(Object.keys(saved[0]).sort(),Object.keys(original).sort());
floating.querySelector('.annotation-preview').dispatchEvent(new w.MouseEvent('dblclick',{bubbles:true})); get('annotation-note').value = '未保存';
get('annotation-note').dispatchEvent(new w.Event('input')); get('annotation-cancel').click();
assert.equal(get('annotation-note').value,saved[0].note); assert.equal(pane.dataset.mode,'view');
d.querySelector('main article p').click(); assert.equal(pane.hidden,true);
""")


def test_focus_hover_pin_clear_and_no_annotation_mutation():
    run_dom(r"""
const snapshot=w.localStorage.getItem(key), card=d.querySelector('.annotation-float');
const mark=d.querySelector('mark.reader-highlight'), toggle=get('reader-focus-toggle');
assert.equal(toggle.getAttribute('aria-pressed'),'false');
assert.equal(d.body.classList.contains('reader-focus-active'),false);
toggle.click(); await flush();
mark.dispatchEvent(new w.MouseEvent('pointerover',{bubbles:true}));
assert.equal(mark.classList.contains('reader-focus-target'),true);
assert.equal(card.classList.contains('annotation-focused'),true);
mark.click(); await flush();
assert.equal(pane.hidden,true); assert.equal(get('reader-entry-panel').hidden,true);
assert.match(get('reader-focus-status').textContent,/已锁定/);
mark.dispatchEvent(new w.MouseEvent('pointerout',{bubbles:true,relatedTarget:d.body}));
assert.equal(mark.classList.contains('reader-focus-target'),true);
d.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape',bubbles:true}));
assert.doesNotMatch(get('reader-focus-status').textContent,/已锁定/);
card.click(); assert.match(get('reader-focus-status').textContent,/已锁定/);
d.querySelectorAll('main article p')[1].click();
assert.doesNotMatch(get('reader-focus-status').textContent,/已锁定/);
toggle.click(); await flush();
assert.equal(mark.classList.contains('reader-focus-target'),false);
assert.equal(card.classList.contains('annotation-focused'),false);
assert.equal(w.localStorage.getItem(key),snapshot);
assert.equal(JSON.parse(w.localStorage.getItem('ensemble-reader:appearance-v1')).focus,false);
""")


def test_focus_priority_over_midline_and_hover_restores_pinned_annotation():
    run_dom(r"""
const second={id:'second',block:2,start:0,quote:'Delta beta',note:'第二条'};
const data=JSON.stringify({format:'ENSEMBLE_READER_ANNOTATIONS_V1',report_key:key,records:[second],qa_history:[]});
Object.defineProperty(get('annotation-import-file'),'files',{configurable:true,value:[{size:data.length,text:async()=>data}]});
get('annotation-import-file').dispatchEvent(new w.Event('change')); await flush();
const first=d.querySelector('mark[data-annotation-id="legacy-one"]'), other=d.querySelector('mark[data-annotation-id="second"]');
const cards=[...d.querySelectorAll('.annotation-float')];
Object.defineProperty(w,'innerHeight',{configurable:true,value:700});
first.getBoundingClientRect=()=>({top:80,height:20});
other.getBoundingClientRect=()=>({top:340,height:20});
get('reader-focus-toggle').click();w.dispatchEvent(new w.Event('resize'));await flush();
assert.equal(other.classList.contains('reader-focus-target'),true);
assert.equal(cards[1].classList.contains('annotation-foremost'),true);
first.click();await flush();
assert.equal(cards[0].classList.contains('annotation-foremost'),true);
assert.ok(Number(cards[0].style.zIndex)>Number(cards[1].style.zIndex));
cards[1].dispatchEvent(new w.MouseEvent('pointerover',{bubbles:true}));await flush();
assert.equal(other.classList.contains('reader-focus-target'),true);
assert.equal(cards[1].classList.contains('annotation-foremost'),true);
cards[1].dispatchEvent(new w.MouseEvent('pointerout',{bubbles:true,relatedTarget:d.body}));await flush();
assert.equal(first.classList.contains('reader-focus-target'),true);
assert.equal(cards[0].classList.contains('annotation-foremost'),true);
d.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Escape'}));await flush();
assert.equal(other.classList.contains('reader-focus-target'),true);
first.getBoundingClientRect=()=>({top:-200,height:20});other.getBoundingClientRect=()=>({top:900,height:20});
w.dispatchEvent(new w.Event('scroll'));await flush();
assert.equal(d.body.classList.contains('reader-focus-active'),false);
""")


def test_focus_survives_search_mark_rebuild_and_deleting_note():
    run_dom(r"""
get('reader-focus-toggle').click();
d.querySelector('mark.reader-highlight').click();
get('reader-search').value='Alpha';get('reader-search').dispatchEvent(new w.Event('input'));
await new Promise(resolve=>setTimeout(resolve,220));
assert.equal(d.querySelector('mark.reader-highlight').classList.contains('reader-focus-target'),true);
const card=d.querySelector('.annotation-float');
card.querySelector('.annotation-preview').dispatchEvent(new w.MouseEvent('dblclick',{bubbles:true}));
get('annotation-note').value='更新后 $y^2$';get('annotation-save').click();await flush();
assert.equal(d.querySelector('mark.reader-highlight').classList.contains('reader-focus-target'),true);
assert.equal(card.classList.contains('annotation-focused'),true);
card.click();card.querySelector('.annotation-float-actions button:nth-child(3)').click();await flush();
assert.equal(d.querySelector('mark.reader-focus-target'),null);
assert.equal(d.body.classList.contains('reader-focus-active'),false);
""")


def test_palette_changes_are_separate_from_annotations_and_sync_to_qa():
    run_dom(r"""
const snapshot=w.localStorage.getItem(key), palette=get('reader-palette');
let messages=[];get('qa-frame').contentWindow.postMessage=value=>messages.push(value);
palette.value='butter';palette.dispatchEvent(new w.Event('change'));
assert.equal(d.documentElement.dataset.readerPalette,'butter');
assert.equal(w.getComputedStyle(d.body).backgroundColor,'rgb(255, 254, 251)');
assert.equal(JSON.parse(w.localStorage.getItem('ensemble-reader:appearance-v1')).palette,'butter');
assert.equal(messages.at(-1).type,'ENSEMBLE_QA_APPEARANCE');
assert.equal(messages.at(-1).palette,'butter');
palette.value='white';palette.dispatchEvent(new w.Event('change'));
assert.equal(w.getComputedStyle(d.body).backgroundColor,'rgb(255, 255, 255)');
assert.equal(w.localStorage.getItem(key),snapshot);
const css=d.querySelector('style').textContent;
assert.match(css,/translateY\(-6px\) scale\(1.012\)/);
assert.match(css,/prefers-reduced-motion:reduce/);
assert.match(css,/body.reader-focus-active article img \{ filter:none; opacity:1; \}/);
""")


def test_focus_preferences_restore_and_keyboard_pin_without_schema_changes():
    run_dom(r"""
assert.equal(get('reader-focus-toggle').getAttribute('aria-pressed'),'true');
assert.equal(d.documentElement.dataset.readerPalette,'butter');
const content=d.querySelector('.annotation-float .annotation-preview');
content.focus();
content.dispatchEvent(new w.KeyboardEvent('keydown',{key:'Enter',bubbles:true}));
assert.match(get('reader-focus-status').textContent,/已锁定/);
assert.deepEqual(JSON.parse(w.localStorage.getItem(key)),[original]);
""", before_boot="""
w.localStorage.setItem('ensemble-reader:appearance-v1',JSON.stringify({focus:true,palette:'butter'}));
""")


def test_focus_malformed_preferences_fall_back_and_blocked_storage_still_works():
    run_dom(r"""
assert.equal(get('reader-focus-toggle').getAttribute('aria-pressed'),'false');
assert.equal(d.documentElement.dataset.readerPalette,'white');
Object.defineProperty(w.Storage.prototype,'setItem',{configurable:true,value(){throw new Error('blocked');}});
get('reader-focus-toggle').click();
get('reader-palette').value='butter';get('reader-palette').dispatchEvent(new w.Event('change'));
assert.equal(get('reader-focus-toggle').getAttribute('aria-pressed'),'true');
assert.equal(d.documentElement.dataset.readerPalette,'butter');
""", before_boot="""
w.localStorage.setItem('ensemble-reader:appearance-v1','not-json');
""")


def test_focus_does_not_intercept_drag_selection_or_touch_hover():
    run_dom(r"""
get('reader-focus-toggle').click();
const mark=d.querySelector('mark.reader-highlight');
const touch=new w.Event('pointerover',{bubbles:true});
Object.defineProperty(touch,'pointerType',{value:'touch'});mark.dispatchEvent(touch);
assert.equal(d.querySelector('.reader-focus-target'),null);
const range=d.createRange();range.selectNodeContents(mark);w.getSelection().addRange(range);
mark.dispatchEvent(new w.MouseEvent('pointerover',{bubbles:true}));mark.click();
assert.doesNotMatch(get('reader-focus-status').textContent,/已锁定/);
assert.equal(w.getSelection().toString(),original.quote);
w.getSelection().removeAllRanges();
mark.click();assert.match(get('reader-focus-status').textContent,/已锁定/);
""")


def test_focus_export_keeps_data_and_preferences_not_transient_target():
    run_dom(r"""
let exported;w.URL.createObjectURL=blob=>{exported=blob;return 'blob:test';};
w.URL.revokeObjectURL=()=>{};w.HTMLAnchorElement.prototype.click=function(){};
w.Blob=globalThis.Blob;
get('reader-focus-toggle').click();d.querySelector('mark.reader-highlight').click();
get('reader-palette').value='butter';get('reader-palette').dispatchEvent(new w.Event('change'));
get('annotation-save-html').click();
const html=await exported.text(), copy=new JSDOM(html);
assert.equal(copy.window.document.documentElement.dataset.readerPalette,'butter');
assert.equal(copy.window.document.body.dataset.readerFocus,'on');
assert.equal(copy.window.document.body.classList.contains('reader-focus-active'),false);
assert.equal(copy.window.document.querySelector('.reader-focus-target'),null);
const records=JSON.parse(copy.window.document.getElementById('embedded-annotations').textContent);
assert.deepEqual(records,[original]);
""")


def test_paired_and_single_panels_body_close_and_reference_exclusivity():
    run_dom(r"""
askFromNote(); await flush();
assert.equal(pane.hidden,false); assert.equal(qa.hidden,false);
assert.equal(d.body.classList.contains('reader-paired'),true);
get('qa-close').click(); await flush(); assert.equal(pane.hidden,true);
assert.equal(get('annotation-floats').hidden,false);
assert.equal(get('annotation-quote').hidden,true);
assert.equal(d.body.classList.contains('reader-paired'),false);
get('qa-open-general').click(); await flush();
assert.equal(pane.hidden,true); assert.equal(qa.hidden,false);
const entry = {id:'standalone',time:'today',turns:[{question:'问题',answer:'回答',focus:''}]};
send('ENSEMBLE_QA_SAVE',{entry});
assert.equal(JSON.parse(w.localStorage.getItem(key+':qa-history-v1'))[0].id,'standalone');
askFromNote(); await flush();
send('ENSEMBLE_QA_NEW_CONTEXT'); await flush();
assert.equal(pane.hidden,true); assert.equal(qa.hidden,false);
askFromNote(); await flush();
d.querySelector('main article p').click(); await flush();
assert.equal(pane.hidden,true); assert.equal(qa.hidden,true);
askFromNote();
d.dispatchEvent(new w.Event('ensemble-reader-card-open')); await flush();
assert.equal(pane.hidden,true); assert.equal(qa.hidden,true);
""")


def test_navigation_search_ticks_and_annotations_survive_fallback_highlighting():
    run_dom(r"""
get('nav-annotations').click(); assert.equal(get('toc-panel').hidden,true);
assert.equal(get('annotation-tools').hidden,false);
get('nav-toc').click(); assert.equal(get('annotation-tools').hidden,true);
get('reader-search').value = 'Alpha'; get('reader-search').dispatchEvent(new w.Event('input'));
await new Promise(resolve => setTimeout(resolve,220));
assert.equal(get('reader-search-status').textContent,'1/2');
assert.equal(d.querySelectorAll('mark.reader-search-hit').length,2);
assert.equal(get('reader-position-ticks').querySelectorAll('.search-match,.search-current').length,2);
assert.equal(get('reader-position-ticks').querySelectorAll('.annotation-location').length,1);
get('search-next').click(); assert.equal(get('reader-search-status').textContent,'2/2');
d.querySelector('.annotation-float .annotation-preview').dispatchEvent(new w.MouseEvent('dblclick',{bubbles:true}));
get('annotation-note').value='搜索中保存'; get('annotation-save').click(); await flush();
assert.equal(get('reader-search-status').textContent,'1/2');
assert.equal(d.querySelectorAll('mark.reader-highlight').length,1);
get('search-clear').click(); assert.equal(d.querySelectorAll('mark.reader-search-hit').length,0);
assert.equal(d.querySelector('main article p').textContent,'Alpha beta and Alpha gamma.');
assert.equal(d.querySelector('mark.reader-highlight').textContent,'Alpha beta');
""")


def test_context_menu_override_is_scoped_and_shortcuts_work():
    run_dom(r"""
const paragraph = d.querySelector('main article p');
function context(node,shiftKey=false) { const event = new w.MouseEvent('contextmenu',{bubbles:true,cancelable:true,shiftKey}); node.dispatchEvent(event); return event.defaultPrevented; }
assert.equal(context(paragraph),false);
assert.equal(context(d.querySelector('mark.reader-highlight')),true);
assert.equal(context(d.querySelector('mark.reader-highlight'),true),false);
const node = paragraph.lastChild, range = d.createRange();
range.setStart(node,5); range.setEnd(node,10); w.getSelection().removeAllRanges(); w.getSelection().addRange(range);
assert.equal(context(paragraph),true); assert.equal(get('selection-menu').hidden,false);
paragraph.dispatchEvent(new w.KeyboardEvent('keydown',{key:'N',altKey:true,shiftKey:true,bubbles:true,cancelable:true}));
assert.equal(pane.dataset.mode,'edit');
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,2);
""")


def test_search_uses_native_highlight_api_without_changing_article_dom():
    run_dom(r"""
w.Highlight = class { constructor(...ranges) { this.ranges=ranges; } };
w.CSS.highlights = new Map();
get('reader-search').value='beta'; get('reader-search').dispatchEvent(new w.Event('input'));
await new Promise(resolve=>setTimeout(resolve,220));
assert.equal(w.CSS.highlights.get('reader-search').ranges.length,2);
assert.equal(w.CSS.highlights.get('reader-search-current').ranges.length,1);
assert.equal(d.querySelectorAll('mark.reader-search-hit').length,0);
assert.equal(get('reader-position-ticks').querySelectorAll('.search-match,.search-current').length,2);
get('search-clear').click(); assert.equal(w.CSS.highlights.has('reader-search'),false);
assert.equal(d.querySelector('mark.reader-highlight').textContent,original.quote);
const shortcut = new w.KeyboardEvent('keydown',{key:'f',ctrlKey:true,bubbles:true,cancelable:true});
d.body.dispatchEvent(shortcut);
assert.equal(shortcut.defaultPrevented,true); assert.equal(d.activeElement,get('reader-search'));
""")


def test_qa_and_annotation_markdown_tables_remain_text_only():
    run_dom(r"""
const target = d.createElement('div');
w.EnsembleReaderMarkdown.render(target,'|A|B|\n|:---|---:|\n|$|x|$|`a|b`|\n|c\\|d|<img src=x onerror=alert(1)>|');
assert.equal(target.querySelectorAll('table').length,1);
assert.equal(target.querySelectorAll('tbody td').length,4);
assert.equal(target.querySelector('code').textContent,'a|b');
assert.equal(target.querySelector('.math-inline').textContent,'\\(|x|\\)');
assert.equal(target.querySelector('img'),null);
assert.equal(target.querySelectorAll('tbody td')[2].textContent,'c|d');
w.EnsembleReaderMarkdown.render(target,'$\\href{javascript:alert(1)}{x}$');
assert.equal(target.querySelector('a'),null);
""")


def test_storage_identity_does_not_depend_on_ui_rendering_profile():
    # This is the same key formula used before the UI redesign. Meeting/body,
    # not stylesheet or rendering version, determine access to legacy records.
    source = "# Report\n\nAlpha beta and Alpha gamma.\n\nDelta beta.\n"
    expected = "ensemble-reader:" + hashlib.sha256(("LR-OLD\n" + source).encode()).hexdigest()
    assert f'data-annotation-key="{expected}"' in render_academic_review_html(source, meeting_id="LR-OLD")


def test_import_json_and_old_annotated_html_merge_without_overwriting_or_execution():
    run_dom(r"""
const field = get('annotation-import-file');
async function importContent(content) {
  Object.defineProperty(field,'files',{configurable:true,value:[{size:content.length,text:async()=>content}]});
  field.dispatchEvent(new w.Event('change')); await flush();
}
const incoming = {...original,id:'backup-two',block:2,start:0,quote:'Delta beta.',note:'第二条'};
const data = {format:'ENSEMBLE_READER_ANNOTATIONS_V1',report_key:key,records:[{...original,note:'旧备份，不应覆盖'},incoming],qa_history:[]};
await importContent(JSON.stringify(data));
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,2);
assert.equal(JSON.parse(w.localStorage.getItem(key)).find(record=>record.id===original.id).note,original.note);
await importContent(JSON.stringify({...data,report_key:'ensemble-reader:other',report_meeting:'LR-OTHER'}));
assert.match(get('annotation-status').textContent,/不是同一会议/);
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,2);
await importContent('null'); assert.match(get('annotation-status').textContent,/格式无效/);
const htmlBackup = '<html data-annotation-key="'+key+':shared:old"><body><script>window.unwantedImport=1;</script><script type="application/json" id="embedded-annotations">'+JSON.stringify([{...incoming,id:'from-html'}])+'</script></body></html>';
await importContent(htmlBackup);
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,3);
assert.equal(w.unwantedImport,undefined);
""")


def test_exported_html_contains_data_not_transient_panels_or_key():
    run_dom(r"""
let exported;
w.Blob = Blob;
w.URL.createObjectURL = blob => { exported=blob; return 'blob:test'; };
w.URL.revokeObjectURL = () => {};
w.HTMLAnchorElement.prototype.click = function() {};
askFromNote(); await flush();
send('ENSEMBLE_QA_NEW_CONTEXT');
send('ENSEMBLE_QA_SAVE',{entry:{id:'standalone',time:'today',turns:[{question:'问',answer:'答',focus:''}]}});
get('reader-search').value='Alpha'; get('reader-search').dispatchEvent(new w.Event('input')); await new Promise(resolve=>setTimeout(resolve,200));
get('annotation-save-html').click(); const output=await exported.text();
const copy = new JSDOM(output);
assert.equal(copy.window.document.querySelector('#annotation-panel').hidden,true);
assert.equal(copy.window.document.querySelector('#qa-panel').hidden,true);
assert.equal(copy.window.document.querySelector('#reader-entry-panel').hidden,true);
assert.equal(copy.window.document.querySelector('#reader-entry-body').childElementCount,0);
assert.equal(copy.window.document.querySelector('#annotation-floats').childElementCount,0);
assert.equal(copy.window.document.querySelector('#reader-glass'),null);
assert.equal(copy.window.document.querySelector('body').classList.contains('reader-paired'),false);
assert.equal(copy.window.document.querySelectorAll('mark.reader-search-hit').length,0);
assert.equal(copy.window.document.querySelector('#qa-frame').hasAttribute('srcdoc'),false);
assert.equal(JSON.parse(copy.window.document.querySelector('#embedded-annotations').textContent)[0].note,original.note);
assert.equal(JSON.parse(copy.window.document.querySelector('#embedded-qa-history').textContent)[0].id,'standalone');
copy.window.close();
""")


def test_current_toc_tracks_sections_scroll_and_history_without_moving_body():
    run_dom(r"""
const links=[...get('toc-panel').querySelectorAll('a')];
const headings=[...d.querySelectorAll('article h1,article h2,article h3')];
assert.equal(links.length,4);
Object.defineProperty(w,'innerHeight',{configurable:true,value:800});
Object.defineProperty(w,'scrollY',{configurable:true,writable:true,value:0});
Object.defineProperty(d.documentElement,'scrollHeight',{configurable:true,value:3000});
const tops=[80,220,800,2600];
headings.forEach((h,i)=>h.getBoundingClientRect=()=>({top:tops[i]-w.scrollY,height:30}));
w.dispatchEvent(new w.Event('resize')); await flush();
assert.equal(links[0].getAttribute('aria-current'),'location');
assert.equal(d.querySelectorAll('#toc-panel a[aria-current]').length,1);
w.scrollY=100;w.dispatchEvent(new w.Event('scroll'));await flush();
assert.equal(links[1].getAttribute('aria-current'),'location');
assert.equal(links[1].parentElement.classList.contains('reader-toc-current'),true);
assert.equal(links[0].hasAttribute('aria-current'),false);
w.scrollY=700;w.dispatchEvent(new w.Event('hashchange'));await flush();
assert.equal(links[2].getAttribute('aria-current'),'location');
let revealed=0;get('toc-panel').getBoundingClientRect=()=>({top:100,bottom:300,height:200});
links[3].getBoundingClientRect=()=>({top:350,bottom:380,height:30});
get('toc-panel').scrollTo=options=>{revealed++;get('toc-panel').scrollTop=options.top;};
get('nav-annotations').click();
w.scrollY=2200;w.dispatchEvent(new w.Event('scroll'));await flush();
assert.equal(links[3].getAttribute('aria-current'),'location');
assert.equal(revealed,0);
get('nav-toc').click();await flush();assert.equal(revealed,1);
assert.equal(w.scrollY,2200);
assert.equal(d.querySelectorAll('#toc-panel a[aria-current]').length,1);
assert.equal(w.getComputedStyle(links[3]).fontWeight,'700');
""", markdown="# Report\n\nAlpha beta.\n\n## One\n\nText.\n\n### Detail\n\nText.\n\n## Two\n\nText.\n")


def test_star_and_toc_transient_views_reset_on_export_without_changing_data():
    run_dom(r"""
const card=d.querySelector('.annotation-float');card.click();card.querySelector('.annotation-star-toggle').click();await flush();
assert.equal(get('reader-star-rail').childElementCount,1);
assert.equal(get('toc-panel').querySelectorAll('[aria-current]').length,1);
let exported;w.Blob=globalThis.Blob;w.URL.createObjectURL=blob=>{exported=blob;return 'blob:test';};
w.URL.revokeObjectURL=()=>{};w.HTMLAnchorElement.prototype.click=function(){};
get('annotation-save-html').click();const html=await exported.text(), copy=new JSDOM(html);
assert.equal(copy.window.document.getElementById('reader-star-rail').childElementCount,0);
assert.equal(copy.window.document.querySelector('#toc-panel [aria-current]'),null);
assert.equal(copy.window.document.querySelector('.reader-toc-current'),null);
assert.equal(JSON.parse(copy.window.document.getElementById('embedded-annotations').textContent)[0].starred,true);
assert.equal(JSON.parse(w.localStorage.getItem(key))[0].starred,true);
""")


def test_import_changed_ui_hash_checks_body_not_hash_alone():
    run_dom(r"""
const copy = d.documentElement.cloneNode(true);
copy.dataset.annotationKey='ensemble-reader:older-ui:shared:saved';
copy.removeAttribute('data-meeting-id'); // Legacy HTML identifies its meeting in the footer.
copy.querySelector('#embedded-annotations').textContent=JSON.stringify([{...original,id:'old-ui-note'}]);
copy.querySelector('style').textContent='body {color:red;}';
w.confirm = () => { throw new Error('An unchanged report must not ask to skip safeguards'); };
const content=copy.outerHTML, field=get('annotation-import-file');
Object.defineProperty(field,'files',{configurable:true,value:[{name:'old.annotated.html',size:content.length,text:async()=>content}]});
field.dispatchEvent(new w.Event('change')); await flush();
assert.match(get('annotation-import-status').textContent,/已导入 1 条/);
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,2);
assert.equal(JSON.parse(w.localStorage.getItem(key)).find(item=>item.id==='old-ui-note').block,1);
""")


def test_version_migration_requires_unchanged_context_and_explicit_confirmation():
    run_dom(r"""
const copy = d.documentElement.cloneNode(true);
copy.dataset.annotationKey='ensemble-reader:old-version';
const added=d.createElement('p'); added.textContent='Only in the older report.';
const firstParagraph=copy.querySelector('main article p');
firstParagraph.parentNode.insertBefore(added,firstParagraph);
copy.querySelector('#embedded-annotations').textContent=JSON.stringify([{...original,id:'migrated',block:2}]);
const field=get('annotation-import-file');
async function attempt(content) {
  Object.defineProperty(field,'files',{configurable:true,value:[{name:'old.html',size:content.length,text:async()=>content}]});
  field.dispatchEvent(new w.Event('change')); await flush();
}
let confirmations=0;
w.confirm = () => {confirmations++; return false;};
await attempt(copy.outerHTML);
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,1);
assert.match(get('annotation-import-status').textContent,/已取消版本迁移/);
w.confirm = () => {confirmations++; return true;};
await attempt(copy.outerHTML);
assert.equal(confirmations,2);
assert.equal(JSON.parse(w.localStorage.getItem(key)).find(item=>item.id==='migrated').block,1);
copy.querySelectorAll('main article p')[1].textContent='Changed Alpha beta paragraph.';
copy.querySelector('#embedded-annotations').textContent=JSON.stringify([{...original,id:'unsafe',block:2}]);
await attempt(copy.outerHTML);
assert.match(get('annotation-import-status').textContent,/原段落已改变/);
assert.equal(JSON.parse(w.localStorage.getItem(key)).some(item=>item.id==='unsafe'),false);
""")


def test_import_empty_original_html_explains_browser_storage_and_reports_progress():
    run_dom(r"""
const content=d.documentElement.outerHTML, field=get('annotation-import-file');
let finish;
Object.defineProperty(field,'files',{configurable:true,value:[{name:'original.html',size:content.length,text:()=>new Promise(resolve=>{finish=resolve;})}]});
field.dispatchEvent(new w.Event('change'));
assert.match(get('annotation-import-status').textContent,/正在读取：original.html/);
finish(content); await flush();
assert.match(get('annotation-import-status').textContent,/文件内没有批注数据/);
assert.match(get('annotation-import-status').textContent,/保存带批注和问答的 HTML/);
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,1);
""")


def test_saved_annotation_card_tracks_document_anchor_and_pair_stays_docked():
    run_dom(r"""
const mark=d.querySelector('mark.reader-highlight'); let anchorY=320;
const card=d.querySelector('.annotation-float');
Object.defineProperty(w,'scrollY',{configurable:true,writable:true,value:800});
mark.getBoundingClientRect=()=>({top:anchorY-w.scrollY});
const article = d.querySelector('main article');
article.getBoundingClientRect=()=>({right:600});
card.getBoundingClientRect=()=>({width:300});
Object.defineProperty(w,'innerWidth',{configurable:true,value:1200});
await flush();
assert.equal(card.style.top,'320px'); assert.equal(card.style.left,'612px');
assert.equal(w.getComputedStyle(card).position,'absolute');
assert.equal(d.body.dataset.readerPanels,'0');
w.scrollY=900; w.dispatchEvent(new w.Event('scroll')); await flush();
assert.equal(card.style.top,'320px');
anchorY=430; w.dispatchEvent(new w.Event('resize')); await flush();
assert.equal(card.style.top,'430px');
askFromNote(); await flush();
assert.equal(w.getComputedStyle(pane).position,'fixed');
assert.equal(d.body.classList.contains('reader-paired'),true);
""")


def test_plain_highlights_do_not_open_cards_or_appear_in_the_menu():
    run_dom(r"""
assert.equal(get('annotation-floats').childElementCount,1);
assert.equal(pane.hidden,true);
d.querySelector('.annotation-float .annotation-preview').dispatchEvent(new w.MouseEvent('dblclick',{bubbles:true}));
get('annotation-note').value=''; get('annotation-note').dispatchEvent(new w.Event('input'));
assert.equal(get('annotation-empty-message').hidden,false);
get('annotation-save').click(); await flush();
assert.equal(get('annotation-floats').childElementCount,0);
assert.equal(get('annotation-list').querySelectorAll('.annotation-list-row').length,0);
assert.equal(pane.hidden,true); assert.equal(d.body.dataset.readerPanels,'0');
const mark=d.querySelector('mark.reader-highlight');
mark.click(); await flush(); assert.equal(pane.hidden,true);
assert.equal(get('annotation-floats').childElementCount,0);
mark.dispatchEvent(new w.MouseEvent('dblclick',{bubbles:true}));
assert.equal(pane.hidden,false); assert.equal(pane.dataset.mode,'edit');
assert.equal(get('annotation-empty-message').hidden,false);
get('annotation-ask-ai').click(); await flush();
assert.equal(qa.hidden,false); assert.equal(d.body.classList.contains('reader-paired'),true);
get('annotation-cancel').click(); await flush();
assert.equal(get('annotation-empty-message').hidden,true);
assert.equal(get('annotation-preview').hidden,true);
assert.equal(get('annotation-quote').hidden,false);
get('qa-close').click(); await flush(); assert.equal(pane.hidden,true);
mark.dispatchEvent(new w.MouseEvent('dblclick',{bubbles:true}));
get('annotation-note').value='真正的批注'; get('annotation-note').dispatchEvent(new w.Event('input'));
assert.equal(get('annotation-empty-message').hidden,true);
get('annotation-save').click();
assert.equal(get('annotation-empty-message').hidden,true);
await flush();
assert.equal(d.querySelector('.annotation-float .annotation-preview').textContent,'真正的批注');
assert.equal(get('annotation-floats').textContent.includes('尚无批注'),false);
assert.equal(get('annotation-quote').hidden,true);
assert.equal(d.querySelector('.annotation-locate').textContent.includes(original.quote),false);
""")


def test_all_notes_expand_and_midline_anchor_controls_overlap_stacking():
    run_dom(r"""
const second={id:'second',block:2,start:0,quote:'Delta beta',note:'第二条 $y$'};
const data=JSON.stringify({format:'ENSEMBLE_READER_ANNOTATIONS_V1',report_key:key,records:[second],qa_history:[]});
Object.defineProperty(get('annotation-import-file'),'files',{configurable:true,value:[{size:data.length,text:async()=>data}]});
get('annotation-import-file').dispatchEvent(new w.Event('change')); await flush();
const cards=[...d.querySelectorAll('.annotation-float')];
assert.equal(cards.length,2); assert.equal(pane.hidden,true);
assert.equal(d.body.dataset.readerPanels,'0');
let firstY=300,secondY=340;
Object.defineProperty(w,'innerHeight',{configurable:true,value:700});
Object.defineProperty(w,'innerWidth',{configurable:true,value:700});
Object.defineProperty(w,'scrollY',{configurable:true,writable:true,value:0});
d.querySelector('main article').getBoundingClientRect=()=>({right:620});
for (const card of cards) card.getBoundingClientRect=()=>({width:300});
d.querySelector('mark[data-annotation-id="legacy-one"]').getBoundingClientRect=()=>({top:firstY-w.scrollY,height:20});
d.querySelector('mark[data-annotation-id="second"]').getBoundingClientRect=()=>({top:secondY-w.scrollY,height:20});
w.dispatchEvent(new w.Event('resize')); await flush();
assert.equal(cards[0].style.left,'292px'); assert.equal(cards[1].style.left,'292px');
assert.equal(cards[1].classList.contains('annotation-foremost'),true);
assert.ok(Number(cards[1].style.zIndex)>Number(cards[0].style.zIndex));
firstY=350;secondY=390; w.dispatchEvent(new w.Event('scroll')); await flush();
assert.equal(cards[0].classList.contains('annotation-foremost'),true);
assert.ok(Number(cards[0].style.zIndex)>Number(cards[1].style.zIndex));
assert.equal(cards[0].style.top,'350px');
w.scrollY=20; w.dispatchEvent(new w.Event('scroll')); await flush();
assert.equal(cards[0].style.top,'350px');
assert.equal(get('annotation-floats').querySelector('blockquote'),null);
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,2);
""")


def test_frosted_preview_card_right_actions_appear_only_when_selected():
    run_dom(r"""
const card=d.querySelector('.annotation-float');
const actions=card.querySelector('.annotation-float-actions');
assert.deepEqual([...actions.querySelectorAll('button')].map(button=>button.textContent),['编辑','问 AI','删除','☆']);
assert.equal(actions.hidden,true);
card.click(); await flush();
assert.equal(actions.hidden,false);
assert.equal(card.classList.contains('annotation-selected'),true);
assert.equal(w.getComputedStyle(actions).position,'absolute');
assert.equal(w.getComputedStyle(actions).flexDirection,'column');
assert.equal(w.getComputedStyle(actions.querySelector('button')).fontSize,'0.78rem');
assert.equal(w.getComputedStyle(card).overflow,'');
const css=d.querySelector('style').textContent;
assert.equal(css.includes('data:image/svg+xml,'),true);
assert.equal(css.includes('backdrop-filter:blur(18px)'),true);
assert.equal(css.includes('inset:12px auto auto calc(100% + 6px)'),true);
assert.equal(css.includes('-webkit-line-clamp:5'),true);
d.body.dispatchEvent(new w.MouseEvent('pointerdown',{bubbles:true}));
assert.equal(actions.hidden,true);
card.click();
actions.querySelectorAll('button')[0].click(); await flush();
assert.equal(pane.dataset.mode,'edit'); assert.equal(pane.hidden,false);
get('annotation-cancel').click(); await flush();
actions.querySelectorAll('button')[1].click(); await flush();
assert.equal(qa.hidden,false); assert.equal(d.body.classList.contains('reader-paired'),true);
get('qa-close').click(); await flush();
actions.querySelectorAll('button')[2].click(); await flush();
assert.equal(d.querySelector('.annotation-float'),null);
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,0);
""")


def test_one_line_directory_opens_full_content_on_right_without_truncating_storage():
    run_dom(r"""
get('nav-annotations').click();
assert.equal(d.body.dataset.readerNavigation,'annotations');
d.querySelector('.annotation-float-actions button').click();
const longNote='**完整批注**\n\n'+('长段落不应被截为八十个字符。'.repeat(12))+'\n\n|项|值|\n|---|---|\n|A|2|';
get('annotation-note').value=longNote;get('annotation-save').click();await flush();
const noteRow=get('annotation-list').querySelector('.annotation-list-row');
assert.deepEqual([...noteRow.querySelectorAll('button')].map(button=>button.textContent),['删除']);
assert.equal(noteRow.querySelector('strong'),null);
assert.equal(noteRow.querySelector('table'),null);
assert.ok(noteRow.querySelector('.annotation-locate').textContent.length<=141);
assert.equal(w.getComputedStyle(noteRow.querySelector('.annotation-locate')).whiteSpace,'nowrap');
assert.equal(w.getComputedStyle(noteRow).borderBottomWidth,'1px');
assert.equal(get('annotation-list').querySelector('.annotation-ask-ai'),null);
assert.equal(get('annotation-list').querySelector('.annotation-add-note'),null);
noteRow.querySelector('.annotation-locate').click();await flush();
assert.equal(get('reader-entry-panel').hidden,false);
assert.equal(get('reader-entry-body').querySelector('strong').textContent,'完整批注');
assert.equal(get('reader-entry-body').querySelectorAll('table tbody tr').length,1);
assert.ok(get('reader-entry-body').textContent.includes('长段落不应被截为八十个字符。'.repeat(12)));
assert.equal(pane.hidden,true);assert.equal(qa.hidden,true);
get('reader-entry-close').click();await flush();
askFromNote(); await flush();
send('ENSEMBLE_QA_SAVE',{entry:{id:'linked-thread',time:'today',turns:[{question:'真实的问题',answer:'**完整回答**\n\n回答内容不是原文引用。',focus:original.quote}]}});await flush();
const rows=[...get('annotation-list').querySelectorAll('.annotation-list-row')];
assert.equal(rows.length,2);
assert.ok(rows[1].textContent.includes('真实的问题'));
assert.equal(rows[1].querySelector('strong'),null);
assert.equal(rows[1].textContent.includes('完整回答'),false);
assert.deepEqual([...rows[1].querySelectorAll('button')].map(button=>button.textContent),['删除']);
rows[1].querySelector('.annotation-locate').click(); await flush();
assert.equal(qa.hidden,true);
assert.equal(get('reader-entry-panel').hidden,false);
assert.equal(get('reader-entry-body').querySelector('strong').textContent,'完整回答');
get('reader-entry-continue').click();await flush();
assert.equal(qa.hidden,false);
assert.equal(get('reader-entry-panel').hidden,false);
assert.equal(d.body.dataset.readerPanels,'2');
rows[1].querySelector('button').click(); await flush();
const saved=JSON.parse(w.localStorage.getItem(key));
assert.equal(saved[0].note,longNote);
assert.equal(saved[0].qaThreads?.length || 0,0);
assert.equal(get('annotation-list').querySelectorAll('.annotation-list-row').length,1);
get('nav-toc').click();assert.equal(d.body.dataset.readerNavigation,'toc');
""")


def test_single_pane_overlays_body_with_slide_in_animation():
    run_dom(r"""
get('qa-open-general').click(); await flush();
assert.equal(d.body.dataset.readerPanels,'1');
assert.equal(w.getComputedStyle(d.body).paddingRight,'');
const css=d.querySelector('style').textContent;
assert.equal(css.includes('body[data-reader-panels="1"] { padding-right:'),false);
assert.equal(css.includes('body[data-reader-panels="1"],body[data-reader-panels="2"]'),false);
assert.equal(css.includes('body[data-reader-panels="2"] { padding-right:'),false);
assert.equal(css.includes('@keyframes reader-pane-enter'),true);
assert.equal(css.includes('animation:reader-pane-enter 240ms ease-out both'),true);
assert.equal(css.includes('.annotation-panel,.reader-entry-panel,.qa-panel,.drawer,nav { animation:none !important;'),true);
assert.equal(css.includes('grid-template-columns:340px'),false);
""")


def test_both_directories_share_left_aligned_space_without_tab_specific_expansion():
    run_dom(r"""
const css=d.querySelector('style').textContent;
assert.equal(css.includes('--reader-nav-column:250px'),true);
assert.equal(css.includes('--reader-shell-max:1320px'),true);
assert.equal(css.includes('grid-template-columns:var(--reader-nav-column) minmax(0,1fr)'),true);
assert.equal(css.includes('nav { justify-self:end; width:min(360px,calc('),true);
assert.equal(css.includes('max(0px,(100vw - var(--reader-shell-max)) / 2)'),true);
assert.equal(css.includes('body[data-reader-navigation="annotations"] nav'),false);
assert.equal(css.includes('body[data-reader-navigation="toc"] nav'),false);
const snapshot=w.localStorage.getItem(key);
const nav=d.querySelector('nav');
get('nav-annotations').click();
assert.equal(get('annotation-tools').closest('nav'),nav);
assert.equal(get('annotation-tools').hidden,false);assert.equal(get('toc-panel').hidden,true);
get('nav-toc').click();
assert.equal(get('toc-panel').closest('nav'),nav);
assert.equal(get('toc-panel').hidden,false);assert.equal(get('annotation-tools').hidden,true);
assert.equal(w.localStorage.getItem(key),snapshot);
""")


def test_annotation_star_updates_directory_and_gray_rail_without_disturbing_search():
    run_dom(r"""
await flush();
const card=d.querySelector('.annotation-float');
const star=card.querySelector('.annotation-star-toggle');
assert.equal(star.getAttribute('aria-pressed'),'false');
assert.equal(get('reader-star-rail').querySelector('.annotation-important'),null);
get('reader-search').value='Alpha';get('reader-search').dispatchEvent(new w.Event('input'));
await new Promise(resolve=>setTimeout(resolve,220));get('search-next').click();
const source=d.querySelector('mark.reader-highlight');
card.click();star.click();await flush();
assert.equal(star.textContent,'★');assert.equal(star.getAttribute('aria-pressed'),'true');
assert.equal(JSON.parse(w.localStorage.getItem(key))[0].starred,true);
assert.equal(JSON.parse(w.localStorage.getItem(key))[0].note,original.note);
assert.equal(d.querySelector('mark.reader-highlight'),source);
assert.equal(get('reader-search-status').textContent,'2/2');
const row=get('annotation-list').querySelector('.annotation-list-row');
assert.equal(row.classList.contains('annotation-starred'),true);
assert.equal(row.querySelector('.annotation-star-icon').textContent,'★');
assert.equal(w.getComputedStyle(row.querySelector('.annotation-locate')).fontWeight,'700');
assert.deepEqual([...row.querySelectorAll('button')].map(button=>button.textContent),['删除']);
const tick=get('reader-star-rail').querySelector('.annotation-important');
assert.equal(get('reader-position-ticks').querySelector('.annotation-important'),null);
assert.equal(tick.parentElement,get('reader-star-rail'));
assert.equal(w.getComputedStyle(get('reader-star-rail')).right,'24px');
assert.equal(w.getComputedStyle(tick).width,'24px');
assert.equal(tick.title.startsWith('重要批注：'),true);
assert.equal(get('reader-position-ticks').querySelectorAll('.annotation-location').length,1);
let jumped=false;source.scrollIntoView=function(options){jumped=options.block==='center';};
tick.click();assert.equal(jumped,true);
assert.equal(pane.hidden,true);assert.equal(qa.hidden,true);
row.querySelector('.annotation-locate').click();await flush();
assert.equal(get('reader-entry-star').hidden,false);
assert.equal(get('reader-entry-star').getAttribute('aria-pressed'),'true');
get('reader-entry-star').click();await flush();
assert.equal(get('reader-entry-panel').hidden,false);
assert.equal(get('reader-entry-star').textContent,'☆');
assert.equal(get('annotation-list').querySelector('.annotation-starred'),null);
assert.equal(get('reader-star-rail').querySelector('.annotation-important'),null);
assert.equal(get('reader-position-ticks').querySelectorAll('.annotation-location').length,1);
assert.equal(JSON.parse(w.localStorage.getItem(key))[0].starred,undefined);
assert.equal(get('reader-search-status').textContent,'2/2');
const css=d.querySelector('style').textContent;
assert.ok(css.includes('background:#5f6267; clip-path:polygon(50% 0%'));
""")


def test_star_preserves_unsaved_note_text_and_disappears_for_plain_highlights():
    run_dom(r"""
d.querySelector('.annotation-float .annotation-preview').dispatchEvent(new w.MouseEvent('dblclick',{bubbles:true}));
get('annotation-note').value='未保存的新内容 $y$';get('annotation-note').dispatchEvent(new w.Event('input'));
get('annotation-star').click();await flush();
assert.equal(get('annotation-note').value,'未保存的新内容 $y$');
assert.equal(pane.dataset.mode,'edit');assert.equal(get('annotation-star').getAttribute('aria-pressed'),'true');
assert.equal(JSON.parse(w.localStorage.getItem(key))[0].note,original.note);
get('annotation-cancel').click();await flush();
assert.equal(JSON.parse(w.localStorage.getItem(key))[0].starred,true);
d.querySelector('.annotation-float .annotation-preview').dispatchEvent(new w.MouseEvent('dblclick',{bubbles:true}));
get('annotation-note').value='';get('annotation-save').click();await flush();
assert.equal(get('annotation-list').querySelectorAll('.annotation-list-row').length,0);
assert.equal(get('reader-star-rail').querySelector('.annotation-important'),null);
const source=d.querySelector('mark.reader-highlight');
source.dispatchEvent(new w.MouseEvent('dblclick',{bubbles:true}));
get('annotation-note').value='恢复重要批注';get('annotation-save').click();await flush();
assert.equal(get('reader-star-rail').querySelectorAll('.annotation-important').length,1);
const card=d.querySelector('.annotation-float');card.click();
card.querySelector('.annotation-float-actions button:nth-child(3)').click();await flush();
assert.equal(get('reader-star-rail').querySelector('.annotation-important'),null);
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,0);
""")


def test_star_round_trips_json_html_and_legacy_import_without_overwriting_local_state():
    run_dom(r"""
let exported;w.Blob=Blob;
w.URL.createObjectURL=blob=>{exported=blob;return 'blob:test';};w.URL.revokeObjectURL=()=>{};
w.HTMLAnchorElement.prototype.click=function(){};
const card=d.querySelector('.annotation-float');card.click();card.querySelector('.annotation-star-toggle').click();
get('annotation-export-data').click();const data=JSON.parse(await exported.text());
assert.equal(data.format,'ENSEMBLE_READER_ANNOTATIONS_V1');assert.equal(data.records[0].starred,true);
assert.equal(data.report_key,key);
get('annotation-save-html').click();const backup=await exported.text();
const copy=new JSDOM(backup);const embedded=JSON.parse(copy.window.document.querySelector('#embedded-annotations').textContent);
assert.equal(embedded[0].starred,true);assert.equal(embedded[0].note,original.note);copy.window.close();
async function importData(value){
 const content=typeof value==='string'?value:JSON.stringify(value);
 Object.defineProperty(get('annotation-import-file'),'files',{configurable:true,value:[{size:content.length,text:async()=>content}]});
 get('annotation-import-file').dispatchEvent(new w.Event('change'));await flush();
 await new Promise(resolve=>w.requestAnimationFrame(resolve));
}
card.querySelector('.annotation-float-actions button:nth-child(3)').click();await flush();
await importData(backup);
assert.equal(JSON.parse(w.localStorage.getItem(key))[0].starred,true);
assert.equal(get('reader-star-rail').querySelectorAll('.annotation-important').length,1);
const restored=d.querySelector('.annotation-float');restored.click();restored.querySelector('.annotation-star-toggle').click();
await importData(data); // Existing local unstarred record wins over a starred backup.
assert.equal(JSON.parse(w.localStorage.getItem(key))[0].starred,undefined);
const legacy={...original,id:'legacy-two',block:2,start:0,quote:'Delta beta.'};
await importData({...data,records:[legacy]});
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,2);
assert.equal(JSON.parse(w.localStorage.getItem(key)).find(record=>record.id==='legacy-two').starred,undefined);
await importData({...data,records:[{...legacy,id:'bad-star',starred:'true'}]});
assert.match(get('annotation-import-status').textContent,/格式无效/);
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,2);
await importData({...data,records:[{...legacy,id:'imported-star',starred:true}]});
assert.equal(JSON.parse(w.localStorage.getItem(key)).find(record=>record.id==='imported-star').starred,true);
assert.equal(get('annotation-list').querySelectorAll('.annotation-starred').length,1);
""")


def test_qa_preview_expands_full_math_and_tables_in_second_pane_with_source_guard():
    run_dom(r"""
get('qa-open-general').click();await flush();
const question='解释表格与公式';
const answer='**完整答案**\n\n'+('一段不能被丢弃的回答。'.repeat(80))+'\n\n|量|值|\n|---|---|\n|x|$x^2$|\n\n<img src=x onerror=alert(1)>';
w.dispatchEvent(new w.MessageEvent('message',{source:w,data:{type:'ENSEMBLE_QA_EXPAND',question,answer}}));
assert.equal(get('reader-entry-panel').hidden,true);
send('ENSEMBLE_QA_EXPAND',{question,answer});await flush();
assert.equal(get('reader-entry-panel').hidden,false);
assert.equal(pane.hidden,true);assert.equal(qa.hidden,false);
assert.equal(d.body.dataset.readerPanels,'2');
assert.ok(get('reader-entry-body').textContent.includes('一段不能被丢弃的回答。'.repeat(80)));
assert.equal(get('reader-entry-body').querySelector('table').textContent.includes('x'),true);
assert.equal(get('reader-entry-body').querySelector('.math-inline').textContent,'\\(x^2\\)');
assert.equal(get('reader-entry-body').querySelector('img'),null);
get('reader-entry-panel').scrollTop=200;
send('ENSEMBLE_QA_EXPAND',{question:'第二个问题',answer:'另一条完整答案'});await flush();
assert.equal(get('reader-entry-panel').scrollTop,0);
assert.equal(get('reader-entry-body').textContent.includes('完整答案'),true);
assert.equal(get('reader-entry-body').textContent.includes('一段不能被丢弃的回答'),false);
assert.equal(get('reader-entry-continue').hidden,true);
d.querySelector('main article p').click();await flush();
assert.equal(get('reader-entry-panel').hidden,true);assert.equal(qa.hidden,true);
assert.equal(d.body.dataset.readerPanels,'0');
assert.equal(JSON.parse(w.localStorage.getItem(key))[0].note,original.note);
""")


def test_opaque_white_paper_surfaces_and_rail_preserve_annotation_data():
    run_dom(r"""
const snapshot=w.localStorage.getItem(key);
assert.equal(get('reader-glass'),null);
assert.equal(get('qa-open-general').closest('#annotation-tools'),get('annotation-tools'));
assert.equal(w.localStorage.getItem(key),snapshot);
const css=d.querySelector('style').textContent;
assert.equal(css.includes('.annotation-preview:empty::before'),false);
assert.equal(css.includes('backdrop-filter'),true);
assert.equal(css.includes('background:rgba(255,255,255'),false);
assert.equal(css.includes('--paper:#fff'),true);
assert.equal(w.getComputedStyle(d.body).backgroundColor,'rgb(255, 255, 255)');
assert.equal(w.getComputedStyle(d.querySelector('main')).boxShadow,'0 6px 26px #0000001a,0 1px 6px #0000000d');
assert.equal(css.includes('prefers-reduced-motion:reduce'),true);
assert.equal(css.includes('data:image/svg+xml,'),true);
assert.equal(w.getComputedStyle(get('reader-position-rail')).right,'3px');
askFromNote(); await flush();
assert.equal(w.getComputedStyle(get('reader-position-rail')).right,'3px');
assert.equal(w.getComputedStyle(qa).right,'56px');
assert.ok(Number(w.getComputedStyle(get('reader-position-rail')).zIndex)>Number(w.getComputedStyle(qa).zIndex));
assert.equal(w.getComputedStyle(qa).top,'16px');
assert.equal(w.getComputedStyle(qa).bottom,'16px');
assert.equal(w.localStorage.getItem(key),snapshot);
""")


def test_old_json_hash_mismatch_requires_unique_quote_and_user_confirmation():
    run_dom(r"""
const field=get('annotation-import-file');
async function attempt(data) {
  const content=JSON.stringify(data);
  Object.defineProperty(field,'files',{configurable:true,value:[{name:'old.json',size:content.length,text:async()=>content}]});
  field.dispatchEvent(new w.Event('change')); await flush();
}
const data={format:'ENSEMBLE_READER_ANNOTATIONS_V1',report_key:'ensemble-reader:old-ui-json',records:[{...original,id:'from-old-json',block:10000}],qa_history:[]};
let asked=0;
w.confirm=()=>{asked++; return false;}; await attempt(data);
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,1);
assert.match(get('annotation-import-status').textContent,/已取消旧 JSON 迁移/);
w.confirm=()=>{asked++; return true;}; await attempt(data);
assert.equal(asked,2);
assert.equal(JSON.parse(w.localStorage.getItem(key)).find(item=>item.id==='from-old-json').block,1);
const count=JSON.parse(w.localStorage.getItem(key)).length;
await attempt({...data,records:[{...original,id:'ambiguous',quote:'beta'}]});
assert.match(get('annotation-import-status').textContent,/没有唯一原文匹配/);
assert.equal(JSON.parse(w.localStorage.getItem(key)).length,count);
""")
