#!/bin/sh
set -eu
# Apply pending migrations explicitly before starting (never auto-create tables).
alembic upgrade head
exec "$@"
