# Test Report

Reference build validation for v0.7.0:

- validation date: 2026-09-22;
- unit tests: **306 passed** with Python 3.12 and the repository test environment;
- coverage instrumentation is available through `pytest-cov`; coverage was not
  re-measured for this reference run;
- Python bytecode compilation: passed;
- CLI help and JSON Schema export: passed (82 schemas);
- wheel build: passed; the wheel exposes only `ensemble-v07` and includes the
  governance package plus portable configuration examples;
- v0.6 compatibility import: tested read-only preflight, copy-on-write migration,
  retained frozen files and event-chain prefix, v0.7.0 version/digest acceptance,
  running-meeting rejection, and tampered-event rejection; no production meeting
  was migrated or resumed during validation;
- deliberation initialization: tested with the full `model × 4 persona` roster;
- audit initialization: tested with one fresh Audit Member per base model;
- general-principle execution: tested through random initial-drafter selection,
  D0→D3 frozen amendment windows, isolated positions and co-sponsorship,
  seed-replayable amendment order, Chair conflict/dependency processing,
  sealed supermajority and Protective Vote paths, traceable draft application,
  final ratification, and the atomic-item policy boundary;
- Human consultation: tested for immutable conflict questions and rulings,
  exact `N`, threshold formula, comparison, derived minimum-vote records, and
  scoped resume behavior; one-question-at-a-time Human—Chair explanation,
  immutable dialogue turns, superseded consultation chains, and Chair conflict
  reclassification are also tested;
- public, identity-private, governance-private, and human-private separation: tested;
- configuration portability: relative paths resolve from the configuration file,
  absolute config paths are retained per meeting, and `ENSEMBLE_CONFIG` works from
  outside the project directory;
- CLI output placement: `ensemble-v07 start` creates `M-.../` under the invocation
  working directory and does not redirect output to the configuration directory;
- default Slurm notification transport: tested with a subprocess double for
  `sbatch` arguments, scheduler job-ID receipts, deduplication, and omitted-email
  audit records; no Slurm job or external email was submitted;
- interactive progress: tested for phase status, participant call activity, D0
  display, and general-position display only after the all-participant release
  barrier was frozen;
- sealed-window scheduling: tested for concurrent execution across independent
  `provider:model` lanes, enforcement of each model's frozen concurrency limit,
  stable persona/roster ordering, and durable retention of valid individual
  submissions when another call in the same window fails;
- SMTP STARTTLS, delivery receipt deduplication, and failed-delivery persistence:
  tested with an in-process SMTP double; no external email was sent;
- live model discovery: passed for DeepSeek, Moonshot/Kimi, and Gemini using the
  configured credentials; the resulting catalog remains authoritative over this
  dated validation report;
- startup model filtering: provider-specific invocation methods are respected,
  blank provider selection chooses all enabled providers, and configured model
  candidates remain gated by the live catalog;
- Chinese terminal input: the full reported task text is tested in UTF-8 and
  GB18030, with UTF-8 JSON persistence verified without replacement escapes;
- output-limit diagnosis: a provider response whose measured reasoning-token
  usage exhausts the configured output-token control limit is distinguished from
  generic empty output and pauses as `MODEL_OUTPUT_LIMIT_REACHED`;
- output-token controls: by default no client-side per-call limit is sent; explicit
  `--max-output-tokens` and opt-in model-specific budgets remain supported;
- Research Desk QC: tested for claim-level isolation, final packet validation, and
  downgrading unsupported `CLEAR` consensus to `QUALIFIED` while retaining a usable
  `SOURCE_BACKED` packet;
- transient-provider retry visibility: tested for all three retry callbacks,
  terminal attempt counters, real exponential delays, HTTP `Retry-After` header
  and Google `retryDelay` body handling, and sanitized last-failure reporting;
- live text generation: passed for `deepseek-flash` and
  `kimi-k2.7-code-highspeed` using a 256-token output limit and no explicit
  temperature control parameter;
- Gemini generation transport: the validation run recorded a 404 for the
  catalogued `gemini-2.5-flash` and a transient 429 for `gemini-flash-latest`.
  These are run-specific availability observations, not hard-coded model policy;
  rerun live discovery before selecting a Gemini model.

Run `make smoke` after cloning.
