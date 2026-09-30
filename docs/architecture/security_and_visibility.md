# Security and Visibility Architecture

ENSEMBLE treats information boundaries as software access-control boundaries.

## Compartments

A meeting workspace should contain logically separate compartments:

```text
public/
representatives/<meeting-local-id>/
chair_private/
governance_private/
identity_private/
human_private/
audit_private/
```

Research Desk 的共享 evidence packet 位于 `public/research/`；精确查询、候选结果、
排除理由和请求关联位于 `audit_private/research/`。Research Desk 的模型配置仍属于
`identity_private/`，provider exchange 属于 `governance_private/`。

## Representative

May receive only:
- common Representative rules;
- exactly one persona runtime memory;
- current stage-local protocol;
- current public substantive state;
- own explicitly allowed state.

Must not receive:
- future-stage protocol;
- other persona prompts;
- hidden scoring/selection rules;
- model/persona identity mapping;
- Chair-private observations;
- audit detection metrics.

## Chair

May receive procedural/governance-private state required to execute current governance rules, keyed by meeting-local Representative IDs.

Chair does not require real model/persona identity mapping to classify amendments or compute status transitions.

## Audit

After the governance-defined unblinding point, Audit may receive governance-private and identity-private records required to evaluate model/persona/procedure interactions and strategic behavior.

Audit runtime must be fresh and isolated from prior Representative sessions.

## Human / Orchestrator

Can access all compartments.

`human_private/` contains operational human-contact data such as the escalation
email address. It is not part of the later Audit unblinding surface and must not
be loaded into any model context.

## Derived summaries

Summaries are views, not source of truth. Immutable source documents and hash-chained events remain authoritative.

## Public repository caveat

The governance repository may be public for humans while remaining unavailable to deliberating Representatives. If Representatives receive browsing/search tools, the tool proxy must block Project_ENSEMBLE governance resources and known mirrors during deliberation. Otherwise Procedural-Horizon and Evaluation firewalls can be bypassed by retrieval.
