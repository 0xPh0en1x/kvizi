# Kvizi Backlog

## AI-flavored Kvizi phrases

Status: first progressive-copy slice implemented; conversations and g4f remain.

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

Implemented in the first slice:

1. `kvizi/ai.py` contains the provider interface and official Groq adapter.
2. All AI flags are off by default; a missing key means a disabled provider path.
3. New-question announcements send `copy.py` first and are edited only after a
   validated AI intro arrives; structured facts stay server-owned.
4. Timeout/429/5xx/network failures use a durable SQLite queue processed by
   `/cron/maintenance`; Telegram edit retries reuse the saved candidate text.
5. Provider, validation, migration, delayed-send, retry, edit, and disabled-flag
   behavior are covered by tests.

Next AI slices:

1. Expand progressive copy event-by-event to results, streaks, risk and daily recap.
2. Add `/kvizi_ai_check` plus provider metrics/circuit breaker.
3. Implement Reddit-like Reply/Quote discussion branches with Groq.
4. Add an explicit, remotely smoke-tested no-auth g4f allowlist only after the
   official provider path is stable on PythonAnywhere.

Do later, after core quiz operations stay stable in the real Telegram group.
