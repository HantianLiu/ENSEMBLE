# Contributing

Project_ENSEMBLE is governance-first software. A code change must not silently
invent a policy that the governance documents leave open.

1. Read `AGENTS.md`, `docs/governance/`, and the relevant item in `TODO.md`.
2. Keep model/provider behavior behind the provider abstraction. Never hard-code
   credentials, local paths, live model IDs, or a fixed Representative count.
3. Preserve immutable meeting artifacts and resume semantics.
4. Add deterministic tests for every new state transition, threshold, recovery
   path, and confidentiality boundary.
5. Run `pytest` and `python scripts/export_json_schemas.py` before proposing a
   change. Generated schemas must match the source models.

This checkout is the v0.7.3 preview and installs `ensemble` (`ensemble-v071`
is a compatibility alias retained from the original v0.7.1 distribution). The separate v0.7.0 installation uses
`ensemble-old` (`ensemble-v07` remains its compatibility alias). Install the
versions in separate virtual environments: both use the Python import namespace
`project_ensemble`. Do not publish real meeting directories, resume scripts,
credentials, or private meeting compartments in issues or pull requests. Use
synthetic fixtures and the issue templates instead.
