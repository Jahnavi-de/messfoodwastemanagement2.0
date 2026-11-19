"""
Hostel food-waste backend (Python / Flask). Replaces the old PHP files in nextjsbackend/.

Runs the same way on a laptop, a Raspberry Pi or a cloud server:
    pip install -r requirements.txt
    python app.py                                   -> http://0.0.0.0:8000
    gunicorn -w 2 -b 0.0.0.0:8000 app:app           (Raspberry Pi / Linux server)

Endpoints (same JSON answers as the old PHP files):
    POST /login                 (was login.php)
    POST /register              (was register.php)
    GET|POST /attendance        (was attendance.php)  ?action=mark | mine | all
    POST /predict               (was predict.php)     -> forwards to the ML API
    POST /save_prediction       (was save_prediction.php)
    GET  /dashboard-data        forwards to the ML API's /dashboard-data
    POST /sensor_ingest         Raspberry Pi uploads   (see iot.py)
    GET  /sensor_data           dashboard sensor panel (see iot.py)
    GET  /prediction_context    predict-form auto-fill (see iot.py)
    GET  /health
"""

import json
import logging
from datetime import date, datetime, timedelta
from decimal import Decimal

import bcrypt
import pymysql
import requests
from flask import Flask, jsonify, request
from flask.json.provider import DefaultJSONProvider

from db import close_db, ensure_schema, env, execute, now_local, query
from iot import iot

log = logging.getLogger("backend")


class JSONProvider(DefaultJSONProvider):
    """Dates as 2026-11-09 / 2026-11-09 13:05:12 (like PHP), decimals as numbers."""

    sort_keys = False

    @staticmethod
    def default(o):
        if isinstance(o, datetime):
            return o.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(o, date):
            return o.isoformat()
        if isinstance(o, Decimal):
            return float(o)
        if isinstance(o, timedelta):
            return str(o)
        return DefaultJSONProvider.default(o)


app = Flask(__name__)
app.json = JSONProvider(app)
app.teardown_appcontext(close_db)
app.register_blueprint(iot)

ML_API_URL = env("ML_API_URL", env("WASTE_MODEL_API_URL", "http://127.0.0.1:5000")).rstrip("/")
if ML_API_URL.endswith("/predict"):
    ML_API_URL = ML_API_URL[: -len("/predict")]

_schema_ready = False


def _try_schema():
    global _schema_ready
    try:
        ensure_schema()
        _schema_ready = True
    except Exception as e:  # database not running yet: try again on the next request
        log.warning("Could not prepare database tables yet: %s", e)


# --------------------------------------------------------------------------- plumbing

@app.before_request
def before():
    if request.method == "OPTIONS":
        return "", 204
    if not _schema_ready:
        _try_schema()


@app.after_request
def cors(response):
    response.headers["Access-Control-Allow-Origin"] = env("ALLOWED_ORIGIN", "*")
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Device-Id, X-Api-Key"
    return response


@app.errorhandler(pymysql.err.OperationalError)
def db_down(e):
    log.error("Database error: %s", e)
    return jsonify(success=False, status="error", message="Database connection failed"), 500


@app.errorhandler(404)
def not_found(_e):
    return jsonify(success=False, message="Not found"), 404


@app.errorhandler(405)
def wrong_method(_e):
    return jsonify(success=False, message="Method not allowed"), 405


@app.errorhandler(500)
def server_error(_e):
    return jsonify(success=False, status="error", message="Server error"), 500


def body():
    """JSON request body as a dict (empty dict if missing/invalid), like PHP's php://input."""
    data = request.get_json(force=True, silent=True)
    return data if isinstance(data, dict) else {}


def to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def to_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def first(*values, default=None):
    """PHP's `a ?? b ?? c`: the first value that is not None."""
    for v in values:
        if v is not None:
            return v
    return default


# --------------------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    hashed = bcrypt.hashpw(password.encode()[:72], bcrypt.gensalt(rounds=10)).decode()
    # Same algorithm as PHP's password_hash(); "$2y$" keeps the hashes readable by PHP too.
    return "$2y$" + hashed[4:]


def check_password(password: str, stored: str) -> bool:
    if not stored or not stored.startswith(("$2y$", "$2a$", "$2b$")):
        return False
    try:
        return bcrypt.checkpw(password.encode()[:72], ("$2b$" + stored[4:]).encode())
    except ValueError:
        return False


# --------------------------------------------------------------------------- routes

@app.get("/")
@app.get("/health")
def health():
    return jsonify(service="Food waste backend (Python)", status="ok")


@app.post("/login")
def login():
    data = body()
    if not data:
        return jsonify(status="error")
    email = str(data.get("email", ""))
    password = str(data.get("password", ""))
    role = data.get("role", "")
    if role not in ("admin", "student"):
        return jsonify(status="invalid_role"), 422

    user = query("SELECT * FROM users WHERE email = %s", (email,), one=True)
    if not user:
        return jsonify(status="no_user")
    if not check_password(password, user["password"]):
        return jsonify(status="wrong_password")
    if (user.get("role") or "admin") != role:
        return jsonify(status="wrong_role")
    return jsonify(status="success", user={
        "id": user["id"], "name": user["username"], "email": user["email"], "role": user.get("role") or "admin",
    })


@app.post("/register")
def register():
    data = body()
    if not data:
        return jsonify(error="No data received")
    username = str(data.get("username", "")).strip()
    email = str(data.get("email", "")).strip()
    password = str(data.get("password", ""))
    role = data.get("role", "")
    if role not in ("admin", "student"):
        return jsonify(status="invalid_role"), 422
    if not username or not email or not password:
        return jsonify(status="error")

    if query("SELECT id FROM users WHERE email = %s", (email,), one=True):
        return jsonify(status="email_exists")
    try:
        _, new_id = execute(
            "INSERT INTO users (username, email, password, role) VALUES (%s, %s, %s, %s)",
            (username, email, hash_password(password), role),
        )
    except pymysql.err.MySQLError as e:
        log.error("register failed: %s", e)
        return jsonify(status="error")
    return jsonify(status="success", user={"id": new_id, "name": username, "email": email, "role": role})


@app.route("/attendance", methods=["GET", "POST"])
def attendance():
    action = request.args.get("action", "")

    if request.method == "POST" and action == "mark":
        user_id = to_int(body().get("userId"))
        if user_id <= 0:
            return jsonify(success=False, message="A valid student is required."), 422
        if not query("SELECT id FROM users WHERE id = %s AND role = 'student'", (user_id,), one=True):
            return jsonify(success=False, message="Only student accounts can mark attendance."), 403
        today = now_local().date().isoformat()
        try:
            execute(
                """INSERT INTO attendance (user_id, attendance_date, status) VALUES (%s, %s, 'present')
                   ON DUPLICATE KEY UPDATE status = 'present', marked_at = CURRENT_TIMESTAMP""",
                (user_id, today),
            )
        except pymysql.err.MySQLError as e:
            log.error("attendance mark failed: %s", e)
            return jsonify(success=False, message="Could not mark attendance."), 500
        return jsonify(success=True, date=today)

    if request.method == "GET" and action == "mine":
        records = query(
            """SELECT attendance_date, status, marked_at FROM attendance
               WHERE user_id = %s ORDER BY attendance_date DESC LIMIT 14""",
            (to_int(request.args.get("userId")),),
        )
        return jsonify(success=True, records=records)

    if request.method == "GET" and action == "all":
        records = query(
            """SELECT u.id, u.username, u.email, a.attendance_date, a.status, a.marked_at
               FROM attendance a INNER JOIN users u ON u.id = a.user_id
               WHERE u.role = 'student'
               ORDER BY a.attendance_date DESC, a.marked_at DESC LIMIT 100"""
        )
        return jsonify(success=True, records=records)

    return jsonify(success=False, message="Unknown attendance action."), 404


def _forward_to_ml(method, path, payload=None):
    url = ML_API_URL + path
    try:
        r = requests.request(method, url, json=payload, timeout=30)
    except requests.RequestException:
        return jsonify(success=False, message="Could not reach model API", modelApiUrl=url), 502
    try:
        decoded = r.json()
    except ValueError:
        return jsonify(success=False, message="Invalid response from model API"), 502
    return jsonify(decoded), r.status_code


@app.post("/predict")
def predict():
    data = body()
    if not data:
        return jsonify(success=False, message="No data received"), 400
    return _forward_to_ml("POST", "/predict", data)


@app.get("/dashboard-data")
def dashboard_data():
    return _forward_to_ml("GET", "/dashboard-data")


@app.post("/save_prediction")
def save_prediction():
    data = body()
    if not data:
        return jsonify(success=False, message="No data received")

    recs = data.get("recommendations") if isinstance(data.get("recommendations"), dict) else {}
    qty = recs.get("quantities") if isinstance(recs.get("quantities"), dict) else {}
    menu_items = data.get("menuItems") if isinstance(data.get("menuItems"), list) else []

    def text(value, default, size):
        return str(value if value not in (None, "") else default)[:size]

    row = (
        text(data.get("date"), now_local().date().isoformat(), 10),
        text(data.get("dayOfWeek"), "Monday", 20),
        text(data.get("mealType"), "lunch", 20),
        to_int(data.get("studentsEnrolled")),
        to_float(data.get("averageAttendance")),
        text(data.get("specialEvent"), "no", 10),
        text(data.get("weather"), "clear", 20),
        text(data.get("holidayPeriod"), "no", 10),
        to_int(data.get("menusServed")),
        to_float(data.get("leftoverFromPreviousDay")),
        to_float(data.get("predictedWasteKg")),
        to_float(data.get("cost")),
        text(data.get("recommendation"), "Low", 20),
        to_float(data.get("confidence")),
        json.dumps(menu_items, separators=(",", ":")),
        to_float(first(qty.get("rice_kg"), data.get("recommendedRiceKg"), default=0)),
        to_float(first(qty.get("dal_kg"), data.get("recommendedDalKg"), default=0)),
        to_int(first(qty.get("roti_count"), data.get("recommendedRotiCount"), default=0)),
        to_float(first(qty.get("veg_kg"), data.get("recommendedVegKg"), default=0)),
        to_float(first(qty.get("nonveg_kg"), data.get("recommendedNonvegKg"), default=0)),
        to_float(first(recs.get("current_waste_kg"), data.get("currentWasteKg"), default=0)),
        to_float(first(recs.get("optimized_waste_kg"), data.get("optimizedWasteKg"), default=0)),
        to_float(first(recs.get("waste_reduction_kg"), data.get("wasteReductionKg"), default=0)),
        to_float(first(recs.get("reduction_percent"), data.get("reductionPercent"), default=0)),
    )
    try:
        _, new_id = execute(
            """INSERT INTO waste_predictions (
                   date, day_of_week, meal_type, students_enrolled, average_attendance, special_events,
                   weather, holiday_period, number_of_menus, leftover_previous_day, predicted_waste, cost,
                   recommendation, confidence, menu_items, recommended_rice_kg, recommended_dal_kg,
                   recommended_roti_count, recommended_veg_kg, recommended_nonveg_kg, current_waste_kg,
                   optimized_waste_kg, waste_reduction_kg, reduction_percent)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            row,
        )
    except pymysql.err.MySQLError as e:
        return jsonify(success=False, message="Could not save prediction entry", error=str(e))
    return jsonify(success=True, message="Prediction entry saved successfully", id=new_id)


_try_schema()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # 0.0.0.0 = reachable from other devices on the network (e.g. the Raspberry Pi).
    app.run(host=env("HOST", "0.0.0.0"), port=int(env("PORT", 8000)), debug=False)
