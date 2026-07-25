# Kiro Backend Prompt Caching: Findings (Not Viable)

- Date: 2026-07-25
- Status: Closed — no action. Caching is not a viable lever on this stack.
- Scope: `kiro-gateway` + opencode, talking to the Kiro backend
  (`runtime.*.kiro.dev/generateAssistantResponse`).

## Question

Is prompt-cache hitting beneficial for the real usage scenario
(kiro-gateway + opencode with claude-opus-4.8)? If so, wire it up.

## Method

Layer-1 passive observation, run DIRECT to the Kiro backend (bypassing the
gateway's own parsing so nothing is filtered): send the SAME long prompt
(~10,600 tokens: a large synthetic TypeScript module + one question) twice
back-to-back to `generateAssistantResponse`, dump the raw AWS event-stream
frames for each round, and inspect the tail (`metadataEvent` / `meteringEvent`)
for any cache fields and for round-to-round differences.

Chosen over modifying the gateway because the decisive unknown was whether the
Kiro backend protocol returns/accepts cache information at all — a question
answerable by reading raw backend frames, with zero gateway changes.

## Findings (hard evidence)

Real tail frames from the Kiro backend on the long prompt:

```
metadataEvent      {"stopReason":"END_TURN"}
contextUsageEvent  {"contextUsagePercentage":1.29...}
meteringEvent      {"unit":"credit","unitPlural":"credits","usage":0.10548587691542291}
```

1. **The Kiro backend never returns cache fields.** The entire `metadataEvent`
   payload is `{"stopReason":"END_TURN"}` — no `cache_read_input_tokens`, no
   `cache_creation_input_tokens`, nothing cache-related, on either round.
2. **Billing is credit-based, not token-based.** Consumption is reported by
   `meteringEvent` as `usage: 0.105 credit`. There is no per-token accounting
   and therefore no "cached tokens at a discounted rate" channel that classic
   prompt caching relies on for cost savings.
3. **`metadataEvent` carries only `stopReason`**, not usage detail.
4. **No `cachePoint` input.** The request path is Kiro's private
   `conversationState` / `userInputMessage` shape, not the standard Bedrock
   Messages API. There is no field in which to mark a cache checkpoint, so even
   forwarding an Anthropic `cache_control` would have nowhere to land.

## Protocol-layer context

- The underlying model (Claude Opus 4.8 via Bedrock) DOES support prompt
  caching, and coding agents with repeated long context are a textbook use case
  for it (AWS docs; Claude Code + Bedrock caching reports ~90% warm hit rates).
- BUT `generateAssistantResponse` is CodeWhisperer/Q's PRIVATE wrapper, not
  standard Bedrock `InvokeModel`/Converse. Model capability does not imply the
  private wrapper exposes a cache entry point. Web search found no evidence that
  the private protocol accepts `cachePoint` or returns cache usage. The raw
  frames above confirm it returns none.

## Conclusion: no usable benefit

| Potential benefit | Verdict on this stack |
|---|---|
| Save money (cheaper cached input tokens) | ❌ Credit billing; protocol has no cache-discount channel. |
| Report cache usage to opencode | ❌ Backend returns no cache fields; nothing to display. |
| Reduce latency | ⚠️ Only if the Kiro backend auto-caches identical prefixes internally — but that is opaque (not reported), uncontrollable (no cachePoint), and unmeasurable from our layer. We can do nothing about it and cannot observe it. |

On the kiro-gateway + opencode path, prompt caching is not something we can
drive and observe a benefit from. The Kiro backend's private protocol exposes
neither a cache control input nor cache usage output, and billing is credits.
This is firmer evidence for the earlier "drop the caching line" decision.

## Dead code (kept intentionally)

`kiro/streaming_anthropic.py` contains cache plumbing that predates this work
and is NOT ours:

- `_extract_cache_usage_fields()` (:101) mapping
  `cache_read_input_tokens` / `cacheReadInputTokens` /
  `cache_creation_input_tokens` / `cacheCreationInputTokens`
- its call sites at :524 (streaming) and :762 (non-streaming)

Since the Kiro backend never emits these fields, this mapping never fires — it
is effectively dead code on this backend. It is written defensively ("only
forwarded when explicitly returned by upstream") and is harmless.

Decision: **leave it in place.** It is upstream code; removing it would create
needless merge conflicts against upstream and provides no benefit. Documented
here so its inertness is understood rather than mistaken for a working feature.

## No rollback required

This investigation produced no gateway code changes. The earlier "drop caching"
decision was a conclusion, not an edit — no caching logic was ever added by us.
The only uncommitted changes in the working tree belong to the native-reasoning
passthrough work and are unrelated to caching.

## Out of scope / possible follow-ups

If the real motivation is latency or credit reduction (caching was only a
guessed means), more promising levers exist and would need their own
investigation:

- Trim per-turn resent history/context volume (directly cuts credits and
  prefill time).
- Audit the gateway for payload bloat (`AUTO_TRIM_PAYLOAD` path already exists).
