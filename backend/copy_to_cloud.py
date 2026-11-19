"""
Copy all your data from the local XAMPP database to the cloud database.

    1. Put the cloud database details in backend/.env (DB_HOST, DB_PORT, DB_USER,
       DB_PASS, DB_NAME, DB_SSL=true).
    2. Keep XAMPP's MySQL running, then:
           python copy_to_cloud.py
       (if your XAMPP MySQL has a password: python copy_to_cloud.py --from-pass yourpassword)

It creates the database and tables in the cloud if needed, then copies users,
attendance, waste_predictions, iot_devices and sensor_readings with the same ids.
Running it again only adds rows that are not in the cloud yet, so it is safe to repeat.
"""

import argparse
import sys

import pymysql

from db import connect_or_create, ensure_schema, env

# Parents before children, so foreign keys are always satisfied.
TABLES = ["users", "attendance", "waste_predictions", "iot_devices", "sensor_readings"]
BATCH = 500


def main():
    p = argparse.ArgumentParser(description="Copy local XAMPP data to the cloud database in .env")
    p.add_argument("--from-host", default="localhost")
    p.add_argument("--from-port", type=int, default=3306)
    p.add_argument("--from-user", default="root")
    p.add_argument("--from-pass", default="")
    p.add_argument("--from-db", default="nextjs_project")
    args = p.parse_args()

    if env("DB_HOST", "localhost") in ("localhost", "127.0.0.1") and env("DB_NAME", "nextjs_project") == args.from_db:
        sys.exit("backend/.env still points at your local database. Put the cloud database details in .env first.")

    try:
        src = pymysql.connect(
            host=args.from_host, port=args.from_port, user=args.from_user, password=args.from_pass,
            database=args.from_db, charset="utf8mb4", cursorclass=pymysql.cursors.Cursor,
        )
    except pymysql.err.OperationalError as e:
        sys.exit(f"Cannot open the local database ({e}). Is MySQL running in XAMPP?")
    try:
        dst = connect_or_create()
    except pymysql.err.OperationalError as e:
        sys.exit(f"Cannot connect to the cloud database ({e}). Check DB_HOST/DB_PORT/DB_USER/DB_PASS/DB_SSL in .env")

    timezone = env("DB_TIMEZONE", "+05:30")
    with src.cursor() as cur:
        cur.execute("SET time_zone = %s", (timezone,))  # same clock on both sides
    ensure_schema(dst)
    print(f"Copying {args.from_db} on {args.from_host}  ->  {env('DB_NAME', 'nextjs_project')} on {env('DB_HOST')}\n")

    for table in TABLES:
        with src.cursor() as cur:
            cur.execute("SHOW TABLES LIKE %s", (table,))
            if not cur.fetchone():
                print(f"  {table:<18} not in the local database, skipped")
                continue
        with dst.cursor() as cur:
            cur.execute(f"SHOW COLUMNS FROM `{table}`")
            cloud_columns = {row["Field"] for row in cur.fetchall()}

        with src.cursor() as cur:
            cur.execute(f"SELECT * FROM `{table}`")
            columns = [c[0] for c in cur.description if c[0] in cloud_columns]
            keep = [i for i, c in enumerate(cur.description) if c[0] in cloud_columns]
            rows = [tuple(r[i] for i in keep) for r in cur.fetchall()]

        sql = (f"INSERT IGNORE INTO `{table}` ({', '.join(f'`{c}`' for c in columns)}) "
               f"VALUES ({', '.join(['%s'] * len(columns))})")
        added = 0
        with dst.cursor() as cur:
            for start in range(0, len(rows), BATCH):
                added += cur.executemany(sql, rows[start:start + BATCH]) or 0
        print(f"  {table:<18} {len(rows):>7} rows locally, {added:>7} new in the cloud")

    with dst.cursor() as cur:
        print("\nRows now in the cloud database:")
        for table in TABLES:
            cur.execute(f"SELECT COUNT(*) AS n FROM `{table}`")
            print(f"  {table:<18} {cur.fetchone()['n']:>7}")
    src.close()
    dst.close()
    print("\nDone. Restart the backend (python app.py) so it uses the cloud database.")


if __name__ == "__main__":
    main()
