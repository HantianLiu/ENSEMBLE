# TODO

## 科学冻结后的独立人类可读性重写层

- [ ] 在最终科学与引文核查冻结后，以原文为只读输入另存一份读者稿；按章节重写句法、段落、路标与冗余，不得重新研究或改变主张、范围、确定性、数值、公式、引文和异议。原始冻结 Markdown 始终保留。
- [ ] 对每个改写章节做原文—读者稿差异核验：先机械核对引文、数字、公式与标题，再由独立模型逐项核对语义不变量；任何无法确认的章节回退原文，并记录原因。经核验的读者稿才可成为默认 HTML/PDF 来源。该步骤须有明确的模型调用预算、失败回退和可恢复落盘点。
- [x] 已把文字锚定的专业度、分段、活泼性、论证路标及信息分层契约接入文献报告和学术重绘的读者正文生成／局部修订入口；规划、证据、审阅和审计中间产物不套用。HTML 支持可展开的辅助 `[注]`，但这不等于完成独立重写层。

## 连续设问的撤回与返回

- [x] 快速文献调研的逐条模块范围咨询：在下一条可撤回上一条，整组提交前可撤回最后一条；原回答保留在审计记录，撤回和重答追加为新记录。
- [ ] 逐个审查其余连续人类咨询与初始化向导：凡上一条尚未触发不可逆的模型调用或冻结结果，应提供返回上一级；已冻结的回答需采用追加撤回／替代记录并明确后续影响，不能直接覆盖或删除。为每种菜单加入中途退出、撤回、恢复和最终确认测试。

## 文献综述写作流程重构（设计已记录，未实施）

- [ ] 下次实现讨论从 [NEXT_VERSION_NOTES.md](NEXT_VERSION_NOTES.md)
      的完整设计纪要接续：模块 Builder 提纲与三稿表决、专职主笔、智库长科学审阅、
      滚动术语表、临时引文 ID 与正式编号、最终通读和人类再审入口。当前会议及代码
      不因这条 TODO 改变。

## Scholarly rendering science-ballot follow-up

- [x] Before each final scholarly science ballot, allow each reviewer up to four
      Research Desk checks per section across revision cycles. Freeze each decision
      and result for recovery, reuse duplicate claims without charging another
      check, and admit new scientific corrections only with at least partial
      support. Existing meetings retain their original final-ballot policy.

## Blocked by governance decisions

- [x] Adopt a mechanical Atomic Drafting Item granularity as a trial pending Think Tank review.
- [x] Use a replayable random order for each frozen amendment window.
- [x] Adopt `ceil(N_ACTIVE/4)` as the trial Clause Split support threshold,
      pending Think Tank review.
- [x] Adopt `ceil(N_ACTIVE/3)` as the trial Motion to Suspend support threshold,
      pending Think Tank review.
- [x] Define 3+ way Primary Drafter tie procedure.
- [x] Define malformed/schema-invalid reachable-model output policy for ballots and
      independently recoverable submissions; implementation remains next-version work.
- [x] Define ballot atomicity after a mid-ballot pause: retain complete votes and re-collect invalid/missing votes.
- [x] Decide that an all-PASS audit round ends discussion early when no finding,
      question, material request, or unresolved pivotal missing submission remains.
- [x] Define a Human-reopened audit as a fresh review of the complete meeting.
- [x] Finalize Think Tank multi-Librarian review aggregation semantics: independent,
      Human-readable audit appendices; mechanical bundling only; no substantive synthesis.

## Adopted next-version governance package — not implemented

> These decisions were recorded on 2026-09-21. They are TODO requirements, not
> current runtime policy. They must apply only to newly initialized meetings after
> implementation, schema/version pinning, and tests. Existing meetings retain their
> frozen governance and recovery semantics.

- [ ] Implement dynamic, per-item missing-ballot pivotality. Freeze the eligible voter
      set at runtime; never hard-code 12. For a binary threshold `T`, pass when
      `YES >= T`, fail when `YES + MISSING < T`, and pause only when
      `YES < T <= YES + MISSING`. For ranked/multi-option ballots, pause only when a
      valid completion of missing ballots can change the winner, advancement set, tie
      status, or pass/fail result. The abandoned “missing <= 1/4” rule must not return.
- [ ] Keep all eligible voters in the denominator even when their provider fails.
      Missing votes are calculated independently for every ballot item. Independent
      items continue; only a pivotal item and its dependency chain pause. Human may,
      after repeated risk warnings, irrevocably authorize pivotal missing ballots as
      blank abstentions. A later response cannot reopen that frozen result.
- [ ] Add two-level structured-response recovery. Preserve valid items from a parseable
      batch and resubmit each invalid item at most 3 times. Resubmit a wholly
      unparseable envelope at most 3 times without consuming item retries; after that,
      collect items separately, each with its own 3-attempt limit. Valid submissions
      stay sealed. Non-voting contributions that remain invalid become
      `EMPTY_SUBMISSION` unless the sole required artifact is structurally essential.
- [ ] Do not allow voluntary abstention in ordinary proposal ballots. A Representative
      must vote YES/NO; transport failure or invalid output is a missing vote, not a
      voluntary abstention.
- [ ] Add deterministic continuation checks returning `CONTINUE`, `BLOCKED`, or
      `INDETERMINATE`. Deterministic CONTINUE/BLOCKED binds Chair. Chair rules only an
      INDETERMINATE case with reasons; any one valid Representative objection triggers
      an absolute-majority vote. Every eligible Representative returns `NO_OBJECTION`
      or a reasoned objection; an unrecovered nonresponse defaults to
      `NO_OBJECTION_BY_NONRESPONSE`. Publish anonymized reasons only after finalization.
- [ ] Add an optional Human-designated backup Chair at initialization. Chair provider
      transport calls receive 5 retries after the initial call (6 total calls). A
      task-scoped no-confidence event transfers only the failed task to backup Chair;
      the original Chair returns for the next task. Triggers include exhausted
      transport retries, original schema-invalid output plus 2 failed lossless repairs,
      a third state-integrity/frozen-record violation, or failure of the third permitted
      dependency-correction version.
- [ ] Give backup Chair a clean-room, independent quota. It receives frozen task inputs,
      applicable rules, and public state, but no failed Chair output, validator error,
      repair prompt, objection reason, vote, or hidden reasoning. If backup also fails,
      save and exit for Human. Representatives see only `CHAIR` and must not learn the
      underlying model, handoff, or failure. Human and the restricted audit layer retain
      the identity/handoff record.
- [ ] On recovery after both Chairs fail, let Human select a new Chair, the original
      Chair, or backup Chair and hold a natural-language problem-solving dialogue. The
      completed dialogue is then atomically published to all agents as Human's final
      interpretation for that case. It is case-specific unless audit nominates it as a
      precedent and Human explicitly confirms it. A confirmed precedent affects only
      newly initialized meetings.
- [ ] Implement versioned late dependency correction. Affected eligible voters decide
      by absolute majority; every NO requires a reason. Chair reads sealed opposition
      reasons after voting. A failed version must be redrafted; a passed version may be
      redrafted voluntarily. Main Chair and backup Chair each receive at most 3 versions.
      Related independent items continue while the affected dependency chain pauses.
- [x] Implement the 3+-way Primary Drafter tie rule: full eligible set submits a sealed
      ranking with no abstention; first preferences select the top two; a tie for the
      second advancement seat receives a tied-candidate runoff; the final top two use a
      sealed binary ballot; Chair casts the deciding vote if still tied. Do not expose
      Drafting Alignment scores, ranks, or selection rationale.
- [x] Add a generic tie fallback for multi-option ballots without a specific rule:
      tied leaders enter a runoff; a remaining tie is resolved by a public Chair deciding
      vote with a concise public reason. Chair's underlying model remains hidden.
- [x] Implement the Type II genuinely-new-option reconstruction rule: mechanical Chair
      qualification, one proposer revision, absolute-majority exclusion objection,
      qualified-only public release, fresh A/B reballot when no new option survives,
      and a non-recursive Type III set with status quo when at least one survives.
- [ ] End an audit immediately as `AUDIT_CLOSED_ALL_PASS` only when every eligible audit
      member submitted a valid PASS and no findings, questions, material requests, or
      pivotal missing audit submissions remain. Non-material advisory findings may be
      retained in an appendix without blocking closure. Any unresolved material factual,
      procedural, or outcome-changing finding blocks closure.
- [ ] Only Human may reopen a closed audit. Reopening reviews the entire meeting but does
      not revoke, freeze, overwrite, or hide the original publication. Audit only reports
      material problems and exposes a Human-invoked handoff API; it never edits the
      document or automatically creates a correction meeting.
- [ ] Route all material findings from one audit into one Human-created correction
      meeting. Human reselects Representatives, Chair, backup Chair, models, and reasoning
      effort. Inherit the original final document, evidence bundle, original/new audit
      records, and triggering evidence as read-only inputs. Once correction starts, stop
      auditing the old version and freeze the meeting inputs; no later material may be
      added. The correction meeting has ordinary full revision authority, including
      changes beyond the triggering docket.
- [ ] A correction result does not receive automatic audit, but Human may start one. The
      accepted correction becomes the default current version; every historical version
      retains a permanent stable identifier. Publish an itemized change record with old
      and new text, reason, linked finding, decision, vote threshold/result, and anonymous
      reservations; no special label is required for changes beyond the audit docket.
- [ ] Before freezing a correction meeting, Research Desk must freshness-check inherited
      evidence. Refresh `STALE` material; retain unrefreshable material as
      `STALE_UNRESOLVED`, which cannot alone support a factual conclusion. A critical fact
      supported only by such evidence becomes `UNRESOLVED`, and a wholly dependent
      amendment cannot proceed unless Human explicitly authorizes a normal-threshold vote
      after repeated warnings. The authorization is item- and meeting-specific and the
      unresolved status remains reader-visible.
- [ ] Enforce Representative sealed-file access. A successful read of any protected
      fragment counts as one violation; a blocked attempt only produces a private warning.
      Before result freeze, Chair chooses the affected ballot scope and uses the widest
      reasonable scope when uncertain. Secretly solicit but discard the disqualified
      Representative's affected votes, remove that Representative entirely from the
      affected denominator, and reveal neither the exclusion nor its history to that
      Representative or peers. Frozen historical results are changed only through a
      Human-reopened audit.
- [ ] Within one meeting, 3 confirmed successful protected-file violations permanently
      destroy the Representative runtime: terminate future calls, leave the seat vacant,
      and recalculate future eligible sets. Do not replace the seat or carry the violation
      count across meetings. Print the destruction event only to the Human terminal and
      restricted audit log. Preserve access evidence; “destroy” never deletes records.
- [ ] For duplicate original/retry calls, freeze the first complete response that passes
      all structural and substantive validity checks. Discard later response content;
      retain only provider usage needed for telemetry. If Human replaces a model, all old
      in-flight requests become invalid at the confirmation boundary and can never win.
- [ ] Model replacement preserves Representative ID, office, valid sealed submissions,
      frozen public statements, and own allowed history. The replacement sees no old
      unfinished output, hidden reasoning, validator error, or repair prompt. It
      automatically restarts only unfinished tasks. A valid sealed vote submitted before
      replacement remains valid and cannot be changed by replacement.
- [ ] Keep sealed content out of the terminal before release, while leaving meeting files
      readable to Human. Human is not an audited meeting subject and retains final task
      interpretation authority; do not add a Human file-read audit or a mid-meeting task
      mutation interface.
- [x] Pin governance version, configuration snapshot, and hashes at initialization.
      Recovery checks the pinned software and governance digest. A v0.6 meeting can
      be copied into a separate v0.7.0 directory only after an integrity preflight;
      the original is never migrated in place. The import retains frozen artifacts
      as history and applies v0.7.0 rules to later unfinished steps.
- [ ] Provide an explicit new-version successor route for cases where the Human
      wants to inherit only frozen documents as reference material, excluding old
      ballots and in-flight procedural state.
- [ ] Persist root-level UTF-8 `original_prompt.txt`. A successor also stores the direct
      parent's prompt as `parent_prompt.txt` and maintains append-only
      `meeting_lineage.json` with ancestor meeting IDs, Human-confirmed natural-language
      titles, meeting types, parent links, and inherited-material categories. Chair drafts
      the title from the original prompt; Human confirms or edits it before registry write.
- [ ] On recovery, discard and regenerate all unfrozen staging artifacts. Reuse an existing
      frozen path only when its content hash is identical; a conflicting frozen artifact
      must pause rather than be overwritten. Random selections need only record/freeze the
      selected outcome; no random seed or replay algorithm is required.

## Engineering work that is safe now

- [ ] Next version: make every live task table resolve the effective runtime through
      the audited model-replacement ledger. The immutable initialization registry
      remains historical provenance, but the UI must label future calls with the
      currently active provider/model and may additionally mark it as replaced.
- [ ] GitHub release hardening: provide bilingual Chinese/English prompts and runtime
      UI, complete dependency and installation metadata, credential setup examples,
      and a documented `model_config` file that maps providers/models to environment
      variable names and capability controls. Keep secrets outside the repository.
- [x] Package v0.7.0 under the independent `project-ensemble-v07` distribution and
      `ensemble-v07` command; ship governance data and configuration templates in the
      wheel, add environment/key-file credential parsing, security/contribution notes,
      and freeze per-meeting config snapshots, hashes, software version, original
      prompt, and lineage. Full bilingual prompt/UI coverage remains in the item above.

- [ ] Next version: replace the Research Desk question/answer progress view with a
      fixed-slot **Representative × question** matrix after the question-submission
      barrier closes. Rows are Representative identities; columns are `Q1..Qmax`,
      where `Qmax` is the frozen per-Representative limit for that stage. Every row
      reserves the same number of task slots; a Representative that submits fewer
      questions leaves explicit `NO_SUBMISSION`/abstention cells instead of changing
      the table shape. Keep question submission and Research Desk answering as visibly
      separate phases. Cell states are: queued = gray, Research Desk processing =
      white, evidence response complete = green, and returned to the requester for
      rewrite = yellow. A terminally abandoned invalid question must be visibly marked
      `ABANDONED` and excluded from the evidence packet count, without making the
      whole matrix or meeting appear failed. Preserve a non-color textual symbol/status
      for accessibility and redirected terminals.
- [ ] Next version: change the per-question rewrite control limit from the current
      **2 rewrite attempts** to **3 rewrite rounds**. A Research Desk rejection returns
      only that cell to its originating Representative; other queued questions continue.
      The revised question re-enters the Research Desk queue at the tail. If the claim
      is still not concrete and externally verifiable after round 3, abandon only that
      question and continue the meeting. This is a per-item recovery limit, not a
      provider-network retry count and not a meeting-wide failure threshold.
- [ ] Next version: define and implement an explicit failure taxonomy instead of
      treating every invalid contribution as fatal. At minimum distinguish
      `STRUCTURAL_FATAL` (state/integrity/governance damage; pause or terminate),
      `PARTICIPANT_ABSTENTION_EQUIVALENT` (invalid or repeatedly unrepairable speech,
      vote, amendment, or question that may be recorded as abstention/abandonment),
      `ITEM_QC_EXCLUDED` (one evidence or drafting item is excluded while the batch
      proceeds), and `TRANSIENT_RETRYABLE` (transport/provider availability). Define
      stage-specific consequences rather than applying one global fallback. Do not
      enable this policy for production meetings until Constitution/governance review,
      recovery tests, and quorum/threshold impact tests are complete.
- [ ] The future failure-policy audit record must retain the item ID, requester,
      stage, original submission, every revision, rejection/failure reason, attempt
      count, final disposition, continuation decision, provider-exchange references,
      and the applicable prompt template version/hash. Add an audit diagnostic that
      flags repeated cross-model failures under the same prompt as a possible prompt or
      schema-design defect; this is diagnostic evidence, not proof that the model was
      at fault. Human-readable audit output should state plainly which isolated items
      were abandoned and why the remainder of the meeting remained valid.
- [ ] Next version: introduce typed, independently budgeted context injection with
      hard growth limits, a 10,000-token per-item acceptance limit, large-fragment
      review, canonical byte-stable cache prefixes, and versioned compatibility for
      running meetings. Interim v0.7 safeguards now bound the automatically injected
      Research evidence view and preflight the complete provider input, but do not yet
      implement the full typed per-context budget architecture. Design notes:
      `NEXT_VERSION_NOTES.md`.
- [x] Persist meeting manifests and initial session configuration in compartmentalized immutable files.
- [x] Allow an audited, future-only runtime replacement for one participant
      (`R-...`, `CHAIR`, or `RESEARCH_DESK`) through `ensemble-v07 replace-model`.
      The immutable identity manifest and historical exchanges remain unchanged;
      a paused/safely interrupted meeting resumes missing work with the latest
      replacement. Resolve replacement-model input/output budgets under the
      meeting's existing safety policy without rewriting frozen budget snapshots.
- [x] Add interactive model replacement controls: keep `Ctrl+C`/`Ctrl+R` visible
      in the live task table; handle `Ctrl+R` only at a durable provider-call
      boundary, then offer single-participant versus whole-model selection,
      live model discovery, an audited rationale, and automatic resume. Keep
      the terminal listener reference-counted for parallel calls and restore
      terminal modes before Human prompts.
- [ ] Reconstruct the complete meeting state from the event log.
- [ ] Add SQLite backend as an optional index; keep immutable files authoritative.
- [ ] Add richer live capability normalization after provider model discovery.
- [x] Add token/cost telemetry without exposing metrics to Representatives.
- [x] Freeze one shared Representative reasoning-effort control and separate Chair control;
      map normalized levels only where the selected provider/model supports them.
- [x] Add the optional shared Research Desk core: bounded-claim normalization,
      adversarial OpenAlex retrieval, structured evidence packets, freshness cache,
      audit-private traces, and an explicit `research-claim` command.
- [ ] Research Desk original-text reading: replace fixed-character excerpt windows
      with document-aware units. For structured HTML, preserve a complete section
      or provision (including subparagraphs); for PDFs, use page/heading/paragraph
      boundaries and retain a page locator. When no structure can be recovered,
      cut at sentence/paragraph boundaries with bounded overlap. Never end a
      model-visible excerpt in the middle of a substantive sentence. If one unit
      exceeds its token budget, split it into numbered continuation chunks and
      explicitly mark the unit incomplete until all required chunks are available;
      `READABLE_EXCERPT` must not imply that the whole provision or document was read.
      Audit the original span, extracted span, omissions, source URL, version/date,
      and section/page locator. Regression cases: naturally discovered eCFR
      § 610.1 must deliver the entire 649-character provision rather than stopping
      after “Each ap”; a public Pfizer earnings PDF must retain the relevant
      revenue sentence and page number. Keep retrieval and model context bounded.
- [ ] Research Desk alternative-source QC: a blocked or dead original may trigger
      bounded title/DOI/identifier-based discovery, but a thematically related
      page is not an alternate copy. Rank primary/official or publisher-hosted
      originals first; require document identity, edition/date, and passage-level
      checks before labelling a candidate as an alternate edition or relying on
      it for document-specific claims. Keep unrelated results only as separately
      labelled background leads, or exclude them. Audit every screening reason
      and preserve the original access failure. Regression case: a dead third-party
      Pfizer blog must not turn a generic Wikipedia page into an apparent replacement
      for that blog or displace the naturally discovered Pfizer public PDF.
- [x] Implement the complete `literature_review` state machine: inherited or empty origin,
      four-office outline planning, module-serial evidence questions, bounded evidence dossiers,
      module drafting/review/confirmation, contested-view preservation, whole-report synthesis,
      unconditional publication, Think Tank factual-opposition filtering, fact-neutral readability
      patches, and visible Markdown/PDF/literature-bundle outputs.
- [x] Add a mandatory Human review gate after research decomposition, Chair clustering, and
      Representative outline review but before any formal module research. Record an immutable
      approval or a rejection rationale; a rejection starts a fresh versioned planning cycle and
      passes the Human objections into the new decomposition, clustering, and review prompts.
- [ ] Next version: add a bounded **initial reconnaissance search window** immediately after
      project decomposition and before the research modules are frozen. Each planning-panel model
      may submit at most **16 independent search queries**. The window should explicitly encourage
      broad discovery rather than premature claim verification: search across terminology variants,
      adjacent fields, landmark papers, systematic reviews, major narrative reviews, consensus or
      guideline documents, and competing interpretations. Review literature should be actively
      preferred as a map of the field, without treating a review as automatically authoritative.
      Results should be stored as a shared, provenance-linked reconnaissance dossier visible to the
      decomposition panel and Human, then used to narrow oversized modules, identify missing
      subquestions, and select bounded source sets. This phase is exploratory: it must not silently
      promote search hits to verified evidence, must not replace the later adversarial Research Desk
      QC, and must record query text, requester, returned sources, deduplication, and search limits.
      The 16-query limit is per model and per reconnaissance window; retries and provider failures
      must not create unbounded extra searches. Do not connect this gate to the current production
      workflow until recovery, rate-limit, and provenance tests are complete.
- [ ] Later Research Desk optimization: after claim normalization and before any
      OpenAlex/Tavily retrieval, determine whether the meeting-local public evidence
      database already satisfies the research need. This is broader than the current
      exact-fingerprint freshness cache and must produce one audited disposition:
      `EXACT_REUSE`, `SEMANTICALLY_SATISFIED`, `PARTIALLY_SATISFIED`, or
      `NOT_SATISFIED`. Full reuse requires compatible verification question, time,
      geography, subject, scope, freshness, invalidation state, coverage of every
      requested factual sub-item, and a completed adversarial/counter-search attempt.
      Partial coverage should retrieve only the recorded gaps. `UNRESOLVED` may still
      satisfy a request when the fresh packet faithfully establishes that the evidence
      remains unresolved; satisfaction must never be interpreted as claim truth.
      Persist candidate packet IDs, field-level coverage, uncovered gaps, and the final
      disposition. When uncertain, choose partial or full retrieval rather than unsafe
      semantic reuse. Do not connect this gate to the current production state machine
      until its false-reuse tests and recovery compatibility are complete.
      The literature-report workflow now has its own bounded, model-assessed cache-only coverage
      gate; this TODO remains for the general-purpose Research Desk and stricter field-level reuse.
- [x] Parallelize independent deduplicated Research Desk claim groups with a bounded,
      resumable pipeline. Apply separate concurrency/rate gates to the Research Desk
      model, OpenAlex, Tavily, and document downloads rather than treating model
      concurrency as a universal limit; account for Tavily's four adversarial queries
      per claim. First remove per-call backend success/failure data from shared retriever
      instance state, and defer literature-bundle manifest/ZIP rebuilding until the
      round release barrier. Persist each validated group privately as it completes,
      isolate claim-level QC failures, resume only missing or invalid groups, and emit
      final resolutions/evidence snapshots in the frozen group order without exposing
      partial Research Round results.
- [ ] Add the stage-native request relay that lets a model pause an in-progress
      Proposal/Challenge/Veto turn, request exactly one Research Desk claim, receive
      the frozen evidence packet, and resume without giving the model direct network access.
- [ ] Add explicit provenance graph storage.
- [x] Add a required terminal setup UI with live provider/model discovery.
- [x] Add durable, deduplicated email escalation for failure/human decision points.
- [x] Use scheduler-managed Slurm notification jobs by default while allowing
      notification configuration to be omitted during the current milestone.
- [x] Display live phases, model-call activity, and publicly releasable speech in
      interactive runs without leaking partial sealed submissions.
- [x] Replace append-only progress output for fixed-size work windows
      (for example ballots, Research Desk claims, and Think Tank reviews) with a
      stable two-column TUI task table. List every task as one persistent row: pending
      tasks are gray, running tasks are white or optionally pulse white/gray, completed
      tasks are green, and failed tasks are red. Put the bounded status/detail summary
      in the second column and show completed/total counts as the progress indicator,
      so long model output does not continuously displace the task overview. Preserve
      full durable logs, sealed-window non-disclosure, a non-animated accessibility
      mode, and compact start/completion records without streamed fragments when
      stdout/stderr is not an interactive TTY. Provider streaming remains an internal
      liveness signal only; it never exposes partial reasoning or response text.
- [x] Group scholarly-rendering progress by current section, then writing,
      science review and citation review, with the active revision cycle and
      review round nested below its stage. Keep the existing per-model task
      table beneath this hierarchy; resumed sections use the same display.
- [x] Add the private provider-invocation boundary with durable exchanges,
      retry/pause escalation, and hidden-reasoning removal.
- [ ] Implement the complete deliberation and audit state machines after their
      unresolved governance branches are configured or decided.
- [ ] Add CLI for executing a dry-run meeting with fake providers.
- [x] Run the deliberation general-principle preparation path through D0,
      all-Representative positions, amendment-window freeze, randomized docket,
      and sealed co-sponsorship freeze.
- [x] Implement Chair document/conflict processing before opening sequential ballots.
- [x] Implement D0→D3 amendment windows, sequential sealed binary ballots,
      Protective Vote, traceable Chair draft application, and final ratification.
- [x] Implement trial atomic-item scoring, status transition, Primary Drafter
      selection, and the independently frozen C0 detailed-clause draft.
- [x] Implement the trial second-level C0 clause docket, one isolated review per
      ACTIVE Representative, proposal grouping, and split/suspension support freeze.
- [x] Implement the Type I proposer retain/revise window and protective-majority
      second ballot, including terminal rejection and interruption-safe recovery.
- [x] Implement the final Chair execution/handoff brief, independent Librarian
      execution reviews, mechanically bundled Human review packet, and
      interruption-safe recovery through `HANDOFF_READY`.
- [x] Add the final Human publication layer: certified result as main body,
      independent review opinions as advisory appendices, Chair readability
      certification, embedded-CJK PDF rendering, and hash provenance.
- [x] Add the sealed post-meeting submission window, explicit audit-petition
      accounting, optional Consultative minority reports, and the Human-only
      Chair accountability debrief with a mechanically appended identity map.
- [x] Add the short terminal Chair debrief, root-level Human result entry points,
      and hash-chained, fail-closed Representative context-file access auditing.
- [x] Add scoped Human consultation records for procedural conflicts, including
      formulas and derived vote-count thresholds.
- [ ] Add integration tests against recorded provider fixtures.
- [ ] Extend English localization beyond the home/setup/settings and progress
      chrome to every legacy consultation, error, and workflow-specific message;
      never translate model-authored meeting content or change report language.
- [ ] Expand the optional Technician's audited meeting-local repair registry beyond
      bounded structured-evidence context trimming (for example, presentation-
      only PDF repair and recoverable document-assembly faults).  Each repair
      class needs explicit invariants, immutable before/after records, and
      fail-closed tests; Technician must never edit ENSEMBLE source code or
      frozen scientific/ballot content.
- [ ] Make HTML mathematical typesetting fully offline by bundling a vetted browser
      math engine and its licensed fonts; keep the current CDN route explicit until
      then. Extend citation cards from bibliography metadata to bounded, source-
      specific evidence excerpts only after claim-to-source span mapping is frozen;
      never present a packet summary as an exact quote for a particular sentence.
- [ ] Extend the interactive HTML publication target beyond literature-report
      meetings to scholarly rendering and other final-publication workflows,
      using the same frozen-source/provenance contract.
- [ ] Add a validated TeX/Typst PDF compiler backend with a pinned toolchain,
      title/TOC/table/math preflight and a recoverable presentation-only fallback.
      The current ReportLab path remains usable but does not cover all LaTeX math.
- [x] Add JSON Schema export verification in CI.
- [x] Add GitHub Actions for Python 3.12 tests and schema drift checks.
- [ ] Choose an open-source license.
