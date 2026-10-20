"""Print a day's attendance: name, first IN, last OUT, worked hours, missing-exit flag.

Recomputes daily_attendance for that day from the raw events first.

Run from the project root:
    .venv\\Scripts\\python scripts\\today.py
    .venv\\Scripts\\python scripts\\today.py --date 2026-10-20
"""

import argparse
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.attendance import MISSING_EXIT  # noqa: E402


def hhmm(dt) -> str:
    return dt.strftime("%H:%M:%S") if dt else "-"


def main() -> int:
    parser = argparse.ArgumentParser(description="Print GateSense attendance for one day.")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today(), help="YYYY-MM-DD (default today)")
    args = parser.parse_args()

    conn = db.connect()
    summaries = db.refresh_daily(conn, args.date)
    names = {r["employee_code"]: r["name"] for r in conn.execute("SELECT employee_code, name FROM persons")}
    conn.close()

    print(f"GateSense attendance for {args.date.isoformat()}\n")
    if not summaries:
        print("No one enrolled. Run scripts\\enroll.py first.")
        return 0
    print(f"{'Name':<20} {'First IN':<9} {'Last OUT':<9} {'Hours':>6}  {'Missing exit':<12} Status")
    print("-" * 72)
    for s in summaries:
        hours = f"{s.worked_hours:.2f}" if s.worked_hours is not None else "-"
        flag = "YES" if s.status == MISSING_EXIT else ""
        print(f"{names.get(s.person, s.person):<20} {hhmm(s.first_in):<9} {hhmm(s.last_out):<9} {hours:>6}  {flag:<12} {s.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
