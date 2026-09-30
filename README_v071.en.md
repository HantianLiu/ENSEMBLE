# Project ENSEMBLE v0.7.1

[中文说明](README_v071.md) · [English guide](README_v071.en.md)

This is an early preview for public feedback, not a stable unattended research
service. Users must verify model-generated scientific claims, citation mappings,
and final reports before relying on them, especially for consequential decisions.
Please use the GitHub Issues bug or research-feedback templates with synthetic
tasks, minimal reproduction steps, and redacted screenshots. Do not post real
meeting directories, evidence packets, API keys, or private research material.

This version coexists with v0.7.0 rather than upgrading its meetings in place.
It installs `ensemble`; the older version uses `ensemble-old`. Use separate
virtual environments. Project code and documentation are released under the
[MIT License](LICENSE). The bundled HarmonyOS Sans SC fonts retain their
[separate license](THIRD_PARTY_NOTICES.md); the project's MIT license does not
cover them. The PDF renderer bundles the Regular, Medium, and Bold weights.

HTML is the default rich reading format. A single file opens in a browser and
offers a table of contents, collapsible glossary and citation drawers,
scrollable tables, mathematics, highlights, annotations, and optional Q&A using
the reader's own DeepSeek API key. A literature report is rendered from one
frozen Markdown source into HTML and PDF; Markdown remains the editable,
auditable source. The PDF is still produced when possible. A citation click
currently shows the reference entry, **not** a source excerpt tied to the
sentence. If PDF rendering fails, completed Markdown and HTML remain in
`public/final/`, and the meeting is not falsely marked as fully published.
HTML mathematics currently loads MathJax over HTTPS; offline readers see
legible TeX if the script is unavailable. Complex PDF-rendered mathematics
should be checked against the frozen Markdown source.

Selecting body text in HTML opens a Highlight / Annotate / Ask AI toolbar.
Highlights are stored in the local browser by default. The left-hand list
shows compact, collapsible summaries; a plain highlight locates its source
without covering the page, while an annotation or saved Q&A can open details.
Entries and individual Q&A messages can be deleted. You can save a copy of
the HTML with annotations embedded for sharing. The API key is reused only in
the currently open Q&A window's memory; it is not written into HTML,
annotations, or Q&A history. Clear it before sharing a page or screen. Enter
keys only when you trust the report and its external scripts.

The Writer may put secondary source-status or implementation notes on single
Markdown blockquote lines beginning `> **来源状态**：` or `> **实现说明**：`.
HTML presents them as small clickable `[注]` drawer notes; PDF retains the
blockquote. A limitation that changes a scientific conclusion's scope must
remain visible in the main prose. Markdown footnotes (`[^note-id]`) are also
supported, with numbered endnotes in PDF.

Completed meetings can use `ensemble → Resume meeting → Add rendering formats`
to generate HTML, PDF, or both from the latest complete post-meeting erratum
or final Markdown, without calling a model or reopening review. This also
works for compacted archives after hash verification. Supplementary outputs
appear at the meeting root as `SUPPLEMENTARY_REPORT.html` and/or `.pdf`, with
source and output hashes. A successful HTML output remains usable even if the
PDF part fails.

## Install and start

Python 3.12 or newer is required. In this directory:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/ensemble
```

You may also run `scripts/ensemble`, which prefers this directory's `.venv`
and otherwise uses `ENSEMBLE_PYTHON`. `ensemble-v071` is retained as an alias.
Provider credentials and OpenAlex/Tavily settings use the existing
`ensemble.toml` / `model_config.toml` structure. `ENSEMBLE_CONFIG` can point to
another configuration. Do not install this and v0.7.0 into the same virtual
environment: both have the Python import name `project_ensemble`.

On first launch, choose Chinese or English for the interface. You can change
the UI language later in Settings. Each meeting chooses its own report
language; changing the UI does not translate existing output. The setup screen
introduces each meeting type, its stages, and model-selection roles.
Deliberation meetings can either accept a complete brief or help you develop
one through a preparatory Chair conversation before the meeting is created.

The four Representative persona names—Builder, Monitor, Cartographer, and
Librarian—are inspired by *Halo*: in the author's joking words, “the greatest
FPS game ever, to the best of the author's knowledge.” This is a naming nod,
not an official affiliation or a change to their responsibilities.

## What Deliberation does

Deliberation turns an unsettled set of rules, requirements, evaluation criteria,
or tightly constrained prompt instructions into an auditable normative document.
Representatives backed by different models propose, challenge, amend, and vote
on clauses. The Chair integrates accepted decisions under the meeting procedure;
decisions reserved for the Human remain with the Human. Its deliverable is a
decided rule or brief, **not** the result of carrying out that brief. It does
not, by default, search literature, verify empirical claims, or write a review.
Choose Claim verification for one factual proposition, or Literature research
for an evidence-based report.
The Audit meeting runner is not implemented, so Audit is hidden from new-meeting
setup and cannot be created non-interactively. Existing Audit meeting files
remain intact for a future version.

You may provide a complete brief at setup or develop one with a preparatory
Chair. That conversation designs the meeting's assignment; it is not itself
the formal deliberation.

## Literature workflows

Choose Literature research, then Simple or Full. The simple workflow has no
formal Chair. At setup, select a single academic Writer, Research Desk, two or
three models used only for module-split proposals, and at least two distinct
base models for science review. You may enter a finished research brief or
develop it in a preparatory conversation; the final brief is used to suggest
an editable meeting title.

Each split proposer performs one exploratory breadth-first search of at most
eight queries and submits an independent, problem-based module proposal in
parallel. The Writer receives only anonymous proposals and search clues in a
fresh call. It does not vote on, mechanically average, or concatenate them;
it writes its own taskbook without changing Human hard constraints. Formal
evidence verification begins only after Human approval of the module split.
The Writer then prepares per-module plans, can schedule up to three research
rounds per module (stopping early when appropriate), and writes chapters in
sequence. Research Desk can verify independent questions concurrently.
Librarians submit sealed science objections. A revision needs majority science
approval; otherwise the workflow pauses for a meaningful Human decision.
Citation and publication checks are deterministic. The simple workflow does
not add a Chair final review or require Human approval of each chapter.

Non-interactive setup uses `--deliverable-type literature_review
--literature-writing-policy fast`, with `--writer-model`, at least two distinct
`--model` values, `--research-model`, and two or three repeated
`--fast-planner-model provider:model` flags. Do not set `--chair` for fast mode.
Older fast meetings that have no frozen proposer roster continue their
original Writer-only planning route when resumed.

The full v0.7.1 writing procedure retains the existing research and evidence
pipeline but selects one dedicated academic Writer in addition to Chair and
Research Desk. Once a module's evidence dossier is frozen:

1. Builder proposes only the writing outline; three other offices vote over
   up to three versions.
2. One Writer drafts every module sequentially, receiving only that module's
   evidence, short earlier-module summaries, and a rolling glossary.
3. Librarians independently submit sealed science checklists. Chair handles
   each objection under the frozen procedure; the Writer revises, then a
   second checklist and local recheck follow.
4. Citation checks and publication assembly map temporary `C00001-00001`-style
   source IDs into reader-facing numbered references.

Writer prompts use finding-first evidence cards organized as concrete finding,
applicability, limitation, and source ID. A source catalog is separate from
the narrative, and an evidence dossier remains available for checking. The
Writer must organize the report by questions and evidence relationships, not
turn a source list into prose or treat a search snippet as a checked full text.
The reader-facing writing contract also preserves claims and citation scope
while improving syntax, argument signposts, paragraph structure, and
information layers. It does **not** replace science review or the still-planned
independent post-freeze readability-rewrite layer.

During interactive setup you can add local PDF, TXT, Markdown, HTML, or CSV
reference files; `--human-reference PATH` is repeatable for non-interactive
setup. Files are copied into the meeting with SHA-256 provenance. An upload is
only a candidate source, not proof: Research Desk must verify individual
claims. Scanned PDFs may be retained even when their text cannot be extracted.
The current input limit is 20 files per meeting and 100 MiB per file; this
setup route does not append files after the meeting begins.

## Managing meetings

The home screen lists meetings. Compact archive retains a meeting manifest,
original task, Research Desk evidence, literature bundle, Human references,
and a complete manuscript. For an unpublished meeting, only a fully persisted
whole-report draft can be promoted for inheritance; an individual chapter
does not qualify. A newer complete text in a frozen readability record must
pass SHA-256 verification before export. Compaction removes the original
process and audit records, so that meeting cannot resume its old flow. Its
evidence or manuscript can still seed a new meeting. An uncertified draft is
clearly labeled as such. `public/archive_manifest.json` records retained
files, hashes, and certification status.

Permanent deletion requires entering a confirmation phrase containing the
meeting ID. It runs in the background and leaves a status JSON and log under
the user-settings `deletions/` directory. Only `DONE` means the meeting has
been completely removed from the global list. Compaction and deletion release
the meeting lock before cleanup and retry transient NFS “directory not empty”
errors. At normal completion, the terminal asks whether to compact; paused
meetings do not get that prompt. Back up before either operation. ENSEMBLE
cannot recover permanently deleted files, and a running meeting must first be
safely interrupted and allowed to exit.

Model outputs and decisions are frozen as separate immutable artifacts;
resumption performs only unfinished steps. A meeting without the frozen
`literature_writing_policy = "v071"` retains its original procedure. The full
trial rules are in
[`literature_writing_v071.md`](docs/governance/02_deliberation/literature_writing_v071.md).

## Scope and verification

Run unit and regression tests with `python -m pytest`. This checkout has not
yet completed an end-to-end production literature meeting under every new
path. Before a large real meeting, try a small foreground task that can be
interrupted and resumed. Remaining work includes per-item pivotality for
rescue votes after a model failure, `Ctrl+L` Chair-delegation switching, new
Writer-initiated Research Desk questions during drafting, and a Human-triggered
post-publication rereview chain. These are not silently approved by default;
frozen files survive a pause. The longer [main README](README.md) also retains
legacy v0.7.0 background; its historical `ensemble-v07` examples refer to the
older `ensemble-old` runtime.
