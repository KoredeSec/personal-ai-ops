"""Manual backup / restore.

    python -m scripts.backup_db                 # create a backup now
    python -m scripts.backup_db --list          # list backups (newest first)
    python -m scripts.backup_db --restore PATH  # restore (stop the bot first!)
"""

import argparse
import sys
from pathlib import Path

from app.core.config import get_settings
from app.services.backup import BackupError, create_backup, list_backups, restore_backup


def main() -> int:
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--list", action="store_true")
    g.add_argument("--restore", type=Path)
    p.add_argument("--yes", action="store_true", help="skip restore confirmation")
    args = p.parse_args()
    s = get_settings()
    try:
        if args.list:
            for b in list_backups(s.backup_dir):
                print(f"{b}  {b.stat().st_size} bytes")
            return 0
        if args.restore:
            if not args.yes:
                answer = input(f"Restore {args.restore}? The bot MUST be stopped. Type 'restore': ")
                if answer.strip() != "restore":
                    print("Aborted.")
                    return 1
            safety = restore_backup(s, args.restore)
            print(f"Restored. Previous database saved as {safety}")
            return 0
        print(f"Backup created: {create_backup(s)}")
        return 0
    except BackupError as exc:
        print(f"FAILED: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
