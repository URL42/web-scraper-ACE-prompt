import csv
import sqlite3
from pathlib import Path


def export_table(conn, table, dest_path):
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with dest_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if rows:
            writer.writerow(rows[0].keys())
            writer.writerows(rows)
        else:
            writer.writerow(["empty"])


def main():
    db_path = Path("outputs/monitor/monitor.db")
    if not db_path.exists():
        print(f"Database not found at {db_path}. Run browser_agent monitors first.")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    export_table(conn, "monitors", Path("outputs/monitor/monitors.csv"))
    export_table(conn, "runs", Path("outputs/monitor/runs.csv"))
    print("Exported monitors.csv and runs.csv to outputs/monitor/")


if __name__ == "__main__":
    main()
