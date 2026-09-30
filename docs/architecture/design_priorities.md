# Design Priorities

Project_ENSEMBLE does not define or claim a globally optimal multi-agent architecture.

Its current design orientation is conservative and quality-first.

A useful abstraction is constrained optimization rather than a single weighted utility:

```text
minimize: token cost, latency, incidental complexity
subject to:
  acceptable correctness
  acceptable safety
  epistemic integrity
  procedural integrity
  traceability
```

Approximate priority ordering:

```text
Safety / Correctness
  > Epistemic Integrity
  > Procedural Robustness / Traceability
  > Token / Latency Efficiency
```

This does not mean cost is irrelevant. It means cost is optimized *inside* an acceptable epistemic and procedural envelope.

Operational consequences include:

- preserve dissent rather than manufacture consensus;
- label contested outcomes;
- pause on Representative unavailability instead of silently changing the electorate;
- preserve immutable source records rather than relying only on summaries;
- run procedural and epistemic post-checks;
- escalate unresolved governance decisions to the human;
- do not remove isolation or verification merely to save tokens.

For the internal `pragmatic_minimalist` configuration (human-facing label:
`Monitor / 监管者`), simplicity is subordinate to quality constraints: choose the least
complex solution among solutions that already meet the required epistemic and procedural floor.
