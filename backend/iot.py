"""
IoT endpoints: the Raspberry Pi uploads sensor readings here, and the website reads them back.

    POST /sensor_ingest                     Pi upload (headers X-Device-Id, X-Api-Key)
    GET  /sensor_data?action=status         devices online/offline + latest reading per sensor
    GET  /sensor_data?action=meals&days=7   per-meal waste, leftovers, raw material, temp/humidity
    GET  /prediction_context?date=...&meal=lunch   values to auto-fill the prediction form
"""

import hashlib
import hmac
import math
import re
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

import pymysql
from flask import Blueprint, jsonify, request

from db import env, get_db, now_local, query

iot = Blueprint("iot", __name__)

SENSOR_UNITS = {
    "waste_weight": "kg",
    "leftover_weight": "kg",
    "raw_material_weight": "kg",
    "temperature": "C",
    "humidity": "%",
}
# Anything outside these ranges is a wiring or calibration error.
VALUE_LIMITS = {
    "waste_weight": (-5.0, 1000.0),
    "leftover_weight": (-5.0, 1000.0),
    "raw_material_weight": (-5.0, 1000.0),
    "temperature": (-20.0, 80.0),
    "humidity": (0.0, 100.0),
}
MEAL_TYPES = ("breakfast", "lunch", "dinner", "other")
MEAL_ORDER = {m: i for i, m in enumerate(MEAL_TYPES)}
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
MAX_READINGS_PER_REQUEST = 500
# Used when a device sends no meal_type (e.g. a microcontroller in the Wokwi simulator).
# Same defaults as meal_windows in iot-device/config.example.json.
MEAL_WINDOWS = (("breakfast", "05:00", "10:00"), ("lunch", "10:00", "16:00"), ("dinner", "16:00", "24:00"))

UID_RE = re.compile(r"[A-Za-z0-9-]{8,64}")
NAME_RE = re.compile(r"[a-z0-9_]{1,40}")
ITEM_RE = re.compile(r"[a-z0-9_]{1,30}")


def rnd(value, places=2):
    """Round half up (2.345 -> 2.35) like PHP's round(); Python's built-in round() can give 2.34."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def meal_for(moment: datetime) -> str:
    hm = moment.strftime("%H:%M")
    for name, start, end in MEAL_WINDOWS:
        if start <= hm < end:
            return name
    return "other"


def parse_date(value):
    try:
        return datetime.strptime(str(value), "%Y-%m-%d").date()
    except ValueError:
        return None


# --------------------------------------------------------------------------- summaries

def meal_summary(date_from: date, date_to: date) -> list:
    """
    Per-meal totals measured by the sensors between two dates (inclusive), newest first.

    Weight sensors report the weight currently on the scale, so a meal's amount comes
    from the change between consecutive readings of the same scale:
      * waste / leftover bins : every increase is food thrown in (a big drop = bin emptied, ignored)
      * raw-material stock    : every decrease is material taken for cooking (increase = restock, ignored)
    Changes smaller than SENSOR_NOISE_KG are scale noise.
    """
    noise = float(env("SENSOR_NOISE_KG", 0.05))
    # Look a little outside the range so the first reading in it still has a previous one.
    scan_from = (date_from - timedelta(days=2)).isoformat()
    scan_to = (date_to + timedelta(days=2)).isoformat()
    meals = {}

    def meal(day, meal_type):
        key = (str(day), meal_type)
        if key not in meals:
            meals[key] = {
                "date": str(day), "meal_type": meal_type,
                "waste_kg": None, "leftover_kg": None,
                "raw_material_kg": {}, "raw_material_total_kg": None,
                "temperature_avg": None, "humidity_avg": None,
            }
        return meals[key]

    weight_rows = query(
        """
        SELECT meal_date, meal_type, sensor_type, item,
               SUM(CASE
                     WHEN sensor_type IN ('waste_weight', 'leftover_weight') AND delta >= %s THEN delta
                     WHEN sensor_type = 'raw_material_weight' AND -delta >= %s THEN -delta
                     ELSE 0
                   END) AS amount
        FROM (
            SELECT meal_date, meal_type, sensor_type, item,
                   value - LAG(value) OVER (PARTITION BY device_id, sensor_name, sensor_type
                                            ORDER BY recorded_at, id) AS delta
            FROM sensor_readings
            WHERE sensor_type IN ('waste_weight', 'leftover_weight', 'raw_material_weight')
              AND recorded_at >= %s AND recorded_at < %s
        ) w
        WHERE meal_date BETWEEN %s AND %s
        GROUP BY meal_date, meal_type, sensor_type, item
        """,
        (noise, noise, scan_from, scan_to, date_from.isoformat(), date_to.isoformat()),
    )
    for row in weight_rows:
        m = meal(row["meal_date"], row["meal_type"])
        amount = rnd(float(row["amount"] or 0), 2)
        if row["sensor_type"] == "waste_weight":
            m["waste_kg"] = rnd((m["waste_kg"] or 0) + amount, 2)
        elif row["sensor_type"] == "leftover_weight":
            m["leftover_kg"] = rnd((m["leftover_kg"] or 0) + amount, 2)
        else:
            item = row["item"] or "unlabelled"
            m["raw_material_kg"][item] = rnd(m["raw_material_kg"].get(item, 0) + amount, 2)
            m["raw_material_total_kg"] = rnd((m["raw_material_total_kg"] or 0) + amount, 2)

    env_rows = query(
        """
        SELECT meal_date, meal_type, sensor_type, AVG(value) AS avg_value
        FROM sensor_readings
        WHERE sensor_type IN ('temperature', 'humidity') AND meal_date BETWEEN %s AND %s
        GROUP BY meal_date, meal_type, sensor_type
        """,
        (date_from.isoformat(), date_to.isoformat()),
    )
    for row in env_rows:
        m = meal(row["meal_date"], row["meal_type"])
        field = "temperature_avg" if row["sensor_type"] == "temperature" else "humidity_avg"
        m[field] = rnd(float(row["avg_value"]), 1)

    result = list(meals.values())
    result.sort(key=lambda m: MEAL_ORDER[m["meal_type"]])
    result.sort(key=lambda m: m["date"], reverse=True)
    return result


def latest_environment(minutes: int = 60) -> dict:
    """Latest temperature / humidity (any device) from the last `minutes` minutes."""
    out = {"temperature": None, "humidity": None, "recorded_at": None}
    rows = query(
        """
        SELECT r.sensor_type, r.value, r.recorded_at
        FROM sensor_readings r
        JOIN (
            SELECT sensor_type, MAX(recorded_at) AS latest
            FROM sensor_readings
            WHERE sensor_type IN ('temperature', 'humidity')
              AND recorded_at >= NOW() - INTERVAL %s MINUTE
            GROUP BY sensor_type
        ) m ON m.sensor_type = r.sensor_type AND m.latest = r.recorded_at
        """,
        (minutes,),
    )
    for row in rows:
        out[row["sensor_type"]] = rnd(float(row["value"]), 1)
        stamp = row["recorded_at"].strftime("%Y-%m-%d %H:%M:%S")
        out["recorded_at"] = max(out["recorded_at"] or "", stamp)
    return out


# --------------------------------------------------------------------------- upload from the Pi

def authenticate_device():
    """Returns (device_row, None) or (None, error_response)."""
    device_id = (request.headers.get("X-Device-Id") or "").strip()
    api_key = (request.headers.get("X-Api-Key") or "").strip()
    if not device_id or not api_key:
        return None, (jsonify(success=False, message="Missing X-Device-Id or X-Api-Key header"), 401)
    device = query(
        "SELECT id, device_id, name, api_key_hash, is_active FROM iot_devices WHERE device_id = %s",
        (device_id,), one=True,
    )
    if not device or not hmac.compare_digest(device["api_key_hash"], hashlib.sha256(api_key.encode()).hexdigest()):
        return None, (jsonify(success=False, message="Unknown device or wrong API key"), 401)
    if int(device["is_active"]) != 1:
        return None, (jsonify(success=False, message="Device is disabled"), 403)
    return device, None


def validate_reading(r):
    """Returns (clean_reading, None) or (None, error message)."""
    if not isinstance(r, dict):
        return None, "reading must be an object"
    uid = str(r.get("uid") or "")
    if not UID_RE.fullmatch(uid):
        return None, "uid must be 8-64 letters, digits or dashes"
    sensor = str(r.get("sensor") or "")
    if not NAME_RE.fullmatch(sensor):
        return None, "sensor must be 1-40 lowercase letters, digits or _"
    sensor_type = str(r.get("type") or "")
    if sensor_type not in SENSOR_UNITS:
        return None, "type must be one of: " + ", ".join(SENSOR_UNITS)
    item = str(r.get("item") or "").strip().lower()
    if item and not ITEM_RE.fullmatch(item):
        return None, "item must be 1-30 lowercase letters, digits or _"
    raw_value = r.get("value")
    try:
        if isinstance(raw_value, bool):
            raise ValueError
        value = float(raw_value)
        if not math.isfinite(value):
            raise ValueError
    except (TypeError, ValueError):
        return None, "value must be a number"
    value = rnd(value, 3)
    low, high = VALUE_LIMITS[sensor_type]
    if not low <= value <= high:
        return None, f"value {value:g} is outside {low:g}..{high:g} for {sensor_type} (check wiring/calibration)"
    recorded_at = str(r.get("recorded_at") or "")
    if recorded_at:
        try:
            recorded = datetime.strptime(recorded_at, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None, "recorded_at must look like 2026-11-09 13:05:12"
        if recorded < datetime(2020, 1, 1) or recorded > now_local() + timedelta(days=1):
            return None, "recorded_at is not a plausible time (is the Pi clock set?)"
    else:  # device without a clock: use the time the reading arrived
        recorded = now_local().replace(microsecond=0)
        recorded_at = recorded.strftime("%Y-%m-%d %H:%M:%S")
    meal_type = str(r.get("meal_type") or meal_for(recorded)).lower()
    if meal_type not in MEAL_TYPES:
        return None, "meal_type must be one of: " + ", ".join(MEAL_TYPES)
    meal_date = str(r.get("meal_date") or recorded_at[:10])
    if parse_date(meal_date) is None:
        return None, "meal_date must look like 2026-11-09"
    return {
        "uid": uid, "sensor": sensor, "type": sensor_type,
        "item": item if sensor_type == "raw_material_weight" else "",
        "value": value, "unit": SENSOR_UNITS[sensor_type],
        "meal_date": meal_date, "meal_type": meal_type, "recorded_at": recorded_at,
    }, None


@iot.post("/sensor_ingest")
def sensor_ingest():
    """
    Body: {"readings": [{"uid", "sensor", "type", "item", "value", "recorded_at", "meal_date", "meal_type"}]}
    recorded_at / meal_date / meal_type are optional (server time and MEAL_WINDOWS are used).
    An empty list is a ping. Accepted uids (new or already stored) and rejected ones are both
    returned so the Pi can clear them from its offline queue. Retries never create duplicates.
    """
    device, error = authenticate_device()
    if error:
        return error
    if (request.content_length or 0) > 1_000_000:
        return jsonify(success=False, message=f"Request too large; send at most {MAX_READINGS_PER_REQUEST} readings"), 413
    data = request.get_json(force=True, silent=True)
    if not isinstance(data, dict) or not isinstance(data.get("readings"), list):
        return jsonify(success=False, message='Body must be JSON: {"readings": [...]}'), 400
    if len(data["readings"]) > MAX_READINGS_PER_REQUEST:
        return jsonify(success=False, message=f"Send at most {MAX_READINGS_PER_REQUEST} readings per request"), 413

    accepted, rejected, stored = [], [], 0
    conn = get_db()
    try:
        conn.begin()
        with conn.cursor() as cur:
            for r in data["readings"]:
                clean, problem = validate_reading(r)
                if problem:
                    rejected.append({"uid": str(r.get("uid") or "") if isinstance(r, dict) else "", "error": problem})
                    continue
                stored += cur.execute(
                    """INSERT IGNORE INTO sensor_readings
                         (device_id, reading_uid, sensor_name, sensor_type, item, value, unit,
                          meal_date, meal_type, recorded_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (device["device_id"], clean["uid"], clean["sensor"], clean["type"], clean["item"],
                     clean["value"], clean["unit"], clean["meal_date"], clean["meal_type"], clean["recorded_at"]),
                )  # 0 means this uid was already stored by an earlier retry
                accepted.append(clean["uid"])
            cur.execute(
                "UPDATE iot_devices SET last_seen_at = NOW(), last_ip = %s WHERE id = %s",
                (request.remote_addr, device["id"]),
            )
        conn.commit()
    except pymysql.err.MySQLError:
        conn.rollback()
        return jsonify(success=False, message="Could not store readings, retry later"), 500

    return jsonify(
        success=True,
        device_id=device["device_id"],
        stored=stored,
        duplicates=len(accepted) - stored,
        accepted=accepted,
        rejected=rejected,
        server_time=now_local().strftime("%Y-%m-%d %H:%M:%S"),
    )


# --------------------------------------------------------------------------- dashboard

@iot.get("/sensor_data")
def sensor_data():
    action = request.args.get("action", "status")

    if action == "status":
        offline_after = int(env("DEVICE_OFFLINE_AFTER_S", 180))
        devices = []
        for row in query(
            """SELECT device_id, name, location, is_active, last_seen_at,
                      TIMESTAMPDIFF(SECOND, last_seen_at, NOW()) AS seconds_since_seen
               FROM iot_devices ORDER BY name"""
        ):
            seconds = row["seconds_since_seen"]
            active = int(row["is_active"]) == 1
            devices.append({
                "device_id": row["device_id"], "name": row["name"], "location": row["location"],
                "is_active": active, "last_seen_at": row["last_seen_at"],
                "seconds_since_seen": None if seconds is None else int(seconds),
                "online": active and seconds is not None and seconds <= offline_after,
            })
        sensors = query(
            """SELECT r.device_id, r.sensor_name, r.sensor_type, r.item, r.value, r.unit, r.meal_type, r.recorded_at
               FROM sensor_readings r
               JOIN (SELECT MAX(id) AS id FROM sensor_readings GROUP BY device_id, sensor_name, sensor_type) latest
                 ON latest.id = r.id
               ORDER BY r.device_id, r.sensor_type, r.sensor_name"""
        )
        return jsonify(
            success=True,
            server_time=now_local().strftime("%Y-%m-%d %H:%M:%S"),
            devices=devices,
            sensors=sensors,
            environment=latest_environment(60),
        )

    if action == "meals":
        date_from, date_to = parse_date(request.args.get("from", "")), parse_date(request.args.get("to", ""))
        if request.args.get("from") is None or request.args.get("to") is None:
            try:
                days = max(1, min(90, int(request.args.get("days", 7))))
            except ValueError:
                days = 7
            date_to = now_local().date()
            date_from = date_to - timedelta(days=days - 1)
        if date_from is None or date_to is None or date_from > date_to:
            return jsonify(success=False, message="from/to must be dates like 2026-11-01, from <= to"), 422
        if (date_to - date_from).days > 366:
            return jsonify(success=False, message="Ask for at most one year at a time"), 422

        meals = meal_summary(date_from, date_to)
        daily = {}
        day = date_from
        while day <= date_to:
            daily[day.isoformat()] = {"date": day.isoformat(), "waste_kg": None, "leftover_kg": None,
                                      "raw_material_total_kg": None}
            day += timedelta(days=1)
        for m in meals:
            for field in ("waste_kg", "leftover_kg", "raw_material_total_kg"):
                if m[field] is not None:
                    daily[m["date"]][field] = rnd((daily[m["date"]][field] or 0) + m[field], 2)
        return jsonify(success=True, **{"from": date_from.isoformat(), "to": date_to.isoformat()},
                       meals=meals, daily=list(daily.values()))

    return jsonify(success=False, message="Unknown action. Use action=status or action=meals"), 404


@iot.get("/prediction_context")
def prediction_context():
    """
    Suggests prediction-form values from data already in the database:
      studentsEnrolled        number of student accounts
      averageAttendance       % of students who marked attendance that day,
                              or the average of the previous 7 days if nobody has marked yet
      leftoverFromPreviousDay leftovers weighed by the leftover sensor the day before
    plus current kitchen temperature/humidity and yesterday's measured waste.
    A field is null when there is no data; the form then keeps what the user typed.
    """
    day = parse_date(request.args.get("date") or now_local().date().isoformat())
    if day is None:
        return jsonify(success=False, message="date must look like 2026-11-10"), 422
    meal = (request.args.get("meal") or "lunch").lower()
    previous_day = day - timedelta(days=1)

    fields = {
        "dayOfWeek": DAY_NAMES[day.weekday()],
        "studentsEnrolled": None,
        "averageAttendance": None,
        "leftoverFromPreviousDay": None,
    }
    sources = {}

    enrolled = int(query("SELECT COUNT(*) AS n FROM users WHERE role = 'student'", one=True)["n"])
    if enrolled > 0:
        fields["studentsEnrolled"] = enrolled
        sources["studentsEnrolled"] = "student accounts"
        present = int(query(
            """SELECT COUNT(*) AS n FROM attendance a JOIN users u ON u.id = a.user_id
               WHERE u.role = 'student' AND a.attendance_date = %s""",
            (day.isoformat(),), one=True,
        )["n"])
        if present > 0:
            fields["averageAttendance"] = rnd(min(100, present * 100 / enrolled), 1)
            sources["averageAttendance"] = f"{present} of {enrolled} students marked attendance on {day}"
        else:
            counts = [int(r["n"]) for r in query(
                """SELECT a.attendance_date, COUNT(*) AS n FROM attendance a JOIN users u ON u.id = a.user_id
                   WHERE u.role = 'student' AND a.attendance_date BETWEEN %s AND %s
                   GROUP BY a.attendance_date""",
                ((day - timedelta(days=7)).isoformat(), previous_day.isoformat()),
            )]
            if counts:
                avg = sum(counts) / len(counts)
                fields["averageAttendance"] = rnd(min(100, avg * 100 / enrolled), 1)
                sources["averageAttendance"] = (
                    f"average of the last {len(counts)} day(s) with attendance (none marked yet for {day})"
                )

    yesterday = {"waste_kg": None, "leftover_kg": None}
    for m in meal_summary(previous_day, previous_day):
        for field in ("waste_kg", "leftover_kg"):
            if m[field] is not None:
                yesterday[field] = rnd((yesterday[field] or 0) + m[field], 2)
    if yesterday["leftover_kg"] is not None:
        fields["leftoverFromPreviousDay"] = yesterday["leftover_kg"]
        sources["leftoverFromPreviousDay"] = f"leftover sensor, {previous_day}"

    return jsonify(
        success=True,
        date=day.isoformat(),
        meal=meal,
        fields=fields,
        sources=sources,
        previous_day={"date": previous_day.isoformat(), **yesterday},
        environment=latest_environment(60),
    )
