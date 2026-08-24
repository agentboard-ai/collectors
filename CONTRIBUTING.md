# Contributing

Thanks for your interest in improving AgentBoard collectors!

## Ground Rules

1. **Privacy is the product.** Any PR that adds a new uploaded field must update [PRIVACY.md](./PRIVACY.md) and [docs/data-fields.md](./docs/data-fields.md) in the same PR. PRs that upload content (text, paths, code) will not be merged.
2. **Standard library only.** Collectors must run with a plain `python3` (3.9+) on macOS, Linux, and Windows. No `pip install`, ever — users audit and run these scripts directly.
3. **Self-contained scripts.** Each collector is intentionally standalone (the Cowork collector, which imports `collect.py`, is the one exception). Resist the urge to extract a shared module — auditability in one read beats DRY here.
4. **Don't break sync state.** Bumping parsing logic may require a rescan; use the `AGENTBOARD_SCRIPT_RELEASE` version constant and the reparse-on-upgrade mechanism rather than ad-hoc cache busting.

## Adding a New Source

The most valuable contribution. A new collector (`collect_<source>.py`) needs:

1. **Discovery** — default log locations on macOS/Linux/Windows, plus environment-variable overrides (follow the pattern: `$<SOURCE>_HOME` etc.).
2. **Parsing** — read the tool's session logs (JSONL/JSON/SQLite) into per-session, per-day aggregates. Parse in memory; never copy log content anywhere.
3. **Token policy** — document how the tool reports tokens. Does input already include cache? Is there a provider total? Add the answer to [docs/token-accounting.md](./docs/token-accounting.md).
4. **Payload** — same field set as existing collectors (see [docs/data-fields.md](./docs/data-fields.md)), with a new `source` value and session-id namespace prefix.
5. **CLI** — support `--summary [dir]`, `--sync [dir]`, `--daemon [dir]`, `--json`.
6. **Fixtures** — add a small synthetic session log under `tests/fixtures/` (never real logs) demonstrating the format.

## Testing

```bash
# Dry-run against your own real logs (nothing uploads):
python3 collectors/collect.py --summary

# Codex token-accounting diagnostics:
python3 collectors/collect_codex.py --diagnose-date 2026-06-01
```

Before opening a PR, run `--summary` for every collector you touched and sanity-check the aggregate against the tool's own usage display where one exists.

## Style

- Python: stdlib only, no type-checking dependencies; match the existing defensive-parsing style (logs are user-editable and frequently malformed — never crash on bad input, skip and continue).
- Keep functions small and named for what they extract; future auditors read this code top to bottom.

## Translations

READMEs exist in English (source of truth), 简体中文, 繁體中文, 日本語, 한국어, and Português (BR). If you update `README.md`, either update every translation or open an issue tagging the translation so it doesn't drift. Docs under `docs/` are English-only by design.
