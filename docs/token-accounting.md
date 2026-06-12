# Token Accounting

AgentBoard's hardest correctness problem is that every AI coding tool reports tokens differently. This document defines the canonical semantics used by all collectors and the dashboard.

## The Two Numbers That Matter

- **`provider_total_tokens`** — the canonical total. Defined as *what the provider itself would report as total usage*. This is the number used for dashboard totals and the leaderboard, and it should match the tool's official usage display.
- **`non_cache_total`** — `provider_total_tokens − cache_read_tokens − cache_creation_tokens`. Used for cache-free comparisons across tools. Computed server-side; collectors upload the components.

Plus the breakdown fields: `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `thoughts_tokens`, `tool_tokens`, and the legacy `tokens_used` (= input + output, kept for backward compatibility).

## Per-Source Policy

### Claude Code / Claude Cowork

Claude transcripts report cache usage as separate fields (`cache_read_input_tokens`, `cache_creation_input_tokens`) **not** included in `input_tokens`.

```
provider_total = input + output + cache_read + cache_creation
```

This matches how Anthropic accounts for usage: cache reads and writes are real consumed tokens, billed at their own rates.

### Codex CLI

Codex session logs report `input_tokens` **already including** cached input tokens. `cached_input_tokens` is an informational sub-field of input, not an addition to it.

```
provider_total = input + output        # cache is already inside input
```

`cache_read_tokens` is still uploaded for breakdown display, but adding it to the total would double-count. The collector also performs **replay-prefix detection**: Codex can re-emit a session's token counters when a session is resumed, so the collector tracks per-session baselines and computes deltas to avoid counting the same tokens twice.

For verification, the Codex collector ships diagnostics:

```bash
python3 collectors/collect_codex.py --diagnose-date 2026-06-01
python3 collectors/collect_codex.py --diagnose-range 2026-06-01 2026-06-07
```

These print `provider_total_tokens`, `non_cache_total`, the policy era applied, and the legacy (pre-fix) figures for comparison.

### Gemini CLI

Gemini session records carry `usageMetadata` with `totalTokenCount`, `promptTokenCount`, `candidatesTokenCount`, `cachedContentTokenCount`, `thoughtsTokenCount`, and `toolUsePromptTokenCount`.

```
provider_total = totalTokenCount       # provider's own figure, used as-is
                 (fallback: input + output when totalTokenCount is absent)
```

Gemini does not report cache creation; `cache_creation_tokens` is always 0 for this source.

### OpenCode / OpenClaw

Both report cache as separate usage fields (`cacheRead` / `cacheWrite`) outside of input, and may carry a provider-reported total (`totalTokens`):

```
provider_total = totalTokens            # provider's own figure, when present
                 (fallback: input + output + cache_read + cache_creation)
```

Same Claude-style convention: cache is additive, never folded into input.

## Why Keep Both Total and Breakdown?

1. **Trust** — users compare AgentBoard numbers against their tool's own dashboard. `provider_total` is defined to match it.
2. **Fairness** — cache-heavy workflows (large repos, long sessions) inflate raw totals. `non_cache_total` lets rankings and comparisons strip cache effects without losing the provider-faithful number.
3. **Explainability** — when a number looks wrong, the breakdown (input/output/cache/thoughts) localizes which source semantics are responsible.

## Time Derivation (Related Metrics)

- `coding_time_mins` — engaged time computed from event timestamps with a 10-minute idle gap rule and a 2-minute session tail, clamped to 480 min/session and 960 min/day.
- `ai_time_mins` — a rough estimate derived from output tokens (≈300 tokens/min reading-speed heuristic).

Both are estimates by construction and documented as such.
