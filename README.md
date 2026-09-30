# Project_ENSEMBLE

**当前版本 / Current version:** [中文说明](README_v071.md) · [English guide](README_v071.en.md)

Project ENSEMBLE 是一个多模型研究与审议工作台。当前 `v0.7.1` 是供反馈的早期
预览版：它支持文献调研的简易与完整流程、单项命题核实、议事会议和学术重绘。
议事会议让不同模型对规则、标准或提示词委托逐条提案、质疑和表决，交付可审计的
规范性文本；它不负责执行该委托，也不默认撰写文献综述。
模型生成的科学结论与引文仍需人工核对。代码采用 [MIT 许可](LICENSE)；随附字体
遵循[独立许可](THIRD_PARTY_NOTICES.md)。请从上方对应语言的当前版本说明开始，
下面较长的英文内容主要记录旧版背景和设计沿革。

Project ENSEMBLE is a multi-model research and deliberation workbench. Version
`0.7.1` is an early feedback preview, not an unattended research service.
Deliberation has independent models propose, challenge, and vote on clauses for
rules or prompt briefs. Its output is an auditable normative document, not
execution of the brief or a literature review. The Audit meeting runner is not
implemented and is hidden from new-meeting setup.
Human verification of scientific claims and citations remains necessary. The
code is [MIT-licensed](LICENSE); the bundled font has a
[separate license](THIRD_PARTY_NOTICES.md). Start with the current-version guide
linked above; the longer English material below also retains legacy design context.

Version **0.7.1**. For the version-specific quick start and current trial limits,
read [`README_v071.md`](README_v071.md). This sibling distribution installs the
canonical `ensemble` command; the previous v0.7.0 runtime is now invoked as
`ensemble-old`. `ensemble-v071` remains a compatibility alias for v0.7.1.
Run this checkout's `scripts/ensemble` directly, or install it into a separate
virtual environment:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/ensemble
```

For new literature-report meetings, v0.7.1 asks for a dedicated academic Writer
model and reasoning effort. After each module's evidence dossier, Builder proposes
a structure-only outline, three offices vote on it, one Writer drafts sequentially,
and one primary Librarian performs two sealed science-review rounds before citation and
publication assembly. Another Librarian model is a failure-only standby, not a routine
second voter. This Human-directed runtime override leaves previously frozen votes intact;
the original trial policy is recorded in
[`docs/governance/02_deliberation/literature_writing_v071.md`](docs/governance/02_deliberation/literature_writing_v071.md).
The initialization menu also offers a separate experimental **fast literature**
workflow: two or three selected models independently propose problem-based
module splits after one exploratory search each. A Writer sees their proposals
anonymously, establishes a Human-approved taskbook, then plans and writes
chapters. Research Desk gathers evidence; science review has a primary model
and a distinct failure-only backup model.
This mode has no Chair; it
does not replace or migrate the ordinary workflow.
The policy is frozen per new meeting; existing v0.7.0 meetings retain their old
flow and should be resumed with `ensemble-old`. The remainder of this README
documents the inherited v0.7.0 interface and legacy workflows; historical
`ensemble-v07 ...` examples below refer to that compatibility alias, whose
canonical name is now `ensemble-old ...`. In this checkout, bare `ensemble`
always starts v0.7.1.

The inherited v0.7.0 distribution used to install `ensemble`; that runtime is
now exposed as `ensemble-old`, with `ensemble-v07` retained as a compatible
version-pinned alias. Existing v0.6 and v0.7.0 meeting workspaces are not
changed in place.

**Experimental constitutional framework and orchestration toolkit for multi-model epistemic deliberation.**

This repository combines two layers:

1. **Governance specification** — the institutional rules, anti-gaming firewalls, personas, deliberation protocol, Think Tank review, and Audit Conference.
2. **Reference implementation** — conservative, fail-closed building blocks that enforce the parts of the protocol that are already sufficiently specified.

The project does **not** claim to be an optimal multi-agent architecture. Its design priority is deliberately conservative:

> **Safety and correctness first; epistemic and procedural integrity next; token and latency efficiency are secondary optimizations.**

Cost is optimized only inside an acceptable epistemic and procedural envelope. When uncertainty cannot be safely resolved, ENSEMBLE prefers explicit uncertainty, preserved dissent, a paused state, or human escalation over forced convergence.

## Implemented now

- Provider abstraction and live model discovery for:
  - DeepSeek/OpenAI-compatible endpoints;
  - Kimi/OpenAI-compatible endpoints;
  - GLM/OpenAI-compatible endpoints;
  - Gemini REST Models API;
  - optional Codex App Server with ChatGPT subscription sign-in.
- Human-selected provider/model roster support.
- Provider-specific capability filtering and optional `selectable_models`
  candidate lists, so non-chat catalog entries do not flood the startup UI.
- Private Representative Registry and meeting-local IDs.
- Stage-local Representative context assembly.
- Persona isolation and anti-gaming visibility boundaries.
- Sealed ballots with no partial-tally disclosure.
- Independent votes inside one sealed window use bounded per-model lanes: different
  models may run concurrently, while same-model calls obey the frozen concurrency
  limit and stable persona order. Dependent ballot rounds and sequential amendments
  remain ordered.
- No-abstention ballot semantics.
- Type III 的任意前二 cutoff 并列先由全部 ACTIVE voter 加赛；加赛边界仍
  并列时由 Chair 公开投决定票，Human 不代选实体方案，原首轮票不重做。
- Provider retry policy: initial call + 3 retries, then `REPRESENTATIVE_UNAVAILABLE` / meeting pause.
- Governance-private Drafting Alignment calculation.
- `floor(30%)` Consultative selection with tied cutoff retained.
- Amendment records, impact scopes, Chair procedural rulings, and explicit conflict graph.
- Append-only hash-chained event log.
- Audit Conference speaking-order controller; PASS consumes a speaking turn; max 5 turns/model.
- Strategic-behavior audit data structures.
- Chair Handoff Brief + Think Tank Review as parallel human-facing outputs.
- Required terminal startup wizard for meeting type, live provider/model selection,
  one shared Representative reasoning-effort setting, Chair model and separate
  reasoning-effort setting, optional Research Desk configuration, task description,
  and optional human-escalation email.
- Optional shared Research Desk with claim normalization, adversarial OpenAlex + Tavily
  retrieval, structured evidence packets, freshness-checked claim caching, and
  audit-private query/screening traces. Unsupported `CLEAR` consensus is downgraded
  to `QUALIFIED` when the underlying packet remains usable. It is not a Representative
  and has no vote.
- Derived literature-review meetings preserve the source meeting as read-only, copy its
  public Research Desk packets and literature bundle with a per-file SHA-256 lineage,
  and reuse fresh matching packets through the ordinary Research Desk cache. A
  deterministic four-member planning panel—one Builder, Monitor, Cartographer, and
  Librarian—submits sealed decompositions of at most eight modules each. Chair clusters
  them, all Representatives perform one bounded structural review, and Chair freezes the
  research outline. The resumable module pipeline then performs evidence coverage,
  drafting, bounded review, confirmation, synthesis, Think Tank checks, and publication.
  The same workflow can start from scratch with an empty meeting-local evidence database;
  choose option 5 in the startup UI instead of selecting a parent meeting. Both
  entry points ask for a target report-body length in non-whitespace Unicode
  characters (excluding references and standalone appendices). The ±20% range
  guides planning and chapter drafting; publication records the measured deviation
  without blocking an otherwise completed report. Non-interactive startup accepts
  `--literature-target-body-characters`.
- Literature-review writing uses task-level academic prose rules independently of the
  four Representative analysis lenses. At outline approval, Human can set the
  expected reader's familiarity with each proposed discipline and approve an article
  skeleton and optional glossary appendix. Each chapter writer sees its chapter
  position, completed-chapter summaries, and the planned topics of later chapters;
  later findings are not prewritten. Reader-facing prose cites stable temporary
  chapter source IDs such as `[C1-2]`, while evidence-packet IDs remain provenance
  metadata. Reviewers receive the chapter source catalog, and assembly deduplicates
  works by strong identifiers and numbers references by first textual appearance.
  Purely stylistic review notes do not change a vote; Chair may copy-edit but may
  not contribute new scientific claims or silently decide factual conflicts.
- Newly initialized v0.7 literature-review meetings with Research Desk enabled also
  run a bounded exploratory search before the four-member planning panel drafts its
  module split. Chair's original questions, answers, and source catalog are published
  for the panel without Chair interpretation or proposed module names. Each planner
  may independently ask follow-up questions, including provisional module names;
  its Q&A stays private until all four planning submissions are frozen, then is
  released together. Chair and each planner have a separate maximum of 16 executed
  search queries per planning cycle; fresh meeting-local cache hits cost no quota.
  Exploratory answers are source-linked research maps, not claim-QC verdicts. Full
  retrieval traces remain audit-private; available original documents are archived
  in the meeting literature bundle. Older v0.7 meetings without the frozen
  `planning_exploration_enabled` manifest flag keep their original planning flow.
- Separate deliberation and fresh Audit Member registries.
- Scheduler-managed email escalation through a minimal Slurm job, with durable
  event records and per-event job-ID receipts. SMTP remains an optional transport.
- Live terminal progress for the current phase, provider-call status, and public
  speech content. Fixed-size work windows use an in-place two-column task table:
  pending tasks are gray, running tasks are white, completed tasks are green, and
  failed tasks are red; transient retries and bounded status details stay in the
  same row while sealed multi-party submissions remain hidden until frozen.
  Its footer also shows meeting-local, deduplicated scholarly-source and web-page
  record counts. These are sources inspected/recorded by retrieval, **not** a
  claim that any model read every page of every archived full text.
- OpenAI-compatible and Gemini calls use streaming transports in interactive runs.
  Streaming is used only as a liveness signal for the in-place status row. The
  terminal never streams response fragments, hidden chain-of-thought, or sealed
  content into the scrollback buffer.
- TTY-aware colored phase separators and bordered public-speech blocks; redirected
  output and `NO_COLOR` environments remain plain text. Human-facing call lines
  include the participant ID, actual provider/model, and bilingual Representative
  position label. The persisted internal persona code is unchanged.
- Provider-neutral token telemetry after each completed model call: prompt,
  cached, cache-miss, completion, reasoning, and total token counts plus the
  derived cache-hit rate. Each record is retained under
  `governance_private/telemetry/` without prompt or response content.
- A private `MeetingEngine` invocation boundary that records provider exchanges,
  preserves retry/pause semantics, escalates task failures, and strips hidden
  reasoning fields before any response is retained.

## Deliberately *not* implemented as policy

Several adopted next-version rules are not yet executable and code must not pretend
otherwise. See `TODO.md`. Important boundaries include dynamic pivotal-missing-ballot
handling, two-level per-item malformed-output recovery, backup-Chair transfer, and the
full Audit/Correction state machines. The current v0.7.1 release hides Audit
meeting creation and the `run-audit` command; existing Audit records remain
unchanged until a future runner is available. Earlier versions exposed a
non-mutating fail-fast interface.

Atomic drafting-item granularity, the detailed-clause docket, Clause Split
support, and Motion to Suspend support currently use explicitly marked trial
rules pending Think Tank review; they are not permanent constitutional rules.

Where one of these decisions is required, the reference implementation raises a policy/configuration error rather than silently selecting a rule.

## Quick start

首次安装、Windows / macOS / Linux 环境要求、可选 CLI 和密钥来源见
[安装与配置指南](docs/INSTALL.md)。安装后直接输入 `ensemble` 即可进入首页；
“设置”可配置界面外观、供应商、联网搜索后端和证据包有效期，首次使用不必先手工创建 TOML。
新会议会询问可读标题；会议索引统一保存在用户配置目录，首页可按标题选会，
也可以输入 `ensemble "会议标题"`（标题不重复时）。

```bash
# Use a dedicated environment; do not share v0.6's environment.
python -m venv .venv
source .venv/bin/activate
python -m pip install -e .
ensemble               # 首页 → 设置 → 模型供应商／联网搜索
ensemble doctor         # 配置完后检查
ensemble discover-models
ensemble "会议标题"       # 标题唯一时可跨目录恢复
```

如需手工维护配置，可复制 `examples/ensemble.example.toml` 与
`examples/model_config.example.toml`，并用 `ENSEMBLE_CONFIG` 指定主配置路径；
这两个示例文件不是向导模式的前置条件。开发测试需另外安装 `.[dev]` 并运行 `pytest`。

The example uses `governance_docs = "@package"`, which resolves to the governance
package shipped in the source checkout or installed wheel. It therefore needs no
machine-specific absolute path. To invoke v0.7.0 from any working directory, set only
the configuration location:

```bash
export ENSEMBLE_CONFIG=/absolute/path/to/ensemble.toml
ensemble doctor
ensemble              # 打开首页：新会议，或恢复/接续已有会议
```

For a source checkout, `scripts/ensemble-v07` is an optional launcher that selects
the checkout's `ensemble.toml` unless `ENSEMBLE_CONFIG` is already set. On an
interactive Linux terminal, both this launcher and the Python entry point enable
the terminal's `IUTF8` flag. Human input additionally
uses an echo-free, Unicode-aware line editor which redraws the complete line after
backspace, so a double-width Chinese glyph cannot leave a one-cell visual remnant.

Each newly created meeting freezes the configuration and optional model-config bytes,
their SHA-256 hashes, software version, governance digest, original Human prompt, and
lineage. Commands operating on that meeting can recover its original configuration path:

```bash
ensemble-v07 open /absolute/path/to/DL-XXXXXXXX
ensemble-v07 request-human --meeting /absolute/path/to/DL-XXXXXXXX \
  --reason-code POLICY_NOT_CONFIGURED --summary "Human policy choice required"
```

Bare `ensemble` (or the backward-compatible `ensemble-v07`) opens the interactive home
screen. It can create a new meeting, open settings, or select an existing meeting from
the current directory or the user-global index. Opening an existing meeting offers separate
choices to continue its frozen workflow, ask the Chair about the latest public
draft and literature, or start a successor meeting. A completed normative meeting
can directly start a literature-review successor; a completed literature review
can directly start scholarly rendering. An uncertified literature draft can enter
rendering only through its existing frozen-readability-failure transfer. The source
meeting remains read-only during successor creation. New meeting directories are
created under the shell's current working directory.
On this installation the earlier runtime remains available as `ensemble-v06`;
existing version-pinned resume scripts continue to select their original runtime.

Chair Q&A is a Human-only sidecar in `human_private/chair_qa/`. At entry the Human
selects a model independently of the original Chair. Answers identify the current
draft version and cite retrieved public report, evidence-packet, or archived-literature
material. The Chair can request new external retrieval when existing material is
insufficient; new sources remain in this Q&A sidecar and never enter the original
meeting's ballots, representative-visible evidence, or published report. Questions,
answers, and retrieved sources persist across draft revisions, while each answer
retains the version and source list used when it was written. Locally archived PDFs
are text-searchable through the declared `pypdf` dependency; a source checkout
missing that dependency can still search evidence-packet metadata but must not
claim to have read PDF full text.
New IDs use `AU-` for Audit, `LR-` for literature-review or research-only meetings,
`DL-` for normative deliberation, and `SR-` for scholarly rendering. `VR-` is reserved
for a future one-shot verification meeting type; v0.7.0 does not yet run that procedure.
Existing `M-` meetings retain their IDs and remain discoverable and resumable. The
explicit `ensemble-v07 start` command remains available, as do
`run-general`, `run-report`, `run-render`, and `run-research`; existing automation and old resume
scripts therefore remain valid. Relative `governance_docs` values are resolved
relative to the configuration file itself, not the caller's current working directory.

Every meeting has a human-readable title in its manifest and in the installation-local
index at `.ensemble/meetings.json` beside the active configuration file. Opening a
completed meeting offers three explicit continuation scopes: inherit only the public
evidence/literature package, inherit only the final resolution or report as an advisory
document, or inherit both. An inherited final document is not promoted to Constitution
status and does not trigger strict clause-by-clause compliance bookkeeping.

If the completed meeting published a literature-review Markdown report or a scholarly
rendering Markdown report, opening it also offers **direct scholarly rendering**. This shortcut fixes the new meeting type to
`scholarly_rendering` and inherits both the frozen report and evidence package; it does
not rerun the source meeting's research, reviews, or votes. The Human still chooses
the rendering Chair, science/citation reviewers, output language, and formats.
The rendering menu offers either the whole report or a local range, including a range
of an already rendered report. For a local range, describe the desired passage in
natural language. Chair maps that request to source blocks; the Human sees the
proposed selected/preserved boundary and must approve it before drafting starts.
An inaccurate boundary can be returned with a reason and replanned. Unselected blocks
retain their frozen source text and a per-block provenance record; only selected
blocks run the ordinary science and citation checks. Existing rendering meetings
without a local-range setting continue their original whole-report flow.

On a safe `Ctrl+C`, ENSEMBLE prints the canonical `ensemble resume <absolute meeting
path>` command and atomically writes both `<meeting-id>_resume.sh` and `resume.sh`
in the launch directory. Both scripts include the meeting ID and human-readable title
and show them when executed. The ID-first filename makes shell completion unambiguous.
Older `resume_<meeting-id>.sh` helpers remain usable and are not removed; `resume.sh`
is refreshed at the next interruption.

### Importing an unfinished v0.6 meeting

The v0.7.0 runner does not write into a v0.6 meeting. To continue a v0.6 meeting
under v0.7.0, stop the old process and import a verified copy into a **new** path:

```bash
ensemble-v07 migrate-v06 \
  --meeting /absolute/path/to/M-OLDID \
  --output /absolute/path/to/M-OLDID-v07 \
  --config /absolute/path/to/v07/ensemble.toml \
  --dry-run
ensemble-v07 migrate-v06 \
  --meeting /absolute/path/to/M-OLDID \
  --output /absolute/path/to/M-OLDID-v07 \
  --config /absolute/path/to/v07/ensemble.toml
ensemble-v07 open /absolute/path/to/M-OLDID-v07
```

Use a separate v0.7.0 configuration file; the importer refuses the path frozen
as the old meeting's v0.6 configuration. The import checks the legacy event hash
chain, a stopped meeting run lock, manifest consistency, configured providers,
every source file, and local symlinks.
It copies frozen submissions and documents without rewriting them, then records
the v0.7.0 governance digest, configuration snapshot, original task prompt, and
migration provenance in the copy. The original directory remains available for
v0.6 rollback. The copied meeting keeps its meeting ID, so open it by **path**;
the installation-local ID index can point to only one copy at a time. Rules for
future, unfinished steps are those of v0.7.0; already frozen decisions are not
re-voted. Audit meetings cannot be imported because v0.7.0 has no Audit runner.
The import makes no model calls and never resumes automatically. If a resumed
legacy artifact requires a procedure that v0.7.0 cannot interpret, execution
pauses and the original v0.6 meeting remains intact.

`ensemble-v07 start` performs live model discovery and creates the durable,
compartmentalized meeting workspace. The notification address may be left blank,
and `[notifications.email].enabled` may remain `false`; if human attention is
later required, ENSEMBLE records `NOTIFICATION_SKIPPED` in the append-only event
log. Interactive deliberation startup then runs the currently supported
general-principle stages and displays phase changes, model-call activity, and
public speech content in the terminal. It does not silently execute policy
branches that remain unresolved in the governance documents.

The startup wizard treats reasoning effort as an input control parameter, not a
measurement of model quality or actual reasoning-token use. `default` sends no
reasoning control to the provider. For a mixed Representative roster, the menu
exposes the union of legal `low`, `medium`, and `high` controls supported by the
selected models. One Human choice still governs the roster, but each less-capable
model is deterministically collapsed to its nearest legal level; a model with no
explicit control uses `default`. Both the requested setting and every model's
effective setting are frozen. Chair and the optional Research Desk retain their
own single-model settings.

If Research Desk is enabled, its selected model and reasoning setting remain
identity-private and do not add a meeting participant. Its trial retrieval stack
uses OpenAlex as the primary scholarly index and optionally Tavily as the general-Web
supplement. The configured freshness window is 7, 30, or 180 days from packet
retrieval time for volatile, versioned, or stable claims respectively; this is a
cache revalidation control, not a claim that evidence stays correct for that long.
During meeting initialization, the Human chooses the OpenAlex candidate limit per
adversarial query: pressing Enter selects the suggested 12 results, and the allowed
range is 1–50. This is a retrieval input limit, not the number of sources that must
appear in the final evidence packet. The choice is frozen for the meeting and used
again on resume; meetings created before this control keep their existing config
behavior. No additional reranking model is used.
New meetings also freeze an HTTP 429 policy. By default, a confirmed daily
credit shortage asks the Human whether Tavily may handle pending searches. If
declined, the fast workflow finishes already-running non-search work and pauses
without waiting for the next daily reset. A 429 that is not confirmed as daily
exhaustion receives short backoff retries instead.
If a 429 omits balance headers, ENSEMBLE queries the authenticated OpenAlex
`/rate-limit` endpoint and caches that diagnostic briefly; it does not infer
daily exhaustion from HTTP status alone. The bundled configuration sends at
most one OpenAlex HTTP request at a time per ENSEMBLE process and spaces search starts by at least
one second. The gate covers each OpenAlex HTTP request only: source reading and
model synthesis may overlap with the next search. Those are input controls, not
an assertion about OpenAlex's global service capacity. A Human may pre-authorize
Tavily for affected 429 searches; the evidence packet records any missing
OpenAlex coverage, and the fast workflow makes an opportunistic OpenAlex recheck
after Tavily source reading. A previously frozen Tavily-only result with an
OpenAlex 429 is never overwritten: if its module evidence dossier is not yet
frozen, a successful OpenAlex recheck adds a separate evidence packet to that
dossier.
Ctrl+R can change this policy for not-yet-started calls in either a new or an
existing meeting, without rewriting its initialization manifest. In staged fast
research and Representative model lanes, model changes and concurrency increases
take effect as soon as the Human finishes the menu; decreases stop refilling
slots until active calls drain below the new cap. After a repeated OpenAlex connection failure, Tavily
takes over automatically when configured. Ordinary web and regulatory searches
use Tavily as their primary discovery backend. Older meetings retain their
original retrieval policy. In the fast literature workflow, independent Research
Desk questions move through separate normalization, retrieval, source-reading,
and synthesis queues, so a delayed search does not block earlier work on other
questions. The menu choice is a frozen input policy, not a measured guarantee
that either external service will be available.
Evidence packets are shared under `public/research/evidence_packets/`,
while exact queries, candidates, exclusions, and screening reasons remain under
`audit_private/research/`.
Every new Research Desk request, including requests made after resuming an older
meeting, now reads a bounded selection of original sources *before* synthesis.
For selected public web pages, Tavily Extract obtains page text when Tavily is
configured; public PDFs and other documents are fetched directly and relevant
text is extracted. The model receives only short, attributed excerpts, not an
unbounded full-document dump. Read attempts, content digests, selected excerpts,
and failures are recorded under `audit_private/research/source_reads/`. Search
snippets remain discovery leads: a source without a readable original excerpt
cannot by itself substantiate `SOURCE_BACKED`. Previously frozen evidence
packets and reports are not rewritten. An already-running CLI process must be
safely stopped and resumed to load this code; no meeting migration is required.
If an original is blocked, unreadable, or lacks a readable link, Research Desk
uses a bounded follow-up search based on the document's title or DOI. Results
from other sites are independent, unverified candidates, not automatic copies
of the original: the Desk must check identity, version, and the relevant passage
before relying on one. Search queries, rejected candidates, and read attempts
are retained in the private audit trail.
Non-fatal duplicate PDF `/Length` parser warnings are counted in each source's
private read record rather than repeated in the live terminal. Affected PDF
excerpts are flagged for page-level verification; pages that cannot be parsed
are skipped and recorded without blocking other sources.

For v0.7.1 scientific manuscript review, Librarians receive a bounded digest
of findings from packets cited by the current chapter, including contradictory
findings and scope limits, rather than the full immutable evidence dossier.
Omission counts and the full dossier path are supplied so a shortened input is
not mistaken for a complete source inventory. Codex calls also check the
provider's character limit separately from the model's token context budget.

For meetings with Representatives, initialization also offers optional maximum
parallelism for independent sealed submissions. When enabled, each selected
Representative model is frozen at up to four simultaneous in-flight calls;
with three distinct models and four personas each, all 12 Representatives can
begin the same independent ballot window together. Dependent ballot rounds and
sequential amendments remain sequential. This can reduce waiting time but may
lower provider prompt-cache hit rates or hit provider concurrency limits. The
default leaves existing configured, discovered, or safe-fallback caps unchanged.

Deliberation meetings also freeze a Human-selected decision-rigor mode. The
default **strict** mode retains the established `ceil(3*N_ACTIVE/4)` high
threshold. **Relaxed** mode changes only votes that originally require that
3/4 high threshold to `floor(N_ACTIVE/2)+1`, where `N_ACTIVE` is the number
of eligible active voters for that item. Existing absolute-majority, 1/4,
1/3, and other thresholds are unchanged. The public meeting manifest and
`public/decision_policy.json` record the Human choice; a meeting created
before this control remains strict. For artifact compatibility, some frozen
result JSON retains the legacy field name `supermajority_required`; the
recorded formula and public decision policy define its actual threshold in
relaxed meetings. The pre-translation governance package is preserved under
`docs/governance_legacy_2026_09_23`; resumed meetings select the exact package
matching their pinned governance digest. New meetings use the updated package.

Representative prompts now use separate common rules and functional lenses for
deliberation and literature-review meetings. Human-readable instructions for
literature planning, research, drafting, review, and scholarly rendering are in
Chinese; JSON field names and protocol enum values remain unchanged for API
compatibility. Literature writing instructions explicitly ask for a scholarly
review rather than a normative instrument.

New literature-review setup also freezes the report language (`zh`, `en`, or
`fr`), independent full-report and chapter-summary choices, and 1–5 controls
for paragraph segmentation and prose liveliness. After the Chair's proposed
research outline, the Human approves or rejects the modules, may return only
the article skeleton for local revision, and sets expected reader proficiency
for each proposed discipline. The writing task rules apply to every drafter and
Chair integration independently of the four functional office prompts. A
chapter cites a real source using a stable `Ca-b` working ID; final assembly
maps these to first-appearance numbered references and keeps packet IDs in
separate provenance. Objective identifier leaks receive a local, audited
readability repair before publication; they are not automatic scientific vetoes.

Human-facing runtime labels are bilingual office titles: `Builder / 建构者`,
`Monitor / 监管者`, `Cartographer / 制图者`, and
`Librarian / 智库长`. These labels are presentation-only; prompts,
persisted persona codes, and governance semantics retain their internal values.

The meeting-type menu also offers a research-only meeting. It creates no voting
Representatives and no Chair: the Human enters one concrete, externally
verifiable claim, the selected Research Desk performs adversarial retrieval and
evidence synthesis, and the meeting publishes `RESEARCH_RESULT.json` together
with the downloadable `LITERATURE_BUNDLE.zip` at the meeting root. If it is
interrupted, resume it with `ensemble resume /path/to/LR-XXXXXXXX`.
In interactive startup, the Human can instead discuss an initial idea with a
temporary preparatory Chair, request a candidate claim with `/draft`, and
confirm or edit it before the meeting starts. `/back` returns to direct input.
This drafting assistant does not become a formal Chair or verify facts; the
approved final claim is saved as `original_prompt.txt`, while the preparatory
dialogue is retained privately in `human_private/prompt_development.json`.

For a new or continued literature-review meeting, interactive startup also
offers a conversation with the selected meeting Chair before research begins.
The Human may describe the task over several turns, use `/draft` at any point
to request the current best complete brief, ask for a full revision, or use
`/back` to return to direct input. The Chair may stop asking questions once
key requirements are clear, but only the Human can confirm the final brief.
No retrieval or report writing occurs during this preparatory conversation.
The confirmed brief becomes `original_prompt.txt`; the conversation is kept
in `human_private/prompt_development.json`.

The menu also offers a derived `scholarly_rendering` meeting for a completed
literature-review report. Chair is the lead renderer; the Human separately selects
at least two science-bookkeeping models and two citation-bookkeeping models, freezes
the sequential science-review order, and chooses one output language (`zh`, `en`, or
`fr`), optional article/abstract structure, segmentation and liveliness controls,
and one or more of interactive HTML, Markdown, LaTeX, and PDF. The source report remains immutable and
prominently linked. Science bookkeeping runs before citation bookkeeping; any
scientific correction beyond the source requires Research Desk support. Resume this
meeting with `ensemble-v07 open /path/to/M-XXXXXXXX` or the compatible explicit command
`ensemble-v07 run-render --meeting /path/to/M-XXXXXXXX`.
Pressing Enter at the output-format prompt defaults to interactive HTML. Direct CLI
creation without `--rendering-output-format` uses the same default. Selected
final artifacts are linked at the meeting root as `FINAL_REPORT.html` and,
when selected, `FINAL_REPORT.md`, `FINAL_REPORT.tex`, and `FINAL_REPORT.pdf`; versioned originals
and provenance remain under `public/final/scholarly_rendering/`.
The publication assembler carries the source report's main title into the
reader-facing report, groups the approved rendering sections under their
original research modules, adds a numbered contents list of main chapters,
and nests section-local headings below that level.
For a meeting already completed before this presentation fix, run
`ensemble-v07 reassemble-render --meeting /path/to/M-XXXXXXXX`. This makes
`SCHOLARLY_REVIEW_STRUCTURED.md` (and the selected PDF/LaTeX formats) in the
meeting root without rerunning models or overwriting the frozen original.
The current presentation edition also normalizes chapter/subsection numbering,
keeps ordered lists numbered, and renders Markdown emphasis, tables, links,
and long numeric citation runs in the LaTeX export. Citation ranges are only
typographic compression: the frozen source retains every original citation
ID in its original order. The `.tex` file targets XeLaTeX with `ctexart`,
`geometry`, `longtable`, `array`, `xcolor`, `hyperref`, `amsmath`, and `amssymb`.
Inline `$...$` / `\(...\)` and display `$$...$$` / `\[...\]` mathematics
remain TeX in that file. The bundled PDF continues to use ReportLab when a
TeX engine is unavailable; supported formulas are rendered with Matplotlib
mathtext as vector glyphs, not embedded formula images or raw TeX. Most ordinary
symbols remain selectable; some decorative non-BMP mathematical alphabets may
still have limited text extraction in PDF readers. Unsupported expressions
produce an explicit publication error instead of silently corrupting the PDF.
Each reassembly writes a new versioned edition and
keeps earlier editions and frozen section files unchanged.
The newer structured presentation edition additionally numbers structural
headings inside each chapter, expands clear `(1)(2)`-style inline enumerations
into numbered lists, sorts numbers within each citation bracket without changing
the referenced IDs or their occurrence counts, and maps legacy internal module
references to the published chapter numbers. Section source files remain
unchanged. New drafting prompts ask writers to supply semantic headings without
manual numbering and to keep internal tracking identifiers out of reader prose.
Each rendering block receives the global outline, its chapter position,
adjacent context, and numeric references present in the frozen source. A
chapter-opening introduction uses a labelled callout instead of a numbered
subsection. The optional PDF uses chapter page breaks, a generated table of
contents, framed introductions, repeated table headers, and field cards for
tables wider than four columns. PDF is generated by ReportLab, not by a TeX
compiler. The in-process structural renderer does not require Pandoc, which
is not installed in the current ENSEMBLE runtime.
In newly created rendering meetings, each science reviewer can make up to four
Research Desk checks per section before final science ballots, shared across
revision cycles. The reviewer sees each result before deciding whether to ask
again; duplicate questions reuse evidence without spending another check.
Meetings created before this policy keep their original frozen rules.

The scholarly-rendering setup asks whether unresolved, itemized scientific
objections should be decided by the Human (default) or by a Human-authorized
Chair. During a running meeting, the Human can switch this at any time from a
second terminal with `ensemble-v07 science-authority --meeting /path/to/SR-XXXXXXXX
--mode chair` or `--mode human`; omit `--mode` to inspect the effective setting.
The switch affects only future unresolved science objections, never citation
questions or other Human consultations. The consultation screen also offers
`a` to authorize Chair from that point onward. Authorization changes and Chair
decisions are recorded separately from Human rulings.

An unfinished literature-review meeting that froze Chair publication patches but
failed its final readability check may be resumed with `ensemble-v07 resume
/path/to/M-XXXXXXXX`. The same prompt appears through v0.7 `open`, `run-report`,
and compatible `run-general` entry points. Its menu offers an explicitly Human-selected transfer into
scholarly rendering. The new meeting copies the frozen Chair-patched text as an
**uncertified source draft** plus the evidence database; it does not mark the
original meeting complete or rerun its research. Source status and hashes remain
in the continuation ledger and the reader-visible rendering appendix.

For meetings that enable Research Desk, `ensemble-v07 run-general` now runs sealed
Research Rounds at the implemented substantive window boundaries. Completed
rounds are released through `public/research/rounds/<round-id>/evidence_snapshot.json`;
incomplete rounds remain under governance-private staging and resume without
rerunning complete claims. The manual `ensemble-v07 research-claim` command remains
available for an explicitly human-requested, non-batched lookup, but it refuses
to publish while a formal round is incomplete. The run result reports released
round count and distinct released evidence-packet count from durable snapshots.
After normalization and deduplication, independent claim groups run as a bounded
pipeline. Meeting initialization asks for the maximum number of independent
Research Desk tasks in flight; it defaults to `research.max_concurrent_claim_groups`
and is frozen in the meeting manifest. The same cap applies to independent
cache-only coverage checks in literature-report modules. Sequential Representative
follow-ups remain sequential. Older meetings without this field retain their
previous configuration/model-cap behavior. Larger caps may increase provider
rate limits and reduce prompt-cache reuse. `research.openalex_max_concurrent_requests`,
`research.tavily.max_concurrent_requests`, and
`research.max_concurrent_document_downloads` separately bound the three external
resources. Each completed group is staged privately, final
resolutions retain frozen group order, and the literature manifest and ZIP are
rebuilt only at the round-wide release barrier.
The trial request caps are stage-scaled: the first general-principle research
round allows 4 claims per Representative; post-amendment conditional rounds and
the final Veto round allow 2; the pre-clause-review round allows 4; and the
post-clause conditional round allows 2. The D1→D2 and D2→D3 position windows
reuse the public evidence database instead of opening another pre-round, and C0
drafting reuses the final-ratification snapshot. A round freezes its cap in
`round_policy.json`; an older round already in progress retains the legacy cap
of 4 for recovery compatibility.
OpenAlex authentication is loaded from `OPENALEX_API_KEY` or the configured
literal assignment file and sent only as a Bearer header. The key is never written
to meeting artifacts. Each private query trace records OpenAlex's daily credit
limit, remaining credits, request cost, and seconds until the UTC reset so a 429
can be distinguished from a transient backend failure.

At the provider-selection prompt, enter comma-separated numbers such as `1,2,3`,
or press Enter to select every enabled provider. Each provider may define a
`selectable_models` list in `model_config.toml`; only IDs that are also returned by
that provider's live catalog are offered. Omitting the list exposes every model
compatible with the configured text-generation adapter.

Independent sealed calls use a separate simultaneous in-flight request cap for
each `provider:model`. Provider catalogs are used only when they explicitly
expose a concurrent-request field; RPM, TPM, and daily quotas are not concurrency.
Set `max_concurrent_requests` on a provider or use an exact override such as
`model_max_concurrent_requests = { "model-id" = 2 }`. If neither the provider nor
the configuration supplies this input control parameter, ENSEMBLE safely uses
one simultaneous request for that model. `ensemble-v07 doctor` prints the configured
fallback and model overrides.

### Codex subscription models (optional)

The `codex_subscription` provider uses the installed Codex CLI's **ChatGPT
sign-in**, not an OpenAI Platform API key. This follows the supported Codex App
Server interface. ENSEMBLE checks the account mode, discovers the account's live
model list, starts a fresh ephemeral thread for each sealed response, disables
tools/web search/MCP, and refuses unexpected local instruction files. Codex
subscription limits still apply; ENSEMBLE does not promise unlimited usage.

If you have already used `codex login` with ChatGPT, set
`[providers.codex].enabled = true` in `model_config.toml`. ENSEMBLE creates a
temporary, empty Codex home for each call and links only the local Codex
authentication file into it; it does not copy personal `AGENTS.md`, plugins or
MCP configuration. If your installation stores login state elsewhere, set
`codex_home` to a separately signed-in directory without those instructions or
tools. Never put tokens in `model_config.toml`. The existing
`ensemble-v07` command remains usable, while new recovery helpers now print
and execute `ensemble resume <meeting-path>`.

The checked-in local configuration exposes the live-discovered GLM text catalog
through provider ID `glm`, the OpenAI-compatible base URL
`https://open.bigmodel.cn/api/paas/v4`, and credential variable `GLM_API_KEY`.
No credential value is stored in the project configuration or meeting artifacts.

Terminal task input is decoded as UTF-8 first, with a strict GB18030 fallback for
Chinese SSH/terminal environments, and then normalized to Unicode NFC before it
is persisted as UTF-8 JSON. Invalid bytes are rejected rather than replaced.

For an already-created meeting, an operator can record a decision point and,
when configured, submit its scheduler email job with:

```bash
ensemble-v07 request-human --config ensemble.toml --meeting ./M-XXXXXXXX \
  --reason-code POLICY_NOT_CONFIGURED --summary "Human policy choice required"
```

The default `slurm` transport invokes `sbatch` with the per-meeting address and
`--mail-type=END,FAIL`; the submitted job performs no substantive work. The
Slurm job ID is stored as the durable notification receipt. No ballots, prompts,
model identity mappings, governance-private scoring data, or speech content enter
the notification job. The escalation address is stored under `human_private/`
and is unavailable to Representatives, Chair, Think Tank, and Audit contexts.
The optional `smtp` transport sends the same minimal operational alert used by
earlier builds.

The current milestone does **not** autonomously execute every remaining governance
branch or the complete Audit Conference state machine. The supported deliberation
path runs through final publication and the Chair debrief, but several transitions
still require Human policy decisions listed in
`docs/governance/10_open_questions/open_questions.md`. Those branches continue to
raise `PolicyNotConfiguredError`; the invocation layer pauses and escalates them
instead of selecting a convenient default.

`ensemble-v07 run-general` currently executes and durably records:

```text
random initial drafter
→ D0 initial draft
→ isolated position collection from every Representative
→ all-or-nothing publication of valid positions
→ amendment-window freeze
→ replayable random amendment docket
→ isolated co-sponsorship collection from every Representative
→ governance-private co-sponsorship freeze
→ auditable Chair type/scope/conflict/dependency processing
→ frozen-order sequential sealed ballots
→ `ceil(3N/4)` supermajority decisions
→ one all-Representative reason window and `>N/2` Protective Vote when needed
→ hash-linked Chair application of adopted amendments
→ the same frozen-window cycle for D0→D1, D1→D2, and D2→D3
→ sealed D3 yes/no ratification, with a required reason for every NO vote
→ trial atomic-item table and governance-private Drafting Alignment
→ representative status transition and unique Primary Drafter selection
→ Primary Drafter's independently frozen first detailed-clause draft C0
→ trial docket of C0 second-level numeric clauses
→ one isolated complete-C0 review by every ACTIVE Representative
→ proposals grouped by target clause and split/suspension motions frozen
→ sealed motion support using trial thresholds `ceil(N_ACTIVE/4)` and `ceil(N_ACTIVE/3)`
```

Re-running the command is idempotent at every fully frozen source artifact. A
ballot interrupted after one or more sealed votes is inspected by Chair on
resume: complete valid sealed votes are retained, while invalid and missing
votes are recollected without disclosing a partial tally.

Confirmed amendment conflicts use the conflict-set procedure automatically;
partially conflicting content is split into compatible binary-vote fragments
and exclusive multi-option choice sets. If Chair encounters a different
institutional rule conflict with no configured resolution, Chair opens a
structured Human consultation instead of inventing a default. The consultation
records the eligible roster size, each applicable threshold formula and the
derived minimum vote count. In an interactive terminal, enter `c` to ask Chair
one natural-language procedural question at a time; the explanation returns to
the same menu and does not count as a Human decision. Human may also request a
new Chair classification ruling when apparent mutual exclusion may only be
compatible overlap or dependency. Resolve a consultation non-interactively from
any directory with the exact option listed in its public artifact:

```bash
ensemble-v07 resolve-consultation \
  --meeting /absolute/path/to/M-XXXXXXXX \
  --issue-id HC-W001-STEP001-CONFLICT \
  --decision "先按当前文本表决第一项" \
  --rationale "Apply random-order sequential processing to this conflict set only" \
  --scope THIS_CONSULTATION_ONLY

ensemble-v07 open /absolute/path/to/M-XXXXXXXX
```

### 在会议中途更换模型

交互式运行的任务表底部会常驻显示快捷键：`Ctrl+C` 安全中断，`Ctrl+R` 请求
替换模型。按下 `Ctrl+R` 后，当前在途调用会先完成并落盘，程序在安全边界暂停，
依次让 Human 选择“单个参与者”或“整个模型”、选择对象、选择实时发现的目标模型，
再输入替换原因并自动恢复。非交互终端仍可使用下面的命令行形式。

如果某个参与者的供应商不可用、持续超时或需要改用备用模型，可以在会议暂停
或当前进程安全退出后，只替换该参与者未来的调用。会议身份、人格、已落盘的
响应和既有票据不会被改写；替换记录同时写入治理私有审计链和
`public/model_replacements/`。目标模型必须属于会议配置中已启用的供应商：

```bash
ensemble-v07 replace-model \
  --meeting /absolute/path/to/M-XXXXXXXX \
  --participant R-9DEBEF \
  --model deepseek:deepseek-flash \
  --reason "原模型连续输出格式错误，恢复时改用备用模型"

# 然后用统一入口恢复；系统从会议 manifest 自动选择执行器
ensemble-v07 open /absolute/path/to/M-XXXXXXXX
# 旧 run-report/run-general 命令仍作为兼容别名保留
```

也可以指定 `CHAIR` 或 `RESEARCH_DESK`。替换命令使用会议锁，因此不会与正在
运行的会议并发写入；如果原进程仍在运行，应先按 Ctrl-C，让已落盘进度保持不变，
再执行替换。若要把仍在使用某个旧运行时的全部参与者一起切换，可使用
`--from-model`，例如：

```bash
ensemble-v07 replace-model \
  --meeting /absolute/path/to/M-XXXXXXXX \
  --from-model gemini:gemini-flash-latest \
  --model deepseek:deepseek-flash \
  --reason "Gemini 供应商在本次会议中持续不可用"
```

每次替换都只对之后尚未完成的调用生效。

The Human ruling is immutable and scoped to that consultation; it does not
silently become a global policy. After successful D3 ratification, execution
continues through the trial Drafting Alignment and detailed-clause review rules.
Each trial activation is recorded in immutable meeting artifacts with review
status `PENDING_THINK_TANK_REVIEW`.

By default ENSEMBLE sets **no client-side per-call output-token limit**; the
provider's own model limit applies. This is an operational input parameter, not
measured token consumption or a governance threshold. To opt into ENSEMBLE
budgets, set `governance.provider_output_token_limit` to a positive fallback.
ENSEMBLE then targets `governance.provider_output_context_fraction` of each
model's live-advertised input context, respects any smaller advertised output
hard limit, and uses the configured token value when catalog context metadata is
missing. Opt-in model-specific budgets and their basis are frozen in
`governance_private/model_token_budgets.json` and reused when the meeting
resumes. Set a limit for one run with `--max-output-tokens`.

Input-context safety is independent of that output policy and is enabled by
default. Before every provider call, ENSEMBLE compares a conservative input
estimate (`ceil(UTF-8 bytes / 3) + 512` framing tokens) with a meeting-frozen
acceptance ceiling. The ceiling is
`governance.provider_input_context_fraction` (default `80%`) of a model's
advertised input capacity, or `governance.provider_input_token_limit_fallback`
(default `262144` estimated input tokens) when discovery reports no capacity.
For provider catalogs that omit capacity metadata, a vendor-verified per-model
value can be declared as `providers.<id>.model_input_token_limits`; the same
safety fraction is applied to that value before the generic fallback is used.
The immutable per-model record is
`governance_private/model_input_context_budgets.json`. When this metadata is
added after an older meeting froze a fallback, ENSEMBLE preserves that original
record and writes the auditable upgrade separately to the base
`governance_private/model_input_context_budget_upgrades.json` record or a later
increment such as `model_input_context_budget_upgrades_v2.json`. A request still above
the ceiling after bounded context construction pauses as
`MODEL_INPUT_CONTEXT_BUDGET_EXCEEDED` before any provider call; ENSEMBLE never
silently truncates constitutional rules, the current motion, or a ballot.

Released Research Round snapshots remain the complete authoritative public
evidence database. A single Representative call no longer concatenates every
full snapshot. It receives a deterministic relevance view containing at most
16 active evidence packets and at most 64,000 Unicode characters, with concise
supporting, contradictory, limitation, alternative, source, and unresolved
question fields. The view explicitly reports the number omitted; omission from
the injected view is not evidence of absence and does not remove the public
packet, archived source, or downloadable literature bundle. Snapshot reads used
to derive the view and the exact derived fragment hash are separately recorded
in `governance_private/representative_file_access.jsonl`.

Token telemetry is a measured API observable, separate from that configured
budget. ENSEMBLE defines `cache_hit_rate = cached_prompt_tokens / prompt_tokens`
for a completed request. Counts come from the provider response and are shown as
“未报告” when absent; missing cache counts are never coerced to zero. DeepSeek's
top-level cache fields, OpenAI/GLM `prompt_tokens_details.cached_tokens`, and
Gemini `cachedContentTokenCount` are normalized to the same record shape.
When a pre-telemetry meeting resumes, retained provider exchanges are backfilled
without repeating the model calls; one summary event records the number created.

If a provider reaches either an explicitly configured limit or its own output
limit on reasoning before producing public text, the meeting pauses with
`MODEL_OUTPUT_LIMIT_REACHED`; it does not silently retry with a different
limit. This provider-output policy is separate from Research Desk claim-level
QC, which may retain a usable packet at a lower evidence status. Other
schema-invalid reachable-model output still pauses because the correction/retry
policy remains open.

Provider HTTP timeouts are independent input controls. Every model provider has a
maximum of **1,200 seconds (20 minutes) per response attempt**; configuration
validation rejects a larger value, while a provider may use a shorter timeout.
This limit does not include retry backoff and is not a token budget. An initial
attempt plus three retries remains the configured availability policy; every
scheduled retry is shown in the terminal and appended as
`PROVIDER_RETRY_SCHEDULED` without exposing prompt or response content. Transient
failures use a configured exponential delay of **30, 60, and 120 seconds**. A
provider `Retry-After` value takes precedence when it requires a longer wait,
preventing immediate retry storms after HTTP 429.

Detailed-clause first-round ballots are split into immutable fragments of at most
**10 option sets per model call**. Completed fragments survive interruption and
are merged only after one voter has covered the complete frozen docket. New
fragments use `reason=null` except for Type I `OPPOSE`, whose single decisive
reason is capped at **240 Unicode characters**. Complete ballots saved under the
older schema remain valid and are not repeated during recovery.

After procedural certification and the first independent Librarian review,
Chair now produces a task-adaptive execution/handoff brief. Each Librarian then
reviews that brief independently. The current conservative aggregation policy
mechanically bundles those reviews without Chair synthesis; it does not settle
the still-open permanent Think Tank aggregation rule. A frozen
`public/final/human_review_packet.json` indexes the certified resolution, Chair
brief, both Think Tank review layers, and their SHA-256 provenance. Completion
then assembles `public/final/final_report.md` with the certified result and Chair
handoff as the main body and both independent Think Tank review layers as
advisory appendices. Chair performs a presentation-only readability check; a
failed check pauses publication without changing the certified result. After a
pass, ENSEMBLE embeds an available CJK font and freezes
`public/final/final_report.pdf` plus a SHA-256 publication manifest. Completion
ends at `HANDOFF_READY`; no ACCEPT/REJECT/EXPERIMENTAL/DEFER choice is inferred
for the Human. ENSEMBLE bundles HarmonyOS Sans SC Regular, Medium, and Bold
for Chinese/Latin PDF text and heading hierarchy. Set `ENSEMBLE_CJK_FONT` to
another embeddable TTF/TTC file only when an override is needed. Set
`ENSEMBLE_SYMBOL_FONT` only when the host also lacks DejaVu Sans or ReportLab's
bundled Vera fallback for Latin, Greek, arrows, and mathematical symbols.
The bundled files are unmodified copies from the
[official HarmonyOS design resources](https://developer.huawei.com/consumer/cn/design/resource-V1/).
HarmonyOS Sans uses Huawei's own font license, not the project's license.
The full license accompanies the files in `project_ensemble/assets/fonts/LICENSE.txt`;
see also [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md). Set
`ENSEMBLE_CJK_HEADING_FONT` to override the bundled bold heading font.

When released Research Desk packets exist, the final Markdown and PDF include a
numeric meeting-evidence index and a deduplicated reference list. Citations are
attached separately to supporting, contradictory, limitation, and alternative
findings, and resolve back to packet/source metadata and the downloadable
literature bundle. Sources are deduplicated by DOI, or by normalized URL when no
DOI exists. ENSEMBLE does not infer from wording similarity that a paper endorses
a normative resolution clause; only source links already validated inside a
released evidence packet enter the citation apparatus.

After final publication, every Representative receives one isolated sealed
post-meeting submission. All Representatives explicitly submit whether they
request Audit Conference intervention; CONSULTATIVE Representatives may also
preserve one minority report. The orchestrator freezes minority material first
and audit petitions second, and never treats a missing/invalid submission as
“no request.” This stage uses a hash-traceable derived view of the final report:
the certified body, Think Tank review appendices, and provenance appendix remain
present, while the already published literature catalogue is omitted from the
model prompt and remains available in the complete Markdown/PDF and literature
bundle. Completed sealed submissions are persisted individually before the batch
barrier, so interruption or another model's failure does not discard them. Chair
then produces a Human-only accountability report covering
the meeting, principal problems, evidence-backed performance by Representative
ID, and the exact audit-request status. Chair never receives the private
ID-to-model/persona registry; the orchestrator appends that mapping afterward in
a clearly marked Human-only mechanical appendix. Petitions remain advisory and
do not automatically reopen the resolution or convene an audit.

The terminal shows a separate Chair debrief of at most six short lines; the full
report remains frozen under `human_private/final/`. Human-facing entry points are
also created at the meeting root: `FINAL_REPORT.pdf`, `FINAL_REPORT.md`,
`CHAIR_BRIEF.txt`, `CHAIR_DEBRIEF.md`, and `MEETING_RESULTS.md`. These are stable
relative links or an index; the canonical frozen artifacts remain in their
compartments.

Final-publication PDF template V2 uses left-aligned CJK layout, joins Markdown
soft wraps without inserting spaces between Chinese lines, and removes invisible
format characters that some embedded fonts rendered as boxes. Existing V1
publications remain immutable; the next resume creates `final_report_v2.pdf`
beside V1 and retargets only the root-level `FINAL_REPORT.pdf` convenience link.

Every file included in a Representative context is authorized before reading and
recorded in the hash-chained
`governance_private/representative_file_access.jsonl` ledger with stage, path,
byte count, and content SHA-256. Attempts to include another Representative's
private state or a non-public compartment are recorded and denied before content
release. Full public evidence snapshots read only as derivation sources are
distinguished from the bounded fragment actually exposed; the latter records
selected packet IDs, size, and content hash. With current provider APIs this
measures orchestrator context exposure, not autonomous model browsing; any
future model file tool must use the same gate.

Live progress is written to standard error so the final JSON result on standard
output remains machine-readable. Use `ensemble-v07 run-general --no-progress ...` to
suppress the live task table. When standard error is redirected, ENSEMBLE writes
compact call-start/completion records and omits intermediate stream events and ANSI
cursor controls. Position content is displayed only after every
Representative has submitted and the public position window has been frozen;
partial position sets and all sealed ballot content remain undisclosed.

## Repository map

```text
Project_ENSEMBLE/
├── AGENTS.md                  # Codex/agent implementation rules
├── CODEX_HANDOFF.md           # next-work briefing
├── TODO.md                    # policy-safe engineering backlog
├── pyproject.toml
├── examples/
├── docs/
│   ├── architecture/
│   └── governance/            # v0.7.0 institutional rules
├── src/project_ensemble/
│   ├── audit/
│   ├── governance_private/
│   ├── orchestration/
│   ├── providers/
│   ├── research/
│   ├── runtime/
│   └── storage/
└── tests/
```

## API notes

Provider endpoints are configuration, not hard-coded institutional assumptions. The
example config records the currently supported endpoint defaults; model discovery is
performed live and the Human selects the meeting roster, so discovered model IDs and
availability always take precedence over this document.

## Security model

The implementation treats governance visibility as a software boundary, not a prompt request. A Representative context should never be assembled by concatenating the entire repository. See `docs/architecture/security_and_visibility.md`.

## License

No open-source license has been selected yet. Add one before treating the repository as openly licensed.
