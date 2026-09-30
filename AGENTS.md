# AGENTS.md — Instructions for Codex and coding agents

Read this file before modifying Project_ENSEMBLE.

## 1. Source of truth

The governance source of truth is under `docs/governance/`.

Do **not** resolve an item listed in `docs/governance/10_open_questions/open_questions.md` by coding a convenient default. If implementation reaches an unresolved policy branch, either:

1. expose it as explicit configuration, or
2. fail closed with `PolicyNotConfiguredError`.

## 2. Priority ordering

Project_ENSEMBLE uses conservative epistemic governance:

1. safety / correctness;
2. epistemic integrity;
3. procedural integrity and traceability;
4. efficiency, latency, and token cost.

Do not remove a verification, isolation, archival, or human-escalation mechanism merely to reduce token usage unless governance explicitly authorizes that tradeoff.

## 3. Representative visibility

Never give a Representative the full governance repository.

Representative runtime context is assembled only from:

- common Representative rules;
- exactly one persona runtime file;
- exactly one current-stage protocol;
- current public substantive state;
- Representative's own allowed private state.

Do not expose:

- model/persona registry of other Representatives;
- Drafting Alignment formula, scores, ranks, or selection rationale;
- future procedural stages;
- Chair powers that are not required for the current action;
- audit metrics or anti-gaming detection rules;
- other personas' runtime files;
- hidden reasoning from any model.

## 4. Chair and Audit

Chair is governance-side and must not become a substantive extra Representative.

Audit sessions must be fresh contexts. Never promote/reuse a Representative chat/session as an Audit Member session.

## 5. Persistence

Prefer immutable source documents and append-only events. Derived summaries may be regenerated; they must never become the sole source of institutional history.

Never overwrite a frozen ballot, resolution, ruling, or provenance artifact in place.

## 6. Fail closed

Examples:

- no abstention -> reject an abstention value;
- missing ballot -> do not reveal a partial tally;
- provider unavailable after configured retries -> pause meeting;
- unknown amendment order policy -> do not sort by timestamp as a hidden default;
- missing access authorization -> deny.

## 7. Tests

Every change affecting governance semantics requires tests. Run:

```bash
pytest
```

before handing work back.

## 8. Provider adapters

Provider APIs are transport layers, not governance authorities. Keep provider-specific code isolated under `src/project_ensemble/providers/`.

Model names must be discovered/configured rather than assumed permanent.

## 9. Do not optimize agents against hidden metrics

Do not put governance-private evaluation formulas or strategic-behavior rules in Representative prompts, examples, error messages, or user-visible stage instructions.

## 10. Human authority

When governance explicitly reserves a decision to the human, code must surface the decision point; it must not infer what the human probably wants.

## 11. Public repository does not mean Representative-visible

If this project is published on GitHub, Representatives must still be prevented from fetching/searching the repository, governance documents, or mirrors during deliberation. Any future browsing/tool layer must enforce a deny policy for governance resources. A prompt instruction alone is insufficient.
