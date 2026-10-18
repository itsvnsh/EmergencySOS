"""
audit_log.py - show the latest audit-log events in the terminal.

Every SOS, allocation, rollback, release, admin change and lab run is written
to the audit_log table inside the same transaction as the change it describes.

Usage:
    python audit_log.py          # latest 40 events
    python audit_log.py 100      # latest 100 events
"""
import sys

import db


def main():
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 40
    rows = db.query_all("SELECT created_at, event_type, details FROM audit_log "
                        "ORDER BY log_id DESC LIMIT %s", (max(1, min(limit, 1000)),))
    if not rows:
        print("The audit log is empty.")
        return
    for r in rows:
        print(f"{r['created_at']:%Y-%m-%d %H:%M:%S}  {r['event_type']:<13} {r['details']}")


if __name__ == "__main__":
    main()
