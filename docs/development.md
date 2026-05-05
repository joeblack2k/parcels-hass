# Development

## Local checks

```bash
python -m pip install --upgrade pip pytest
pytest
python -m compileall -q custom_components/package_inbox
```

## Public-release hygiene

Before pushing:

- Keep the Home Assistant domain `package_inbox`.
- Do not copy a live Home Assistant config directory.
- Do not commit `.storage`, databases, logs, diagnostics, secrets, Matrix room IDs, IMAP entry IDs, tokens, dashboard config, or personal defaults.
- Keep Amazon cookie bridge work out of the public HACS default flow.
- Prefer parser fixtures and normalized records over raw personal mail examples.

## CI

The repository runs:

- Unit tests with `pytest`.
- Python syntax validation with `compileall`.
- Home Assistant hassfest validation.
- HACS validation.
