"""
Hostel kitchen sensor node for the Wokwi simulator (ESP32 + MicroPython).

It does what the Raspberry Pi does in the real kitchen, inside the browser:
  * waste_bin  : 50 kg load cell (HX711) under the plate-waste bin
  * rice_store : 50 kg load cell (HX711) under the rice drum
  * kitchen_air: DHT22 temperature + humidity
and uploads the readings to the Python backend (POST /sensor_ingest).

While the simulation runs, click a load cell and move its slider to change the
weight: raise "waste_bin" to throw food away, lower "rice_store" to take rice
for cooking. Click the DHT22 to change temperature / humidity.

Fill in the three settings below (see IOT_SETUP.md, "Wokwi simulator").
"""

import time
import os
import ubinascii
import network
import urequests
import dht
from machine import Pin, disable_irq, enable_irq

# ----------------------------------------------------------------- settings
SERVER_URL = "https://YOUR-TUNNEL-NAME.trycloudflare.com"
DEVICE_ID = "wokwi-sim"
API_KEY = "PASTE-THE-API-KEY-HERE"

SAMPLE_EVERY_S = 2        # read the scales this often
CLIMATE_EVERY_S = 60      # temperature / humidity this often
HEARTBEAT_S = 60          # resend an unchanged weight this often
MIN_CHANGE_KG = 0.1       # send a weight when it changed at least this much
COUNTS_PER_KG = 420       # Wokwi's 50 kg load cell: 0..50 kg -> 0..21000 counts
IST_OFFSET_S = 5 * 3600 + 30 * 60

SCALES = [
    # name,        sensor type,           item,   DT pin, SCK pin
    ("waste_bin",  "waste_weight",        "",     16,     4),
    ("rice_store", "raw_material_weight", "rice", 18,     19),
]
DHT_PIN = 15


# ----------------------------------------------------------------- HX711 driver
class HX711:
    def __init__(self, dt_pin, sck_pin):
        self.dt = Pin(dt_pin, Pin.IN)
        self.sck = Pin(sck_pin, Pin.OUT, value=0)

    def read_raw(self):
        start = time.ticks_ms()
        while self.dt.value() == 1:                      # wait until a conversion is ready
            if time.ticks_diff(time.ticks_ms(), start) > 1000:
                raise OSError("HX711 not responding (check DT/SCK wiring)")
            time.sleep_ms(1)
        state = disable_irq()                            # keep the clock pulses short
        value = 0
        try:
            for _ in range(24):
                self.sck.value(1)
                self.sck.value(0)
                value = (value << 1) | self.dt.value()
            self.sck.value(1)                            # 25th pulse: channel A, gain 128
            self.sck.value(0)
        finally:
            enable_irq(state)
        if value & 0x800000:
            value -= 0x1000000
        return value

    def read_kg(self, samples=3):
        values = sorted(self.read_raw() for _ in range(samples))
        return abs(values[len(values) // 2]) / COUNTS_PER_KG


# ----------------------------------------------------------------- helpers
def connect_wifi():
    wlan = network.WLAN(network.STA_IF)
    wlan.active(True)
    if not wlan.isconnected():
        print("Connecting to Wokwi-GUEST", end="")
        wlan.connect("Wokwi-GUEST", "")
        while not wlan.isconnected():
            print(".", end="")
            time.sleep(0.25)
    print(" connected, IP", wlan.ifconfig()[0])


clock_ok = False


def sync_clock():
    """Set the clock from the internet; if that fails the backend uses its own time."""
    global clock_ok
    try:
        import ntptime
        ntptime.settime()
        clock_ok = time.localtime()[0] >= 2024
    except Exception as e:
        print("No internet time (", e, "); the backend will timestamp readings")
    print("Clock:", now_text() if clock_ok else "not set")


def now_text():
    t = time.localtime(time.time() + IST_OFFSET_S)
    return "%04d-%02d-%02d %02d:%02d:%02d" % t[:6]


def new_uid():
    return ubinascii.hexlify(os.urandom(12)).decode()


def reading(sensor, sensor_type, item, value):
    r = {"uid": new_uid(), "sensor": sensor, "type": sensor_type, "item": item, "value": round(value, 3)}
    if clock_ok:
        r["recorded_at"] = now_text()  # the backend works out the meal (breakfast/lunch/dinner) from this
    return r


pending = []  # readings waiting to be uploaded (kept if the upload fails)


def upload():
    global pending
    batch = pending[:50]
    try:
        r = urequests.post(
            SERVER_URL + "/sensor_ingest",
            json={"readings": batch},
            headers={"X-Device-Id": DEVICE_ID, "X-Api-Key": API_KEY},
        )
        status = r.status_code
        body = r.json() if status == 200 else r.text
        r.close()
    except Exception as e:
        print("Upload failed:", e, "| waiting:", len(pending))
        return False
    if status != 200:
        print("Server answered", status, body)
        return False
    done = set(body.get("accepted", []))
    for item in body.get("rejected", []):
        print("Rejected:", item)
        done.add(item.get("uid"))
    pending = [p for p in pending if p["uid"] not in done]
    if batch:
        print("Uploaded", len(batch), "reading(s), stored", body.get("stored"), "| waiting:", len(pending))
    return True


# ----------------------------------------------------------------- main loop
def main(loops=None):
    connect_wifi()
    sync_clock()
    scales = [(name, kind, item, HX711(dt, sck)) for name, kind, item, dt, sck in SCALES]
    climate = dht.DHT22(Pin(DHT_PIN))
    last_sent = {}      # name -> (kg, ticks_ms)
    last_climate = None
    last_upload = time.ticks_ms()

    print("Sending to", SERVER_URL, "as", DEVICE_ID)
    if not upload():
        print("Check SERVER_URL / DEVICE_ID / API_KEY at the top of main.py")

    n = 0
    while loops is None or n < loops:
        n += 1
        now = time.ticks_ms()

        for name, kind, item, hx in scales:
            try:
                kg = hx.read_kg()
            except OSError as e:
                print(name, "error:", e)
                continue
            previous = last_sent.get(name)
            changed = previous is None or abs(kg - previous[0]) >= MIN_CHANGE_KG
            due = previous is None or time.ticks_diff(now, previous[1]) >= HEARTBEAT_S * 1000
            if changed or due:
                value = kg if changed else previous[0]
                pending.append(reading(name, kind, item, value))
                last_sent[name] = (value, now)
                print("%-11s %7.2f kg%s" % (name, value, "" if changed else "  (heartbeat)"))

        if last_climate is None or time.ticks_diff(now, last_climate) >= CLIMATE_EVERY_S * 1000:
            last_climate = now
            try:
                climate.measure()
                pending.append(reading("kitchen_air", "temperature", "", climate.temperature()))
                pending.append(reading("kitchen_air", "humidity", "", climate.humidity()))
                print("kitchen_air %5.1f C  %5.1f %%" % (climate.temperature(), climate.humidity()))
            except OSError as e:
                print("DHT22 error:", e)

        if pending or time.ticks_diff(now, last_upload) >= 30000:
            if upload():
                last_upload = now
        if len(pending) > 200:
            pending[:] = pending[-200:]

        time.sleep(SAMPLE_EVERY_S)


if __name__ == "__main__":
    main()
