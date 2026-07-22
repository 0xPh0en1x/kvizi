# Kvizi Backlog

## AI-flavored Kvizi phrases

Status: design agreed; implementation has not started.

The complete design for provider-neutral host copy and Reddit-like Telegram
discussion threads is recorded in [`AI_DESIGN.md`](AI_DESIGN.md). It covers
Reply/Quote context, SQLite branch history, Groq and explicit no-auth g4f
fallbacks, privacy, limits, feature flags, tests, and staged PythonAnywhere
rollout.

Keep the core provider-neutral. An unofficial `g4f` adapter may be
experimental, but must never be the only configured provider or a runtime
dependency of quiz delivery.

Idea: use `xtekky/gpt4free` / `g4f` only for stylistic text in Kvizi's voice, not for factual quiz generation.

Good first uses:

- announce-topic flavor text for a new question;
- challenge success/failure phrases;
- daily recap flavor text;
- admin command `/kvizi_hype` to generate a short circus-style announcement.

Guardrails:

- do not generate factual quiz questions automatically;
- do not invent technical claims, answers, sources, scores, or user stats;
- pass only safe structured context: `topic_key`, `difficulty`, base points, and question link;
- use a short timeout and static fallback text so Telegram flows never fail because AI is unavailable;
- keep it optional behind `KVIZI_AI_ENABLED=1`;
- expect `g4f` providers to be unstable and possibly incompatible with PythonAnywhere free outbound/network limits.

The first implementation slice remains:

1. Add `kvizi/ai.py` with a provider interface, Groq adapter, and no-op fallback.
2. Keep all AI flags off by default.
3. Generate only a one- or two-sentence announcement suffix from structured facts.
4. Preserve `kvizi/copy.py` as the guaranteed fallback.
5. Cover timeout/fallback behavior with tests before enabling anything remotely.

Do later, after core quiz operations stay stable in the real Telegram group.
