"""Exercise the isolated reader Q&A renderer without a browser dependency."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


QA_ASSET = (
    Path(__file__).resolve().parents[1]
    / "src/project_ensemble/assets/reader_qa.html"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_reader_qa_renders_common_math_forms_and_waits_for_mathjax():
    script = r"""
const fs = require('fs');
const vm = require('vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const match = source.match(/<script>\s*(\(\(\) => \{[\s\S]*?\}\)\(\);)\s*<\/script>/);
if (!match) throw new Error('Q&A controller script not found');
const calls = [];
class Element {
  constructor(tagName) {
    this.tagName = tagName;
    this.childNodes = [];
    this.events = {};
    this.textContent = '';
    this.value = '';
    this.hidden = false;
    this.className = '';
  }
  appendChild(node) { this.childNodes.push(node); return node; }
  append(...nodes) { this.childNodes.push(...nodes); }
  replaceChildren(...nodes) { calls.push('replace'); this.childNodes = nodes; }
  addEventListener(type, callback) { this.events[type] = callback; }
  setAttribute() {}
  get childElementCount() { return this.childNodes.filter(node => node.tagName).length; }
}
const elements = new Map();
function getElement(id) {
  if (!elements.has(id)) elements.set(id, new Element(id));
  return elements.get(id);
}
getElement('qa-report-data').textContent = JSON.stringify({title: 'test', markdown: '# test'});
const listeners = {};
const messages = [];
const parent = {postMessage(data) {messages.push(data);}};
const window = {
  MathJax: {},
  addEventListener(type, callback) { listeners[type] = callback; },
};
const document = {
  documentElement: {dataset:{}},
  getElementById: getElement,
  createElement: tag => new Element(tag),
  createTextNode: value => ({tagName: null, textContent: value}),
};
const context = vm.createContext({window, document, parent, console});
vm.runInContext(fs.readFileSync(require('path').join(require('path').dirname(process.argv[1]), 'reader_markdown.js'), 'utf8'), context);
vm.runInContext(match[1], context);
const answer = [
  '行内 $a+b$ 和 \\(c+d\\)，同行展示 $$E=mc^2$$。',
  '',
  '$$',
  'x^2+y^2',
  '$$',
  '',
  '\\[z=3\\]',
  '',
  '```tex',
  '$notMath$',
  '```',
].join('\n');
function openSaved() {
  listeners.message({source: parent, data: {
    type: 'ENSEMBLE_QA_OPEN_SAVED', entry: {
      id: 'one', turns: [{question: '公式？', answer}],
    },
  }});
}
function descendants(node) {
  return [node, ...(node.childNodes || []).flatMap(descendants)];
}
openSaved();
const nodes = descendants(getElement('qa-conversation'));
const math = nodes.filter(node => node.className === 'math-inline' || node.className === 'math-display');
const code = nodes.find(node => node.tagName === 'code');
const expand = nodes.find(node => node.className === 'qa-expand');
expand.events.click();
const beforeLoad = calls.filter(value => value === 'typeset').length;
window.MathJax = {
  typesetPromise() { calls.push('typeset'); return Promise.resolve(); },
  typesetClear() { calls.push('clear'); },
};
listeners.load();
openSaved();
listeners.message({source:parent,data:{type:'ENSEMBLE_QA_APPEARANCE',palette:'butter'}});
listeners.message({source:{},data:{type:'ENSEMBLE_QA_APPEARANCE',palette:'white'}});
console.log(JSON.stringify({
  palette: document.documentElement.dataset.readerPalette,
  math: math.map(node => ({className: node.className, text: node.textContent})),
  code: code && code.textContent,
  beforeLoad,
  typesets: calls.filter(value => value === 'typeset').length,
  clears: calls.filter(value => value === 'clear').length,
  expansion: messages.find(message => message.type === 'ENSEMBLE_QA_EXPAND'),
  answer,
}));
"""
    result = subprocess.run(
        ["node", "-e", script, str(QA_ASSET)],
        check=True, capture_output=True, text=True,
    )
    observed = json.loads(result.stdout)
    assert observed["palette"] == "butter"
    assert observed["math"] == [
        {"className": "math-inline", "text": r"\(a+b\)"},
        {"className": "math-inline", "text": r"\(c+d\)"},
        {"className": "math-display", "text": r"\[E=mc^2\]"},
        {"className": "math-display", "text": "\\[\nx^2+y^2\n\\]"},
        {"className": "math-display", "text": r"\[z=3\]"},
    ]
    assert observed["code"] == "$notMath$"
    assert observed["beforeLoad"] == 0
    assert observed["typesets"] == 2
    assert observed["clears"] == 1
    assert observed["expansion"] == {
        "type": "ENSEMBLE_QA_EXPAND", "question": "公式？", "answer": observed["answer"],
    }
