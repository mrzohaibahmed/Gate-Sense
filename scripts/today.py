"""Print a day's attendance: name, shift, first IN, last OUT, worked hours, late / early leave / overtime minutes and
the status.

Computed from the raw events with the same rule as the dashboard and the reports (app.db.roster_days): one line per
roster day (a shift or OFF), plus people with passes but no roster row (unscheduled). A night shift is the day it
starts, so its OUT the next morning is on that line.

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
from app.shifts import OFF, OFF_DAY  # noqa: E402


def hhmm(dt) -> str:
    return dt.strftime("%H:%M:%S") if dt else "-"


def minutes(value: int | None) -> str:
    return str(value) if value else "-"


def main() -> int:
    parser = argparse.ArgumentParser(description="Print GateSense attendance for one day.")
    parser.add_argument("--date", type=date.fromisoformat, default=date.today(), help="YYYY-MM-DD (default today)")
    args = parser.parse_args()

    conn = db.connect()
    days = db.roster_days(conn, args.date, args.date)
    names = {r["employee_code"]: r["name"] for r in conn.execute("SELECT employee_code, name FROM persons")}
    conn.close()

    print(f"GateSense attendance for {args.date.isoformat()}\n")
    if not days:
        print("Nobody is on the roster that day and nobody passed a camera. Plan shifts on the Roster page.")
        return 0
    print(f"{'Name':<20} {'Shift':<10} {'First IN':<9} {'Last OUT':<9} {'Hours':>6} {'Late':>5} {'Early':>5} "
          f"{'Over':>5}  Status")
    print("-" * 90)
    for d in sorted(days, key=lambda d: names.get(d.person, d.person).lower()):
        hours = f"{d.worked_minutes / 60:.2f}" if d.worked_minutes is not None else "-"
        shift = d.shift or (OFF if d.status == OFF_DAY else "-")
        print(f"{names.get(d.person, d.person):<20} {shift:<10} {hhmm(d.first_in):<9} {hhmm(d.last_out):<9} "
              f"{hours:>6} {minutes(d.late_minutes):>5} {minutes(d.early_leave_minutes):>5} "
              f"{minutes(d.overtime_minutes):>5}  {d.status}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
