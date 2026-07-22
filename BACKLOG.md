# Kvizi Backlog

## AI-flavored Kvizi phrases

Status: planned as the next optional feature after database backup/restore.
Keep the core provider-neutral; an unofficial `g4f` adapter may be experimental,
but must never be the only configured provider or a runtime dependency of quiz
delivery.

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

Suggested implementation:

1. Add `kvizi/ai.py` with a provider interface and a no-op fallback provider.
2. Add optional `g4f` adapter loaded only when enabled and installed.
3. Generate only a one- or two-sentence announcement suffix.
4. Store generated phrase in logs/status only if useful for debugging.
5. Cover timeout/fallback behavior with tests.

Do later, after core quiz operations stay stable in the real Telegram group.
