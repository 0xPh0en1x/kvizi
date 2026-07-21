from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_smoke_check_passes_with_temp_deploy_env(tmp_path: Path) -> None:
    questions_path = tmp_path / "questions.csv"
    questions_path.write_text(
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "q1,network,normal,Question?,A,B,C,D,,,1,Because,\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "TELEGRAM_BOT_TOKEN": "123456:token",
        "TELEGRAM_CHAT_ID": "-1001",
        "KVIZI_WEBHOOK_SECRET": "webhook-secret",
        "KVIZI_CRON_SECRET": "cron-secret",
        "KVIZI_ADMIN_IDS": "7",
        "KVIZI_DB_PATH": str(tmp_path / "kvizi.sqlite3"),
        "KVIZI_QUESTIONS_PATH": str(questions_path),
    }

    result = subprocess.run(
        [sys.executable, "scripts/smoke_check.py", "--strict-env"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[OK] questions CSV valid: 1 questions" in result.stdout
    assert "[OK] SQLite journal_mode=wal" in result.stdout
    assert "[OK] /health ok, questions=1" in result.stdout
    assert "[OK] /cron/tick rejects missing cron secret" in result.stdout
    assert "Smoke check finished: failures=0" in result.stdout


def test_smoke_check_warns_for_missing_env_without_strict_mode(tmp_path: Path) -> None:
    questions_path = tmp_path / "questions.csv"
    questions_path.write_text(
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "q1,network,normal,Question?,A,B,C,D,,,1,Because,\n",
        encoding="utf-8",
    )
    env = {**os.environ}
    for name in (
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHAT_ID",
        "KVIZI_WEBHOOK_SECRET",
        "KVIZI_CRON_SECRET",
        "KVIZI_ADMIN_IDS",
    ):
        env.pop(name, None)
    env["KVIZI_DB_PATH"] = str(tmp_path / "kvizi.sqlite3")
    env["KVIZI_QUESTIONS_PATH"] = str(questions_path)

    result = subprocess.run(
        [sys.executable, "scripts/smoke_check.py"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "[WARN] /health is not ready because deploy config is incomplete" in result.stdout
    assert "Smoke check finished: failures=0" in result.stdout
