"""Print a day's attendance: name, first IN, last OUT, worked hours, missing-exit and check flags.

Computed from the raw events with the same rule as the dashboard and the reports (app.db.day_summaries):
one line per person, first IN to last OUT; today's days without an OUT yet say "inside".

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
from app.attendance import CHECK, MISSING_EXIT  # noqa: E402
from app.reports import INSIDE  # noqa: E402


def hhmm(dt) -> str:
    return dt.strftime("%H:%M:%S") if dt else "-"


def main() -> int:
    parser = argparse.ArgumentParser(description="Print GateSense attendance for one day.")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today(), help="YYYY-MM-DD (default today)")
    args = parser.parse_args()

    conn = db.connect()
    summaries, _ = db.day_summaries(conn, args.date)
    names = {r["employee_code"]: r["name"] for r in conn.execute("SELECT employee_code, name FROM persons")}
    conn.close()

    print(f"GateSense attendance for {args.date.isoformat()}\n")
    if not summaries:
        print("No one enrolled. Run scripts\\enroll.py first.")
        return 0
    today = args.date == date.today()
    print(f"{'Name':<20} {'First IN':<9} {'Last OUT':<9} {'Hours':>6}  {'Missing exit':<12} {'Check':<5} Status")
    print("-" * 80)
    for s in summaries:
        hours = f"{s.worked_hours:.2f}" if s.worked_hours is not None else "-"
        flag = "YES" if s.status == MISSING_EXIT and not today else ""
        check = "YES" if s.status == CHECK else ""
        status = INSIDE if s.status == MISSING_EXIT and today else s.status
        print(f"{names.get(s.person, s.person):<20} {hhmm(s.first_in):<9} {hhmm(s.last_out):<9} {hours:>6}  "
              f"{flag:<12} {check:<5} {status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
