# Graph Report - D:\Dev\durka\kvizi  (2026-07-07)

## Corpus Check
- Corpus is ~20,289 words - fits in a single context window. You may not need a graph.

## Summary
- 406 nodes · 1142 edges · 19 communities (17 shown, 2 thin omitted)
- Extraction: 92% EXTRACTED · 8% INFERRED · 0% AMBIGUOUS · INFERRED: 92 edges (avg confidence: 0.57)
- Token cost: 0 input · 0 output

## Community Hubs (Navigation)
- [[_COMMUNITY_Service Test Harness|Service Test Harness]]
- [[_COMMUNITY_Bot Service Orchestration|Bot Service Orchestration]]
- [[_COMMUNITY_App Config Reports|App Config Reports]]
- [[_COMMUNITY_SQLite Repository State|SQLite Repository State]]
- [[_COMMUNITY_Telegram API Client|Telegram API Client]]
- [[_COMMUNITY_Question Bank Routing|Question Bank Routing]]
- [[_COMMUNITY_Copy And Scoring|Copy And Scoring]]
- [[_COMMUNITY_Project Verification Docs|Project Verification Docs]]
- [[_COMMUNITY_State Export Script|State Export Script]]
- [[_COMMUNITY_Runtime Cron Concepts|Runtime Cron Concepts]]
- [[_COMMUNITY_Persona AI Backlog|Persona AI Backlog]]
- [[_COMMUNITY_Deployment Setup|Deployment Setup]]
- [[_COMMUNITY_Gameplay Scoring|Gameplay Scoring]]
- [[_COMMUNITY_Question CSV Operations|Question CSV Operations]]
- [[_COMMUNITY_Cron Endpoint Setup|Cron Endpoint Setup]]
- [[_COMMUNITY_AI Provider Interface|AI Provider Interface]]
- [[_COMMUNITY_Local Cron Tests|Local Cron Tests]]
- [[_COMMUNITY_Smoke Check Tests|Smoke Check Tests]]

## God Nodes (most connected - your core abstractions)
1. `KviziRepository` - 83 edges
2. `KviziService` - 75 edges
3. `make_service()` - 38 edges
4. `TelegramClient` - 34 edges
5. `FakeTelegram` - 27 edges
6. `QuestionBank` - 26 edges
7. `Settings` - 23 edges
8. `Question` - 23 edges
9. `TelegramApiError` - 23 edges
10. `create_app()` - 21 edges

## Surprising Connections (you probably didn't know these)
- `AI-Flavored Kvizi Phrases` --semantically_similar_to--> `Announcement Topic`  [INFERRED] [semantically similar]
  BACKLOG.md → README.md
- `PythonAnywhere Setup` --semantically_similar_to--> `PythonAnywhere`  [INFERRED] [semantically similar]
  DEPLOY.md → README.md
- `cron-job.org` --semantically_similar_to--> `cron-job.org`  [INFERRED] [semantically similar]
  DEPLOY.md → README.md
- `Gameplay` --semantically_similar_to--> `Scoring Rules`  [INFERRED] [semantically similar]
  RELEASE_NOTES.md → README.md
- `Question CSV Workflow` --semantically_similar_to--> `Telegram Question Upload Workflow`  [INFERRED] [semantically similar]
  DEPLOY.md → README.md

## Import Cycles
- None detected.

## Hyperedges (group relationships)
- **Runtime Deployment Operations** — deploy_pythonanywhere_setup, deploy_cron_job_org, readme_pythonanywhere, readme_cron_job_org, release_notes_local_deployment_tooling, requirements_requirements [INFERRED 0.85]
- **Question Lifecycle** — readme_questions_csv, readme_validate_questions, readme_question_upload_workflow, deploy_question_csv_workflow, release_notes_question_operations [INFERRED 0.95]
- **Gameplay Scoring System** — readme_quiz_poll, readme_inline_bets, readme_poll_answer_scoring, readme_scoring_rules, readme_challenge_questions, release_notes_gameplay [INFERRED 0.85]

## Communities (19 total, 2 thin omitted)

### Community 0 - "Service Test Harness"
Cohesion: 0.09
Nodes (53): FakeTelegram, make_question_bank(), make_service(), make_settings(), Any, Path, _seed_poll(), _seed_today_answer() (+45 more)

### Community 1 - "Bot Service Orchestration"
Cohesion: 0.10
Nodes (6): utc_now_iso(), format_report_for_telegram(), base_points(), KviziService, Any, Path

### Community 2 - "App Config Reports"
Cohesion: 0.10
Nodes (39): Counter, Flask, load_settings(), _parse_admin_ids(), _parse_optional_int(), Settings, build_report(), find_duplicate_ids() (+31 more)

### Community 3 - "SQLite Repository State"
Cohesion: 0.10
Nodes (8): AnswerResult, KviziRepository, Any, Connection, Path, _row_to_dict(), ScoreResult, Row

### Community 4 - "Telegram API Client"
Cohesion: 0.14
Nodes (12): Any, TelegramApiError, TelegramClient, Response, RuntimeError, main(), FakeFileResponse, FakeJsonResponse (+4 more)

### Community 5 - "Question Bank Routing"
Cohesion: 0.14
Nodes (14): Question, QuestionBank, Random, TopicRoute, TopicRouter, BackupExportResult, DailySummaryResult, PostQuestionResult (+6 more)

### Community 6 - "Copy And Scoring"
Cohesion: 0.17
Nodes (17): config_text(), ordered_difficulties(), rules_text(), calculate_score(), challenge_cost(), challenge_reward(), _normalize_difficulty(), parse_challenge_economy() (+9 more)

### Community 7 - "Project Verification Docs"
Cohesion: 0.17
Nodes (13): Local Verification, Flask Bot, Kvizi, Telegram Forum Group Topics, Local and Deployment Tooling, PythonAnywhere Prerelease Status, Release Notes, v1 Local Prerelease (+5 more)

### Community 8 - "State Export Script"
Cohesion: 0.31
Nodes (9): export_state(), Any, Connection, Path, _select_all(), _table_names(), _default_output_path(), main() (+1 more)

### Community 9 - "Runtime Cron Concepts"
Cohesion: 0.20
Nodes (11): /cron/backup, /cron/daily, cron-job.org, /cron/maintenance, /cron/tick, SQLite WAL Mode, Weighted Topic Routing, Bot Runtime (+3 more)

### Community 10 - "Persona AI Backlog"
Cohesion: 0.20
Nodes (10): AI-Flavored Kvizi Phrases, Factual Quiz Generation Guardrail, xtekky/gpt4free g4f, KVIZI_AI_ENABLED, Kvizi Backlog, /kvizi_hype Command, Timeout and Static Fallback, Admin Commands (+2 more)

### Community 11 - "Deployment Setup"
Cohesion: 0.22
Nodes (10): Bound Telegram Topics, Deployment Environment Variables, /health Endpoint, Kvizi Deployment, PythonAnywhere Setup, scripts/set_webhook.py, State Export, Telegram Setup (+2 more)

### Community 12 - "Gameplay Scoring"
Cohesion: 0.25
Nodes (8): Safe Structured Context, Challenge Questions, x2/x3 Inline Bets, Non-Negative Score Floor, poll_answer Scoring, Telegram Quiz Poll, Scoring Rules, Gameplay

### Community 13 - "Question CSV Operations"
Cohesion: 0.25
Nodes (8): Question CSV Workflow, Question Backups, Telegram Question Upload Workflow, questions.csv, State Export, Single Correct Answer Constraint, scripts/validate_questions.py, Question Operations

### Community 14 - "Cron Endpoint Setup"
Cohesion: 0.33
Nodes (6): /cron/backup, /cron/daily, cron-job.org, /cron/maintenance, X-Kvizi-Cron-Secret Header, /cron/tick

### Community 15 - "AI Provider Interface"
Cohesion: 1.00
Nodes (3): kvizi/ai.py, No-Op Fallback Provider, AI Provider Interface

## Knowledge Gaps
- **25 isolated node(s):** `Kvizi Backlog`, `xtekky/gpt4free g4f`, `KVIZI_AI_ENABLED`, `/kvizi_hype Command`, `Deployment Environment Variables` (+20 more)
  These have ≤1 connection - possible missing edges or undocumented components.
- **2 thin communities (<3 nodes) omitted from report** — run `graphify query` to explore isolated nodes.

## Suggested Questions
_Questions this graph is uniquely positioned to answer:_

- **Why does `KviziRepository` connect `SQLite Repository State` to `Service Test Harness`, `Bot Service Orchestration`, `App Config Reports`, `Question Bank Routing`, `Copy And Scoring`?**
  _High betweenness centrality (0.202) - this node is a cross-community bridge._
- **Why does `KviziService` connect `Bot Service Orchestration` to `Service Test Harness`, `App Config Reports`, `SQLite Repository State`, `Telegram API Client`, `Question Bank Routing`?**
  _High betweenness centrality (0.191) - this node is a cross-community bridge._
- **Why does `TelegramClient` connect `Telegram API Client` to `Bot Service Orchestration`, `App Config Reports`, `Question Bank Routing`?**
  _High betweenness centrality (0.080) - this node is a cross-community bridge._
- **Are the 11 inferred relationships involving `KviziRepository` (e.g. with `ScoreInput` and `ScoreResult`) actually correct?**
  _`KviziRepository` has 11 INFERRED edges - model-reasoned connections that need verification._
- **Are the 12 inferred relationships involving `KviziService` (e.g. with `Settings` and `KviziRepository`) actually correct?**
  _`KviziService` has 12 INFERRED edges - model-reasoned connections that need verification._
- **Are the 7 inferred relationships involving `TelegramClient` (e.g. with `BackupExportResult` and `DailySummaryResult`) actually correct?**
  _`TelegramClient` has 7 INFERRED edges - model-reasoned connections that need verification._
- **What connects `Kvizi Backlog`, `xtekky/gpt4free g4f`, `Factual Quiz Generation Guardrail` to the rest of the system?**
  _31 weakly-connected nodes found - possible documentation gaps or missing edges._