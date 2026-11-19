#!/usr/bin/env python3
"""
Hostel kitchen sensor client for the Raspberry Pi.

Reads the kitchen sensors, stores every reading in a small local SQLite queue
first, and uploads the queue to the backend (backend/iot.py, POST /sensor_ingest).
If Wi-Fi or the server is down, readings wait in the queue and are sent later,
so nothing is lost.

Sensors (all configured in config.json):
  * load-cell scales through HX711 boards
      - waste_weight         plate-waste bin
      - leftover_weight      container for unserved cooked food
      - raw_material_weight  stock container of rice / dal / oil ... ("item")
  * a DHT22 (or DHT11) temperature + humidity sensor

Usage:
  python3 sensor_client.py                       run forever (what the systemd service does)
  python3 sensor_client.py --check               read each sensor once and test the server login
  python3 sensor_client.py --simulate            run with fake sensors (no hardware needed)
  python3 sensor_client.py --simulate --backfill-days 7
                                                 generate a week of realistic demo data and upload it
"""

import argparse
import glob
import json
import logging
import math
import os
import random
import re
import signal
import sqlite3
import sys
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta

try:
    import requests
except ImportError:  # pragma: no cover
    sys.exit("The 'requests' package is missing: pip install -r requirements.txt")

log = logging.getLogger("foodwaste")

WEIGHT_TYPES = {"waste_weight", "leftover_weight", "raw_material_weight"}
NAME_RE = re.compile(r"^[a-z0-9_]{1,40}$")
DEFAULT_MEAL_WINDOWS = {
    "breakfast": ["05:00", "10:00"],
    "lunch": ["10:00", "16:00"],
    "dinner": ["16:00", "24:00"],
}


# --------------------------------------------------------------------------- helpers

def meal_for(now: datetime, windows: dict) -> tuple:
    """(meal_date, meal_type) for a moment, using the HH:MM windows from config."""
    hm = now.strftime("%H:%M")
    for meal, (start, end) in windows.items():
        if start <= hm < end:
            return now.date().isoformat(), meal
    return now.date().isoformat(), "other"


def make_reading(sensor: str, sensor_type: str, item: str, value: float, now: datetime, windows: dict) -> dict:
    meal_date, meal_type = meal_for(now, windows)
    return {
        "uid": str(uuid.uuid4()),
        "sensor": sensor,
        "type": sensor_type,
        "item": item,
        "value": round(float(value), 3),
        "recorded_at": now.strftime("%Y-%m-%d %H:%M:%S"),
        "meal_date": meal_date,
        "meal_type": meal_type,
    }


def clock_looks_valid(now: datetime) -> bool:
    # A Pi without a battery clock boots in the past until NTP syncs.
    return now.year >= 2024


# --------------------------------------------------------------------------- offline queue

class ReadingQueue:
    """Readings waiting to be uploaded, kept on the SD card so they survive reboots."""

    def __init__(self, path: str, max_rows: int = 200_000):
        self.max_rows = max_rows
        self.lock = threading.Lock()
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS queue (uid TEXT PRIMARY KEY, payload TEXT NOT NULL, created REAL NOT NULL)"
        )

    def put(self, reading: dict) -> None:
        with self.lock:
            self.db.execute(
                "INSERT OR IGNORE INTO queue (uid, payload, created) VALUES (?, ?, ?)",
                (reading["uid"], json.dumps(reading), time.time()),
            )

    def put_many(self, readings: list) -> None:
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany(
                "INSERT OR IGNORE INTO queue (uid, payload, created) VALUES (?, ?, ?)",
                [(r["uid"], json.dumps(r), time.time()) for r in readings],
            )
            self.db.execute("COMMIT")

    def peek(self, limit: int) -> list:
        with self.lock:
            rows = self.db.execute(
                "SELECT payload FROM queue ORDER BY created, rowid LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def remove(self, uids) -> None:
        uids = list(uids)
        if not uids:
            return
        with self.lock:
            self.db.execute("BEGIN")
            self.db.executemany("DELETE FROM queue WHERE uid = ?", [(u,) for u in uids])
            self.db.execute("COMMIT")

    def count(self) -> int:
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM queue").fetchone()[0]

    def trim(self) -> None:
        """Never fill the SD card: drop the oldest readings beyond max_rows."""
        extra = self.count() - self.max_rows
        if extra > 0:
            with self.lock:
                self.db.execute(
                    "DELETE FROM queue WHERE rowid IN (SELECT rowid FROM queue ORDER BY created, rowid LIMIT ?)",
                    (extra,),
                )
            log.warning("Offline queue full: dropped %d oldest readings", extra)


# --------------------------------------------------------------------------- scale drivers

class HX711Scale:
    """Real load cell. offset/scale come from calibrate_scale.py."""

    def __init__(self, cfg: dict):
        from hx711 import HX711  # imported here so --simulate works on any computer

        self.hx = HX711(int(cfg["dout_pin"]), int(cfg["sck_pin"]), int(cfg.get("gain", 128)))
        self.offset = float(cfg.get("offset", 0))
        self.scale = float(cfg.get("scale", 1))
        self.samples = int(cfg.get("samples", 7))
        if self.scale == 0:
            raise ValueError("scale must not be 0 - run calibrate_scale.py")

    def read_kg(self, now: datetime) -> float:
        return (self.hx.read_median(self.samples) - self.offset) / self.scale


class SimulatedScale:
    """
    A fake load cell that behaves like a hostel kitchen, for demos and tests.
      waste bin  : plates scraped during serving times, emptied at night (or when full)
      leftovers  : unserved food added after each meal, cleared next morning
      raw stock  : taken out in chunks while cooking, restocked when low
    """

    SERVING = {  # meal: (start, end, average kg of plate waste per minute)
        "breakfast": ("07:30", "09:30", 0.06),
        "lunch": ("12:00", "14:30", 0.15),
        "dinner": ("19:30", "22:00", 0.12),
    }
    COOKING = {"breakfast": ("06:30", "07:00"), "lunch": ("10:30", "11:15"), "dinner": ("17:30", "18:15")}
    DRAW_KG = {  # raw material used per meal
        "rice": {"breakfast": 2.0, "lunch": 12.0, "dinner": 10.0},
        "dal": {"breakfast": 1.0, "lunch": 5.0, "dinner": 4.5},
        "oil": {"breakfast": 0.4, "lunch": 1.5, "dinner": 1.2},
    }

    def __init__(self, sensor_type: str, item: str, seed: int):
        self.type = sensor_type
        self.item = item or "rice"
        self.rng = random.Random(seed)
        self.weight = 45.0 if sensor_type == "raw_material_weight" else 0.0
        self.last = None
        self.day_factor = {}
        self.done = set()
        self.remaining = {}

    def _factor(self, day) -> float:
        if day not in self.day_factor:
            self.day_factor[day] = self.rng.uniform(0.75, 1.25)
        return self.day_factor[day]

    @staticmethod
    def _plus(hm: str, minutes: int) -> str:
        t = datetime.strptime(hm, "%H:%M") + timedelta(minutes=minutes)
        return t.strftime("%H:%M")

    def read_kg(self, now: datetime) -> float:
        dt_min = 0.0 if self.last is None else max(0.0, (now - self.last).total_seconds() / 60)
        self.last = now
        hm, day = now.strftime("%H:%M"), now.date()
        factor = self._factor(day)

        if self.type == "waste_weight":
            for start, end, rate in self.SERVING.values():
                if start <= hm < end and self.rng.random() < min(1.0, rate * factor / 0.25 * dt_min):
                    self.weight += self.rng.uniform(0.1, 0.4)
            if self.weight > 35:  # bin full, staff empty it
                self.weight = 0.0
            if hm >= "23:30" and ("empty", day) not in self.done:
                self.weight = 0.0
                self.done.add(("empty", day))

        elif self.type == "leftover_weight":
            for meal, (start, end, _) in self.SERVING.items():
                if end <= hm < self._plus(end, 15) and (meal, day) not in self.done:
                    self.weight += self.rng.uniform(0.5, 4.0) * factor
                    self.done.add((meal, day))
            if "06:00" <= hm and ("clear", day) not in self.done:
                self.weight = 0.0
                self.done.add(("clear", day))

        else:  # raw_material_weight
            draws = self.DRAW_KG.get(self.item, {"breakfast": 1.0, "lunch": 4.0, "dinner": 3.0})
            for meal, (start, end) in self.COOKING.items():
                if start <= hm < end:
                    key = (meal, day)
                    if key not in self.remaining:
                        self.remaining[key] = draws[meal] * factor * self.rng.uniform(0.85, 1.15)
                    left = self.remaining[key]
                    if left > 0 and self.rng.random() < min(1.0, 0.5 * dt_min):
                        take = min(left, self.rng.uniform(0.08, 0.25) * draws["lunch"])
                        if self.weight - take < 0.5:
                            self.weight += 40.0  # emergency restock
                        self.weight -= take
                        self.remaining[key] = left - take
            if self.weight < 15 and "09:00" <= hm < "10:00" and ("restock", day) not in self.done:
                self.weight += 40.0
                self.done.add(("restock", day))

        return self.weight + self.rng.gauss(0, 0.004)


# --------------------------------------------------------------------------- climate drivers

def find_dht_iio_device() -> str:
    for path in sorted(glob.glob("/sys/bus/iio/devices/iio:device*")):
        try:
            with open(os.path.join(path, "name")) as f:
                if f.read().strip().startswith("dht11"):
                    return path
        except OSError:
            continue
    raise RuntimeError(
        "No DHT sensor found. Add 'dtoverlay=dht11,gpiopin=4' to /boot/firmware/config.txt "
        "(older systems: /boot/config.txt) and reboot. The dht11 overlay also handles the DHT22."
    )


class DhtKernelDriver:
    """DHT11/DHT22 through the Linux kernel driver (most reliable, works on every Pi model)."""

    def __init__(self, cfg: dict):
        self.path = cfg.get("iio_path") or find_dht_iio_device()

    def read(self, now: datetime) -> tuple:
        last_error = None
        for _ in range(5):  # the sensor often needs a retry or two
            try:
                with open(os.path.join(self.path, "in_temp_input")) as f:
                    temperature = int(f.read().strip()) / 1000
                with open(os.path.join(self.path, "in_humidityrelative_input")) as f:
                    humidity = int(f.read().strip()) / 1000
                return temperature, humidity
            except (OSError, ValueError) as e:
                last_error = e
                time.sleep(2.1)
        raise RuntimeError(f"DHT sensor read failed: {last_error}")


class DhtAdafruitDriver:
    """Alternative using the adafruit-circuitpython-dht library (pip install adafruit-circuitpython-dht)."""

    def __init__(self, cfg: dict):
        import adafruit_dht
        import board

        model = str(cfg.get("model", "DHT22")).upper()
        cls = adafruit_dht.DHT22 if model == "DHT22" else adafruit_dht.DHT11
        self.device = cls(getattr(board, f"D{int(cfg.get('pin', 4))}"), use_pulseio=False)

    def read(self, now: datetime) -> tuple:
        for _ in range(5):
            try:
                t, h = self.device.temperature, self.device.humidity
                if t is not None and h is not None:
                    return float(t), float(h)
            except RuntimeError:
                pass
            time.sleep(2.1)
        raise RuntimeError("DHT sensor read failed 5 times")


class SimulatedClimate:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)

    def read(self, now: datetime) -> tuple:
        hour = now.hour + now.minute / 60
        wave = math.sin((hour - 9) / 24 * 2 * math.pi)
        cooking = any(s <= now.strftime("%H:%M") < e for s, e in SimulatedScale.COOKING.values())
        temperature = 27 + 5 * wave + (3 if cooking else 0) + self.rng.gauss(0, 0.3)
        humidity = min(95.0, max(20.0, 62 - 12 * wave + self.rng.gauss(0, 1.5)))
        return round(temperature, 1), round(humidity, 1)


class RetryingDriver:
    """Creates the real driver on first use and retries on later reads if that fails
    (e.g. a scale that is unplugged at boot), so one broken sensor never stops the others."""

    def __init__(self, factory):
        self.factory = factory
        self.driver = None

    def _get(self):
        if self.driver is None:
            self.driver = self.factory()
        return self.driver

    def read_kg(self, now: datetime) -> float:
        return self._get().read_kg(now)

    def read(self, now: datetime) -> tuple:
        return self._get().read(now)


# --------------------------------------------------------------------------- sensors

class WeightSensor:
    """
    Turns raw scale readings into readings worth uploading:
      * waits until the weight is stable (nobody is in the middle of tipping a plate)
      * sends it when it changed by at least min_change_kg
      * otherwise sends a heartbeat every heartbeat_s so the cloud knows the scale is alive
    """

    def __init__(self, cfg: dict, driver, windows: dict):
        self.name = cfg["name"]
        self.type = cfg["type"]
        self.item = cfg.get("item", "") if self.type == "raw_material_weight" else ""
        self.driver = driver
        self.windows = windows
        self.min_change = float(cfg.get("min_change_kg", 0.1))
        self.tolerance = float(cfg.get("stable_tolerance_kg", 0.05))
        self.heartbeat_s = float(cfg.get("heartbeat_s", 300))
        self.window = deque(maxlen=int(cfg.get("stable_samples", 3)))
        self.last_sent = None
        self.last_sent_at = None

    def sample(self, now: datetime) -> list:
        kg = self.driver.read_kg(now)
        self.window.append(kg)
        if len(self.window) < self.window.maxlen or max(self.window) - min(self.window) > self.tolerance:
            return []
        stable = sum(self.window) / len(self.window)
        changed = self.last_sent is None or abs(stable - self.last_sent) >= self.min_change
        due = self.last_sent_at is None or (now - self.last_sent_at).total_seconds() >= self.heartbeat_s
        if not (changed or due):
            return []
        # A heartbeat repeats the last value unless the change is real, so scale
        # drift never adds up to fake waste on the server.
        value = stable if changed else self.last_sent
        self.last_sent, self.last_sent_at = value, now
        return [make_reading(self.name, self.type, self.item, value, now, self.windows)]


class ClimateSensor:
    def __init__(self, cfg: dict, driver, windows: dict):
        self.name = cfg.get("name", "kitchen_air")
        self.interval_s = float(cfg.get("interval_s", 300))
        self.driver = driver
        self.windows = windows
        self.last_at = None

    def sample(self, now: datetime) -> list:
        if self.last_at is not None and (now - self.last_at).total_seconds() < self.interval_s:
            return []
        self.last_at = now
        temperature, humidity = self.driver.read(now)
        readings = []
        if -20 <= temperature <= 80:
            readings.append(make_reading(self.name, "temperature", "", temperature, now, self.windows))
        if 0 <= humidity <= 100:
            readings.append(make_reading(self.name, "humidity", "", humidity, now, self.windows))
        return readings


# --------------------------------------------------------------------------- upload

class Uploader:
    def __init__(self, cfg: dict, queue: ReadingQueue):
        self.url = cfg["server_url"].rstrip("/") + cfg.get("ingest_path", "/sensor_ingest")
        self.headers = {"X-Device-Id": cfg["device_id"], "X-Api-Key": cfg["api_key"]}
        self.verify = cfg.get("verify_tls", True)
        self.batch_size = int(cfg.get("batch_size", 200))
        self.queue = queue
        self.session = requests.Session()
        self.online = None
        self.last_contact = 0.0

    def _post(self, readings: list):
        return self.session.post(
            self.url, json={"readings": readings}, headers=self.headers, timeout=(5, 30), verify=self.verify
        )

    def _set_online(self, online: bool, detail: str = "") -> None:
        if online != self.online:
            if online:
                log.info("Connected to %s", self.url)
            else:
                log.warning("Server unreachable (%s); %d readings kept offline", detail, self.queue.count())
        self.online = online

    def ping(self) -> bool:
        try:
            r = self._post([])
        except requests.RequestException as e:
            self._set_online(False, type(e).__name__)
            return False
        if r.status_code == 200:
            self._set_online(True)
            self.last_contact = time.time()
            return True
        self._log_http_error(r)
        return False

    def _log_http_error(self, r) -> None:
        try:
            message = r.json().get("message", r.text[:200])
        except ValueError:
            message = r.text[:200]
        if r.status_code in (401, 403):
            log.error("Server refused this device (%s): %s - check device_id/api_key in config.json",
                      r.status_code, message)
        else:
            log.warning("Upload failed with HTTP %s: %s", r.status_code, message)
        self.online = False

    def flush(self, max_batches: int = 50) -> bool:
        """Upload queued readings. True if everything was sent (or nothing to send)."""
        for _ in range(max_batches):
            batch = self.queue.peek(self.batch_size)
            if not batch:
                return True
            try:
                r = self._post(batch)
            except requests.RequestException as e:
                self._set_online(False, type(e).__name__)
                return False
            if r.status_code == 413 and self.batch_size > 10:
                self.batch_size = max(10, self.batch_size // 2)
                continue
            if r.status_code != 200:
                self._log_http_error(r)
                return False
            try:
                body = r.json()
            except ValueError:
                log.warning("Server answered with something that is not JSON: %s", r.text[:200])
                self.online = False
                return False
            rejected = body.get("rejected", [])
            for item in rejected[:5]:
                log.warning("Server rejected reading %s: %s", item.get("uid"), item.get("error"))
            self.queue.remove(set(body.get("accepted", [])) | {i.get("uid") for i in rejected})
            self._set_online(True)
            self.last_contact = time.time()
            log.debug("Uploaded %d readings (%d new)", len(batch), body.get("stored", 0))
        return self.queue.count() == 0


# --------------------------------------------------------------------------- setup

def load_config(path: str) -> dict:
    with open(path) as f:
        cfg = json.load(f)
    for key in ("server_url", "device_id", "api_key"):
        if not cfg.get(key) or "PASTE" in str(cfg.get(key)) or "YOUR-" in str(cfg.get(key)):
            raise SystemExit(f"config.json: set '{key}' (get device_id/api_key from: python manage.py add-device)")
    cfg.setdefault("meal_windows", DEFAULT_MEAL_WINDOWS)
    return cfg


def build_sensors(cfg: dict, simulate: bool) -> list:
    windows = cfg["meal_windows"]
    sensors = []
    for i, sc in enumerate(cfg.get("scales", [])):
        if not sc.get("enabled", True):
            continue
        if not NAME_RE.match(sc.get("name", "")):
            raise SystemExit(f"scale name '{sc.get('name')}' must be lowercase letters, digits or _")
        if sc.get("type") not in WEIGHT_TYPES:
            raise SystemExit(f"scale '{sc['name']}': type must be one of {sorted(WEIGHT_TYPES)}")
        if simulate or sc.get("driver") == "simulate":
            driver = SimulatedScale(sc["type"], sc.get("item", ""), seed=1000 + i)
        else:
            driver = RetryingDriver(lambda sc=sc: HX711Scale(sc))
        sensors.append(WeightSensor(sc, driver, windows))

    climate = cfg.get("climate")
    if climate and climate.get("enabled", True):
        kind = "simulate" if simulate else climate.get("driver", "dht_kernel")
        if kind == "simulate":
            driver = SimulatedClimate(seed=99)
        elif kind == "dht_kernel":
            driver = RetryingDriver(lambda: DhtKernelDriver(climate))
        elif kind == "dht_adafruit":
            driver = RetryingDriver(lambda: DhtAdafruitDriver(climate))
        else:
            raise SystemExit("climate.driver must be dht_kernel, dht_adafruit or simulate")
        sensors.append(ClimateSensor(climate, driver, windows))

    if not sensors:
        raise SystemExit("No sensors enabled in config.json")
    return sensors


def sample_all(sensors: list, now: datetime, errors: dict) -> list:
    readings = []
    for s in sensors:
        try:
            readings.extend(s.sample(now))
            errors.pop(s.name, None)
        except Exception as e:  # one broken sensor must not stop the others
            if s.name not in errors:
                log.error("Sensor %s failed: %s", s.name, e)
            errors[s.name] = str(e)
    return readings


# --------------------------------------------------------------------------- modes

def run_check(cfg: dict, sensors: list, uploader: Uploader) -> int:
    ok = True
    now = datetime.now()
    print(f"Device {cfg['device_id']} -> {uploader.url}")
    print(f"Pi clock: {now:%Y-%m-%d %H:%M:%S}  meal now: {meal_for(now, cfg['meal_windows'])[1]}")
    for s in sensors:
        try:
            if isinstance(s, WeightSensor):
                values = [s.driver.read_kg(datetime.now()) for _ in range(3)]
                print(f"  {s.name:<16} {s.type:<20} {' '.join(f'{v:8.3f}' for v in values)} kg")
            else:
                t, h = s.driver.read(datetime.now())
                print(f"  {s.name:<16} temperature/humidity  {t:.1f} C  {h:.1f} %")
        except Exception as e:
            ok = False
            print(f"  {s.name:<16} ERROR: {e}")
    if uploader.ping():
        print("Server login: OK")
    else:
        ok = False
        print("Server login: FAILED (see message above)")
    return 0 if ok else 1


def run_backfill(cfg: dict, sensors: list, queue: ReadingQueue, uploader: Uploader, days: int) -> int:
    step = max(5, int(cfg.get("sample_interval_s", 5)))
    end = datetime.now().replace(microsecond=0)
    now = end - timedelta(days=days)
    errors, total, buffer = {}, 0, []
    log.info("Generating %d day(s) of simulated readings ...", days)
    while now <= end:
        buffer.extend(sample_all(sensors, now, errors))
        if len(buffer) >= 5000:
            queue.put_many(buffer)
            total += len(buffer)
            buffer = []
        now += timedelta(seconds=step)
    queue.put_many(buffer)
    total += len(buffer)
    log.info("Generated %d readings; uploading ...", total)
    while not uploader.flush(max_batches=100):
        if not uploader.online:
            log.error("Upload stopped; %d readings stay queued and will be sent by the normal run", queue.count())
            return 1
    log.info("Backfill uploaded. Queue now has %d readings.", queue.count())
    return 0


def run_forever(cfg: dict, sensors: list, queue: ReadingQueue, uploader: Uploader, once: bool) -> int:
    sample_every = float(cfg.get("sample_interval_s", 5))
    upload_every = float(cfg.get("upload_interval_s", 30))
    ping_every = float(cfg.get("ping_interval_s", 60))
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stop.set())

    errors = {}
    next_sample = next_upload = 0.0
    backoff = upload_every
    clock_warned = False
    log.info("Running with %d sensor(s); %d readings waiting in queue", len(sensors), queue.count())

    while not stop.is_set():
        t = time.time()
        if t >= next_sample:
            now = datetime.now().replace(microsecond=0)
            if clock_looks_valid(now):
                readings = sample_all(sensors, now, errors)
                for r in readings:
                    queue.put(r)
                    log.debug("%s %s=%s", r["sensor"], r["type"], r["value"])
            elif not clock_warned:
                log.warning("Pi clock is %s - waiting for time sync before recording", now)
                clock_warned = True
            next_sample = t + sample_every

        if t >= next_upload or once:
            queue.trim()
            if queue.count():
                sent = uploader.flush()
            elif time.time() - uploader.last_contact >= ping_every:
                sent = uploader.ping()
            else:
                sent = True
            backoff = upload_every if sent else min(backoff * 2, 300)
            next_upload = time.time() + backoff

        if once:
            break
        stop.wait(max(0.1, min(next_sample, next_upload) - time.time()))

    log.info("Stopping; %d readings still queued (they will be sent next start)", queue.count())
    return 0


def main() -> int:
    here = os.path.dirname(os.path.abspath(__file__))
    p = argparse.ArgumentParser(description="Hostel kitchen sensor client")
    p.add_argument("--config", default=os.path.join(here, "config.json"))
    p.add_argument("--simulate", action="store_true", help="use fake sensors (no hardware)")
    p.add_argument("--backfill-days", type=int, default=0, help="with --simulate: generate N days of history")
    p.add_argument("--check", action="store_true", help="read each sensor once and test the server login")
    p.add_argument("--once", action="store_true", help="one sample + upload, then exit")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    if args.backfill_days and not args.simulate:
        p.error("--backfill-days only works together with --simulate")

    cfg = load_config(args.config)
    queue_path = cfg.get("queue_db", "readings_queue.db")
    if not os.path.isabs(queue_path):
        queue_path = os.path.join(here, queue_path)
    queue = ReadingQueue(queue_path, int(cfg.get("max_queue_rows", 200_000)))
    sensors = build_sensors(cfg, args.simulate)
    uploader = Uploader(cfg, queue)

    try:
        if args.check:
            return run_check(cfg, sensors, uploader)
        if args.backfill_days:
            return run_backfill(cfg, sensors, queue, uploader, args.backfill_days)
        return run_forever(cfg, sensors, queue, uploader, args.once)
    finally:
        if not args.simulate:
            try:
                from hx711 import HX711

                HX711.cleanup()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
