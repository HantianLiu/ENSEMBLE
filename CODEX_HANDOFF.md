# Historical Codex Handoff — Project_ENSEMBLE v0.7.0

## Current state

The repository contains a governance baseline plus a tested reference core. The
core is intentionally incomplete where the constitution is incomplete. New meeting
sessions now start through `ensemble-v07 start` (or bare `ensemble-v07`), which requires a terminal setup UI and
accepts an optional human escalation address. The default notification transport
submits a minimal Slurm job so the scheduler sends the email; missing notification
configuration is recorded rather than blocking startup.

New meetings now separately freeze one reasoning-effort control for all
Representatives and another for Chair. They may also enable a shared, non-voting
Research Desk and then select its model and reasoning effort. The Research Desk
core normalizes bounded claims, performs adversarial OpenAlex/Tavily retrieval,
publishes structured evidence packets, reuses fresh claim fingerprints, and retains
exact queries/candidates/screening decisions under `audit_private/research/`.
Formal Research Rounds are integrated at the implemented deliberation boundaries,
while `ensemble-v07 research-claim` remains the explicit human-requested lookup command.
A stage-native model → Research Desk → model relay inside an in-progress
Proposal/Challenge/Veto turn remains future work; models must never receive direct
network access.

`ensemble-v07 migrate-v06` provides an explicit copy-on-write path for unfinished
v0.6 meetings. It checks the historical event chain, stopped run lock, manifests,
configured providers, files, and symlinks before copying. Only the destination
receives v0.7.0 version/configuration metadata and a migration event; the original
remains available to v0.6. The command cannot import Audit meetings.

`ensemble-v07 run-general` now runs the supported path from D0 through three frozen
amendment windows, Chair conflict/dependency processing, sequential sealed binary
ballots, reason statements and Protective Vote when required, traceable draft
application, D3 ratification, trial Drafting Alignment, detailed-clause review and
option-set processing, Chair/Think Tank handoff, final publication, and the
post-meeting accountability debrief. It is resumable from immutable frozen
artifacts; provider failures and still-open governance policies continue to pause
explicitly. Procedural conflicts open a scoped Human consultation with exact
threshold records; partial-ballot recovery is governed by Chair integrity review:
complete sealed votes are retained while invalid and missing votes are re-collected
without revealing a partial tally. Interactive startup enters this path immediately
and displays live phase, call, and post-freeze speech output. `ensemble-v07 run-general
--no-progress` suppresses it.
Atomic-item scoring uses the Human-authorized trial policy and remains explicitly
marked `TRIAL` pending Think Tank review; the implementation does not silently
promote it to a permanent constitutional rule.

Start by running:

```bash
pip install -e '.[dev]'
pytest
ensemble-v07 doctor --config examples/ensemble.example.toml
```

`doctor` will report missing API keys without printing secret values.

## Recommended next implementation sequence

### P0 — preserve institutional invariants

1. Keep all current tests green.
2. Add a general policy for schema-invalid reachable Representative/Chair output
   **only after Human policy is specified** (Research Desk claim-level repair and
   isolation are already implemented separately).
3. Preserve the Human-selected replayable random amendment ordering rule and its
   seed/algorithm/final-order provenance.
4. Preserve the confirmed recovery rule: Chair retains complete sealed votes and
   re-collects only invalid or missing votes, with an immutable private review.

### P1 — meeting persistence (initialization and resumable artifacts complete;
full reconstruction remains)

`MeetingRepository` persists the initial manifests, task, private roster, human-only
escalation contact, hash-chain event log, frozen submissions, research snapshots,
publication artifacts, and recovery ledgers. A general event-log-to-state
reconstructor still needs to cover every later-stage source document uniformly:

- `meeting_manifest.json`
- public docket
- governance-private state
- representative-local state
- chair-private state
- audit-private state

All transitions should be reconstructible from source documents + event log.

### P2 — remaining orchestration work

The private `MeetingEngine`, stage-local context assembly, provider retry boundary,
Chair procedural records, final handoff/publication, and Representative file-access
ledger are implemented. Remaining orchestration work is limited to the governance
branches listed in `TODO.md`, the stage-native Research Desk relay, and complete
state reconstruction. Do not make the state machine itself Representative-visible.

### P3 — Audit Conference

Use one fresh Audit Member per base model. Implement:

- isolated Phase-1 independent data mining;
- release barrier;
- independent preliminary assessment;
- rotating finite discussion order;
- max five turns/model, PASS counts;
- independent final opinions;
- Chair synthesis with machine-readable support map;
- strategic-behavior audit over governance-private data.

An all-PASS audit round ends discussion immediately when no finding,
recommendation, or strategic-behavior concern remains. The audit execution state
machine itself is intentionally still a fail-fast stub in v0.7.0.

## Explicit non-goals for the current milestone

- Autonomous end-to-end scientific research.
- Autonomous execution of every approved proposal.
- A third “code delivery meeting”.
- Optimizing token cost at the expense of auditability or correctness.
- Training/fine-tuning models against governance-private metrics.
- Claiming ENSEMBLE is an optimal architecture.

## Suggested first real integration test

Use 3 providers/models selected by the human and 4 personas = 12 Representatives.

Choose a bounded task with a checkable deliverable. Record:

- every provider request ID if available;
- input/output token counts;
- raw model output before parsing;
- parsed action;
- all Chair corrections;
- sealed ballot submissions;
- immutable final documents.

The first integration goal is **protocol fidelity**, not benchmark performance.
