# Security and credential handling

Project_ENSEMBLE treats model credentials and private meeting compartments as
security boundaries.

- Never commit `ensemble.toml`, `model_config.toml`, `.env`, key files, meeting
  directories (`M-*`, `AU-*`, `LR-*`, `VR-*`, `DL-*`, `SR-*`), or generated resume
  scripts. The supplied `.gitignore` excludes these common paths, but review
  the staged file list before every public push.
- Configure only the *name* of an environment variable in `model_config.toml`.
  Put the secret itself in the process environment or in a local assignment
  file such as `export GLM_API_KEY='…'`, referenced by `api_key_file`.
- Assignment files are parsed as data. They are never sourced or executed.
- Prefer filesystem permissions `0600` for credential files.
- Run `ensemble doctor --config /path/to/ensemble.toml` before starting a
  production meeting. Doctor reports missing credentials without printing them.
- Do not attach private meeting compartments to public bug reports. Redact
  `identity_private/`, `human_private/`, `chair_private/`,
  `governance_private/`, `think_tank_private/`, and `audit_private/`.

Security reports should initially contain only the affected version, a concise
reproduction, and the expected confidentiality impact. Do not include live API
keys or private meeting content.
