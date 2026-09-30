# Provider Notes

The reference implementation uses live model discovery rather than hard-coded model rosters.

Current default endpoints and reasoning mappings in the example config are based on official provider documentation checked 2026-09-18:

- DeepSeek OpenAI-compatible base URL: `https://api.deepseek.com`; model list at `GET /models`.
- Kimi Open Platform OpenAI-compatible base URL: `https://api.moonshot.ai/v1`; model list at `GET /v1/models` relative to host.
- GLM OpenAI-compatible base URL: `https://open.bigmodel.cn/api/paas/v4`; its text models are
  discovered through the configured OpenAI-compatible catalog endpoint.
- Gemini Models API: `GET https://generativelanguage.googleapis.com/v1beta/models`.

Reasoning controls are deliberately model-scoped:

- DeepSeek Chat Completions accepts `reasoning_effort`; the normalized `medium`
  level maps to DeepSeek `high` because that is the provider's documented mapping.
- Kimi `kimi-k3` accepts `low`, `high`, or `max`; ENSEMBLE maps its normalized
  `low / medium / high` controls to `low / high / max`. Other configured Kimi
  models retain provider defaults unless a separate verified mapping is added.
- Gemini 3 `generateContent` accepts `generationConfig.thinkingConfig.thinkingLevel`.
  Gemini aliases and earlier model generations retain provider defaults because
  their compatible control can differ.
- No GLM effort mapping is asserted by the current configuration; GLM therefore
  exposes only `default` until a model-specific effort control is verified.

By default ENSEMBLE does not send a client-side `max_output_tokens` value. The provider's own
output ceiling applies. A Human may opt into model-specific ENSEMBLE budgets through
`governance.provider_output_token_limit`, or set a one-run limit with `--max-output-tokens`.

Input context has a separate preflight acceptance ceiling. For models with an
advertised input capacity, the default is 80% of that capacity; models without
metadata use the explicit 262,144 estimated-token fallback. The provider-neutral
estimate is conservative and is not reported as measured usage. Research
evidence is compacted into a bounded relevance view before this check; core
governance and current-action state are never silently truncated.

The checked-in operational configuration supplies model-scoped capacities when
an OpenAI-compatible `GET /models` response omits them. DeepSeek Flash/V4 Pro,
Kimi K3, and GLM 5.2/5.3 use a 1,048,576-token model context capacity, producing
an ENSEMBLE acceptance ceiling of 838,860 estimated input tokens under the 80%
safety policy. Kimi K2.6/K2.7 use 262,144 tokens. Older selectable GLM models use
their own 128K/200K-class capacities rather than inheriting the generic fallback.
These are input controls derived from vendor/model metadata, not measured prompt
usage. Gemini continues to use the live Models API `inputTokenLimit` field so
moving `-latest` aliases are not frozen to a stale local value.

The startup TUI computes the intersection of supported normalized levels across
all models controlled by one setting. It never sends an unverified field merely
because an endpoint is OpenAI-compatible.

Provider APIs evolve. Treat endpoint URLs and model IDs as configuration/discovery data, not constitutional facts.
