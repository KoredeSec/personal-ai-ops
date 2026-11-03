"""Container healthcheck: database reachable AND heartbeat fresh. Exit 0 = healthy."""

import sys

from app.core.config import get_settings
from app.database.session import check_database
from app.services.heartbeat import heartbeat_age_seconds


def main() -> int:
    s = get_settings()
    try:
        check_database()
    except Exception as exc:
        print(f"unhealthy: database: {type(exc).__name__}")
        return 1
    age = heartbeat_age_seconds(s.heartbeat_file)
    if age is None:
        print("unhealthy: no heartbeat yet")
        return 1
    if age > s.heartbeat_max_age_seconds:
        print(f"unhealthy: heartbeat stale ({age:.0f}s)")
        return 1
    print(f"healthy: heartbeat {age:.0f}s ago")
    return 0


if __name__ == "__main__":
    sys.exit(main())
