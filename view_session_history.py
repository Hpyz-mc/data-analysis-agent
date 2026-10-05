"""Print every saved turn for one local-development session.

Run from the project root:
    python view_session_history.py <session_id>
"""

import argparse
import json
from uuid import UUID

from app.core.config import ConfigurationError, get_settings
from app.session.store import SessionNotFoundError, SessionStorageError, SessionStore


def main() -> None:
    parser = argparse.ArgumentParser(description="查看某个 session_id 的全部对话历史")
    parser.add_argument("session_id", type=UUID, help="分析接口返回的 session_id")
    args = parser.parse_args()

    try:
        settings = get_settings()
        if not settings.session_database_url:
            parser.error("请先在项目 .env 中配置 SESSION_DATABASE_URL")
        turns = SessionStore(settings.session_database_url).get_history(
            args.session_id, limit=None
        )
    except (ConfigurationError, SessionNotFoundError, SessionStorageError) as exc:
        parser.exit(1, f"读取失败：{exc}\n")

    print(json.dumps(
        {"session_id": str(args.session_id), "turns": turns},
        ensure_ascii=False, indent=2, default=str,
    ))


if __name__ == "__main__":
    main()
