from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def test_local_cron_runs_maintenance_with_temp_state(tmp_path: Path) -> None:
    questions_path = tmp_path / "questions.csv"
    questions_path.write_text(
        "id,topic_key,difficulty,question,option_1,option_2,option_3,option_4,"
        "option_5,option_6,correct_option_ids,explanation,source\n"
        "q1,network,normal,Question?,A,B,C,D,,,1,Because,\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "KVIZI_DB_PATH": str(tmp_path / "kvizi.sqlite3"),
        "KVIZI_QUESTIONS_PATH": str(questions_path),
        "KVIZI_CRON_SECRET": "cron-secret",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_CHAT_ID": "-1001",
    }

    result = subprocess.run(
        [sys.executable, "scripts/local_cron.py", "maintenance"],
        cwd=Path(__file__).resolve().parent.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0
    assert "POST /cron/maintenance -> 200" in result.stdout
    assert '"closed": 0' in result.stdout
