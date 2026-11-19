"""
Command-line tools for the backend (run them in the backend folder).

    python manage.py init-db                                   create any missing tables
    python manage.py add-device <device_id> "<name>" ["<location>"]   register a Pi, prints its API key
    python manage.py list-devices
    python manage.py rotate-key <device_id>                    new API key for a Pi
    python manage.py disable <device_id>   /   enable <device_id>

The API key is printed once; only its SHA-256 hash is stored in the database.
"""

import argparse
import hashlib
import secrets
import sys

import pymysql

from db import DEVICE_ID_RE, connect_or_create, ensure_schema


def new_key():
    return secrets.token_hex(24)


def print_key(device_id, key):
    print(f"\nDevice: {device_id}\nAPI key: {key}\n")
    print("Copy both into iot-device/config.json on the Raspberry Pi (or the simulator):")
    print(f'  "device_id": "{device_id}",\n  "api_key": "{key}"\n')
    print("This key is not stored anywhere in readable form. Run rotate-key if you lose it.")


def main():
    p = argparse.ArgumentParser(description="Food waste backend tools")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init-db", help="create any missing tables")
    a = sub.add_parser("add-device", help="register a Raspberry Pi")
    a.add_argument("device_id")
    a.add_argument("name", nargs="?")
    a.add_argument("location", nargs="?")
    sub.add_parser("list-devices")
    for cmd in ("rotate-key", "disable", "enable"):
        sub.add_parser(cmd).add_argument("device_id")
    args = p.parse_args()

    try:
        conn = connect_or_create()
    except pymysql.err.OperationalError as e:
        sys.exit(f"Cannot connect to the database: {e}\nIs MySQL running (XAMPP) and are the DB_* settings in .env right?")
    ensure_schema(conn)

    with conn.cursor() as cur:
        if args.cmd == "init-db":
            print("Tables are ready.")

        elif args.cmd == "list-devices":
            cur.execute("SELECT device_id, name, location, is_active, last_seen_at FROM iot_devices ORDER BY id")
            rows = cur.fetchall()
            if not rows:
                print("No devices registered yet.")
            for r in rows:
                print(f"{r['device_id']:<20} {r['name']:<25} {(r['location'] or '-'):<20} "
                      f"{'active' if r['is_active'] else 'DISABLED':<9} last seen: {r['last_seen_at'] or 'never'}")

        elif args.cmd == "add-device":
            if not DEVICE_ID_RE.fullmatch(args.device_id):
                sys.exit("device_id must be 3-64 characters: letters, numbers, '-' or '_'.")
            key = new_key()
            try:
                cur.execute(
                    "INSERT INTO iot_devices (device_id, name, location, api_key_hash) VALUES (%s, %s, %s, %s)",
                    (args.device_id, args.name or args.device_id, args.location,
                     hashlib.sha256(key.encode()).hexdigest()),
                )
            except pymysql.err.IntegrityError:
                sys.exit(f"Device '{args.device_id}' already exists. Use: python manage.py rotate-key {args.device_id}")
            print_key(args.device_id, key)

        else:
            if args.cmd == "rotate-key":
                key = new_key()
                cur.execute("UPDATE iot_devices SET api_key_hash = %s WHERE device_id = %s",
                            (hashlib.sha256(key.encode()).hexdigest(), args.device_id))
            else:
                cur.execute("UPDATE iot_devices SET is_active = %s WHERE device_id = %s",
                            (1 if args.cmd == "enable" else 0, args.device_id))
            cur.execute("SELECT 1 FROM iot_devices WHERE device_id = %s", (args.device_id,))
            if not cur.fetchone():
                sys.exit(f"No device called '{args.device_id}'. See: python manage.py list-devices")
            if args.cmd == "rotate-key":
                print_key(args.device_id, key)
            else:
                print(f"Device '{args.device_id}' {args.cmd}d.")
    conn.close()


if __name__ == "__main__":
    main()
