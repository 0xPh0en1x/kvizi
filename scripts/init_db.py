from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from kvizi.config import load_settings  # noqa: E402
from kvizi.database import KviziRepository  # noqa: E402


def main() -> None:
    settings = load_settings()
    repository = KviziRepository(settings.database_path)
    repository.init_db()
    print(f"SQLite initialized: {settings.database_path}")


if __name__ == "__main__":
    main()
