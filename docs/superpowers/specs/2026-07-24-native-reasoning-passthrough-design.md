# Native Reasoning Passthrough (Kiro Gateway)

- Date: 2026-07-24
- Status: Proposed (awaiting approval)
- Scope: `kiro-gateway` only. No opencode-side changes.
- Decision baseline: Plan B — remove fake-reasoning injection, use Kiro's native reasoning stream.

## Problem

opencode shows `reasoning=0` for `claude-opus-4.8` routed through this gateway,
even though Kiro streams genuine reasoning. Two independent defects:

1. The gateway does not parse Kiro's native reasoning events, so real thinking
   never reaches the client.
2. The gateway injects a "fake reasoning" scaffold (`<thinking_mode>` /
   `<thinking_instruction>` tags) into every prompt and then tries to scrape
   `<thinking>` blocks back out of the answer body. This pollutes the prompt,
   fights the native channel, and (per observation) does not actually produce
   reasoning in the opencode path.

## Evidence (hard, not inferred)

Captured via a one-shot script that reused the gateway's auth + headers and
talked to Kiro directly (`/tmp/opencode/capture_reasoning_frames.py`, run
against `claude-opus-4.8`, region `eu-central-1`). Raw stream = 37,248 bytes.

Frame types observed in the AWS event-stream (`:event-type` header → payload):

| `:event-type`            | JSON payload                      | Meaning              | Count |
|--------------------------|-----------------------------------|----------------------|-------|
| `reasoningContentEvent`  | `{"text":"..."}`                  | native reasoning delta | 36  |
| `signature`  (in frame)  | `{"signature":"<base64>"}`        | extended-thinking signature | 1 |
| `assistantResponseEvent` | `{"content":"..."}`               | answer text delta    | 235   |
| `contextUsageEvent`      | `{"contextUsagePercentage":...}`  | already handled      | 1     |
| `meteringEvent`          | `{"unit":...}`                    | credit metering      | 1     |
| `metadataEvent`          | `{...}`                           | metadata             | 1     |

Stream ordering (verified): all 36 reasoning deltas → 1 signature → 235 answer
deltas. This maps 1:1 onto the Anthropic streaming contract (thinking block
with deltas → `signature_delta` → `content_block_stop` → text block).

No-collision facts that make parsing safe:
- `{"text":` appears ONLY in reasoning frames; answer body uses `{"content":`.
- `{"signature":` is unique.
- The base64 signature decodes to real Anthropic thinking material (contains
  `thinking` / `claude-quince` markers), i.e. Kiro passes through a genuine
  extended-thinking signature, not a placeholder.

Fake-reasoning control: `chat.enableThinking` in kiro-cli does NOT gate native
reasoning — disabling it still produced 10 reasoning frames on the same puzzle.
The wire request carries no thinking/effort field. So the gateway does not need
to "ask" for reasoning; it only needs to parse what already arrives.

## Current architecture

- Parse side — `kiro/parsers.py` `AwsEventStreamParser`
  - `EVENT_PATTERNS` (:241) matches only `content/tool_start/tool_input/tool_stop/
    followup/usage/context_usage`. No reasoning pattern.
  - `_process_event` (:308) has no reasoning branch.
- Middle layer — `kiro/streaming_core.py`
  - `KiroEvent` already has `type="thinking"`, `thinking_content`, and thinking
    signature fields.
  - Today `thinking` events come ONLY from `ThinkingParser` (fake reasoning,
    :144-147, :187-199).
- Output side — `kiro/streaming_anthropic.py`
  - Already emits proper native Anthropic thinking blocks when
    `FAKE_REASONING_HANDLING == "as_reasoning_content"` (:261-289 streaming;
    :768 non-streaming), including `signature`.

Conclusion: the output pipeline is already built. The only gap is that its data
source is the fake-reasoning scraper instead of the native reasoning frames.

## Design

Three parts, all inside the gateway.

### Part 1 — Parse native reasoning (`kiro/parsers.py`)

- Add to `EVENT_PATTERNS`:
  - `('{"text":', 'reasoning')`
  - `('{"signature":', 'signature')`
- Add branches in `_process_event`:
  - `reasoning` → `{"type": "thinking", "data": <text>}`
  - `signature` → `{"type": "signature", "data": <signature>}`
- Ordering guarantee holds because the parser already scans for the earliest
  matching prefix in the buffer, and reasoning frames precede answer frames.

### Part 2 — Carry reasoning through the core (`kiro/streaming_core.py`)

- Map parser `thinking` events to `KiroEvent(type="thinking", thinking_content=...)`.
- Map parser `signature` events to a `KiroEvent` that carries the real signature
  (either `type="thinking"` with a signature field set, or a dedicated marker
  consumed at block close). Exact shape decided in implementation to match what
  the output side expects.
- Remove the `ThinkingParser` path (fake reasoning) — see Part 3.

### Part 3 — Remove fake reasoning (Plan B)

Per agreement: keep the code but default it OFF (reversible), not a hard delete.

- `kiro/config.py`: default `FAKE_REASONING_ENABLED` to false. Prompt injection
  (`inject_thinking_tags`, `<thinking_mode>` system addition) becomes inert by
  default. This is what makes the per-message injected tags disappear.
- `kiro/streaming_core.py`: gate the `ThinkingParser` init on the (now-false)
  flag so native reasoning is the sole source unless someone re-enables fake
  reasoning for a non-Claude model.
- `kiro/streaming_anthropic.py`: keep the `as_reasoning_content` output branch.
  Emit the REAL signature from the stream. Do NOT fall back to the placeholder
  `generate_thinking_signature()` — see below.

### Signature: emit real, never fake

`generate_thinking_signature()` (:88) returns `sig_<uuid>` — a fake signature
value on the thinking block. Native reasoning always carries a real signature
(observed 36 reasoning deltas → 1 real signature → 235 answer deltas), so the
only time a real signature is absent is a truncated/interrupted stream.

Emitting a fake signature in that case is worse than emitting none: if it were
ever echoed back and validated, it would fail validation. Therefore:

- When a native signature arrives → emit it on the thinking block.
- When none arrives (truncation) → emit the thinking block WITHOUT a signature
  (or omit the thinking block), never a fabricated one.

The placeholder path is removed for the native flow.

### Signature round-trip: confirmed NON-ISSUE (evidence)

Verified in `kiro/converters_anthropic.py`: inbound
`convert_anthropic_content_to_text` (:62-73) extracts only `type == "text"`
blocks; `convert_anthropic_messages` (:281-306) additionally pulls `tool_use`
(assistant) and `tool_result`/images (user). There is NO `thinking` branch.

Consequence: any `thinking` block (and its signature) in the client's message
history is silently dropped on the way in, so it never reaches the Kiro payload
and Kiro never validates it. Multi-turn signature round-trip is therefore a
non-issue for the first cut. The first cut is pure real-time passthrough of the
current turn's reasoning + signature; it introduces no cross-turn state.

(If Kiro later requires echoed signatures for multi-turn thinking continuity,
that becomes a separate feature: preserve thinking blocks inbound AND forward
them in the Kiro payload. Out of scope here.)

## Non-Claude models

minimax / qwen / glm / deepseek were NOT tested for native reasoning. With fake
reasoning off by default, if they do not emit `reasoningContentEvent` they will
simply have no thinking (answers unaffected). The retained `FAKE_REASONING=true`
switch is the escape hatch for them. Primary target is opus-4.8.

## Risk & rollback

- Blast radius: 3 gateway files; output pipeline unchanged; opencode unchanged.
- Reversible: pure gateway code (git revert); fake reasoning retained behind a
  flag rather than deleted.
- Verification plan:
  1. Re-run the capture script to confirm frame shapes still hold.
  2. Send a complex prompt through the gateway; assert the Anthropic SSE stream
     contains a `thinking` content block with `thinking_delta`s and a
     `signature`, followed by the text block.
  3. Send a trivial prompt; assert no thinking block and no injected
     `<thinking_mode>` tags in the outgoing Kiro payload.
  4. Confirm opencode shows non-zero reasoning for opus-4.8.

## Out of scope

- Caching (credit-billed, four cache-field observations all null; abandoned).
- First-token latency (model-side, not a gateway bug).
- Multi-turn signature validation (flagged above; separate follow-up).
