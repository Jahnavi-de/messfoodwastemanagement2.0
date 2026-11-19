"""
Settings and database access for the backend.

Settings are read, in this order, from:
  1. real environment variables
  2. a `.env` file in this folder (copy .env.example -> .env)
  3. the defaults below, which match XAMPP's MySQL on a laptop (user root, no password)
"""

import os
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import pymysql
from flask import g

HERE = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv(os.path.join(HERE, ".env"))


def env(key, default=None):
    value = os.environ.get(key)
    return default if value in (None, "") else value


APP_TZ = ZoneInfo(env("APP_TIMEZONE", "Asia/Kolkata"))


def now_local():
    """Current kitchen time (naive datetime), so a UTC cloud server still uses IST dates."""
    return datetime.now(APP_TZ).replace(tzinfo=None)


def ssl_options():
    """
    Cloud databases (TiDB Cloud, Azure, Aiven ...) only accept encrypted connections.
    DB_SSL=true uses the standard certificate list that comes with Python (certifi);
    DB_SSL_CA=path uses a certificate file your provider gave you.
    """
    ca = env("DB_SSL_CA")
    if not ca and str(env("DB_SSL", "")).lower() not in ("1", "true", "yes", "on"):
        return None
    if not ca:
        import certifi  # installed together with requests
        ca = certifi.where()
    return {"ca": ca}


def connect(with_database=True):
    conn = pymysql.connect(
        host=env("DB_HOST", "localhost"),
        port=int(env("DB_PORT", 3306)),
        user=env("DB_USER", "root"),
        password=env("DB_PASS", ""),
        database=env("DB_NAME", "nextjs_project") if with_database else None,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
        ssl=ssl_options(),
        connect_timeout=10,
    )
    with conn.cursor() as cur:
        # Show/insert times in kitchen time (IST) even when the server runs in UTC.
        cur.execute("SET time_zone = %s", (env("DB_TIMEZONE", "+05:30"),))
    return conn


def create_database_if_missing():
    """A new cloud database server starts empty: create DB_NAME on it if needed."""
    name = env("DB_NAME", "nextjs_project")
    if not re.fullmatch(r"[A-Za-z0-9_]+", name):
        raise ValueError("DB_NAME may only contain letters, digits and _")
    conn = connect(with_database=False)
    try:
        with conn.cursor() as cur:
            cur.execute(f"CREATE DATABASE IF NOT EXISTS `{name}`")
    finally:
        conn.close()


def connect_or_create():
    """connect(), creating the database first if the server says it doesn't exist."""
    try:
        return connect()
    except pymysql.err.OperationalError as e:
        if e.args and e.args[0] == 1049:  # Unknown database
            create_database_if_missing()
            return connect()
        raise


def get_db():
    """One connection per web request, closed automatically afterwards."""
    if "db" not in g:
        g.db = connect()
    return g.db


def close_db(_exc=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def query(sql, params=None, one=False):
    with get_db().cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone() if one else cur.fetchall()


def execute(sql, params=None):
    """Run INSERT/UPDATE/DELETE; returns (rowcount, lastrowid)."""
    with get_db().cursor() as cur:
        count = cur.execute(sql, params)
        return count, cur.lastrowid


def ensure_schema(conn=None):
    """Create any missing tables (never changes existing ones). Safe to run every start."""
    own = conn is None
    conn = conn or connect()
    try:
        with open(os.path.join(HERE, "sql", "schema.sql"), encoding="utf-8") as f:
            sql = "\n".join(line for line in f if not line.lstrip().startswith("--"))
        with conn.cursor() as cur:
            for statement in (s.strip() for s in sql.split(";")):
                if statement:
                    cur.execute(statement)
            # Databases created before roles existed: keep old accounts working as admins.
            cur.execute("SHOW COLUMNS FROM users LIKE 'role'")
            if not cur.fetchall():
                cur.execute("ALTER TABLE users ADD COLUMN role ENUM('admin','student') NOT NULL DEFAULT 'admin'")
    finally:
        if own:
            conn.close()


DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{3,64}$")
