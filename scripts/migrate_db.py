#!/usr/bin/env python3
"""Operator CLI for gateway-database schema migrations (023 US4, T515 — FR-303/304).

    python scripts/migrate_db.py status   # ledger vs shipped files: version, pending, checksums
    python scripts/migrate_db.py apply    # run pending migrations (the gateway also does this
                                          # at startup — this exists for upgrades/diagnosis)

Connection comes from the same env the platform uses (GATEWAY_DB_URL, or POSTGRES_* +
GATEWAY_DB_HOST/PORT/NAME — see platformlib/store.py). No destructive command exists here:
recovery is restore-then-forward-fix (contract §Evolution policy), never an automatic down.

Backup before the FIRST upgrade of populated state (quickstart §US4):

    pg_dump  "$GATEWAY_DB_URL" -Fc -f gateway-backup.dump          # take the backup
    createdb gateway_verify                                        # disposable database
    pg_restore -d gateway_verify gateway-backup.dump               # prove it restores
    psql -d gateway_verify -c 'SELECT count(*) FROM predictions'   # spot-check row counts

(For the Compose Postgres, prefix with:
 docker compose exec postgres — e.g. `docker compose exec postgres pg_dump -U mlops ...`.)
"""
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO not in sys.path:
    sys.path.insert(0, _REPO)

from platformlib import migrations, store  # noqa: E402


def cmd_status() -> int:
    try:
        conn = store.connect(autocommit=False)
    except store.StoreError as e:
        print(f"cannot reach the gateway DB: {e}", file=sys.stderr)
        return 2
    try:
        st = migrations.status(conn)
    finally:
        conn.close()
    print(f"database version : {st['db_version']}")
    print(f"binary current   : {st['current']}")
    print(f"pending          : {', '.join(st['pending']) or '(none)'}")
    for row in st["recorded"]:
        print(f"  applied {row['version']:03d}_{row['name']}  at {row['applied_at']} "
              f"by {row['applied_by']}  ({row['duration_ms']} ms)  {row['checksum'][:12]}…")
    if st["db_version"] > st["current"]:
        print("!! database is NEWER than this checkout — upgrade the binary; do not write",
              file=sys.stderr)
        return 1
    return 0


def cmd_apply() -> int:
    try:
        report = migrations.apply(applied_by=f"migrate_db.py({os.getenv('USER', 'operator')})")
    except (migrations.MigrationError, store.StoreError) as e:
        print(f"migration failed: {e}", file=sys.stderr)
        return 1
    if report["applied"]:
        print(f"applied: {', '.join(report['applied'])} ({report['duration_ms']} ms)")
    else:
        print(f"up to date at version {report['db_version']} — nothing pending")
    return 0


def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "status":
        return cmd_status()
    if cmd == "apply":
        return cmd_apply()
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
