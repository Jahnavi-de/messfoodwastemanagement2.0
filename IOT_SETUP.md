# IoT extension: sensors → database → model (Python backend)

The whole system is now Python apart from the website: the **backend** (Flask),
the **ML API** (Flask) and the **sensor client** all run on Windows, on a Raspberry Pi
or on a Linux cloud server. The old PHP backend (`nextjsbackend/`) is no longer used.

```
 Sensors                         Backend (Python)                         Website (Next.js)
┌──────────────────────┐        ┌──────────────────────────────────┐      ┌────────────────┐
│ Raspberry Pi         │  HTTP  │ backend/app.py   (port 8000)      │ HTTP │ login, predict,│
│  sensor_client.py    ├───────►│  /sensor_ingest  /sensor_data     │◄─────┤ dashboard,     │
│  HX711 scales, DHT22 │        │  /login /register /attendance     │      │ attendance     │
│ or Wokwi ESP32 sim   │        │  /predict /save_prediction  ──────┼──┐   └────────────────┘
└──────────────────────┘        └───────────────┬──────────────────┘  │
                                                │                     ▼
                                      MySQL / MariaDB        ml-api/app.py (port 5000)
                                      nextjs_project         waste_model.pkl
                                                ▲                     │
                                                └── retrain_from_db.py┘ (nightly)
```

All three Python parts can run on **one Raspberry Pi** (section 4), on your laptop
(section 1), or on a cloud server (section 5).

## What is in the project

| Folder | File | What it does |
|---|---|---|
| `backend/` | `app.py` | Login, register, attendance, predict (forwards to the ML API), save prediction, dashboard data |
| | `iot.py` | Sensor upload from the Pi, sensor data for the dashboard, auto-fill for the predict form |
| | `db.py` | Database connection; settings from `.env` (defaults match XAMPP) |
| | `manage.py` | `init-db`, `add-device`, `list-devices`, `rotate-key`, `disable`, `enable` |
| | `copy_to_cloud.py` | Copies all data from XAMPP to the cloud database (section 1b) |
| | `sql/schema.sql` | All tables; created automatically if missing |
| `ml-api/` | `app.py` | The model API (unchanged logic; reloads the model after retraining) |
| | `retrain_from_db.py` | Retrains the model with waste measured by the sensors |
| `iot-device/` | `sensor_client.py`, `hx711.py`, `calibrate_scale.py` | Runs on the Raspberry Pi; has a `--simulate` mode |
| `wokwi/` | `diagram.json`, `main.py` | The same sensors simulated in the browser on wokwi.com |
| `deploy/` | `foodwaste-backend.service`, `foodwaste-ml.service` | Start backend and ML API at boot on a Pi/server |
| frontend | `lib/`, `app/`, `components/sensor-panel.tsx` | Now calls the Python backend on port 8000 |

Your existing database works unchanged: same tables, and existing accounts log in
with the same passwords.

---

## 1. Run it on your laptop (Windows)

XAMPP is now only needed for **MySQL** (Apache is not used any more).

**Terminal 1 – backend**
```
cd C:\xampp\htdocs\Messfoodwastagemanagement-main\backend
pip install -r requirements.txt
python app.py
```
→ `Running on http://127.0.0.1:8000`. Check `http://localhost:8000/health` in the browser.

**Terminal 2 – ML API**
```
cd C:\xampp\htdocs\Messfoodwastagemanagement-main\ml-api
python app.py
```

**Terminal 3 – website**
```
cd C:\xampp\htdocs\Messfoodwastagemanagement-main
npm run dev
```
`.env.local` already points at `http://localhost:8000`. After changing `.env.local`,
restart `npm run dev`.

**Terminal 4 – simulated Raspberry Pi**
```
cd C:\xampp\htdocs\Messfoodwastagemanagement-main\backend
python manage.py add-device sim-pi "Simulated kitchen Pi" "Laptop"
cd ..\iot-device
pip install requests
```
Create `config.json` with the printed key (PowerShell):
```powershell
$c = Get-Content config.example.json -Raw | ConvertFrom-Json
$c.server_url = "http://localhost:8000"
$c.device_id  = "sim-pi"
$c.api_key    = "PASTE-THE-KEY"
$c | ConvertTo-Json -Depth 10 | Set-Content config.json
python sensor_client.py --simulate --check
python sensor_client.py --simulate --backfill-days 7
python sensor_client.py --simulate
```
If `sim-pi` already exists from the PHP version, use
`python manage.py rotate-key sim-pi` to get a new key, or just keep your old `config.json`
and change only `server_url`.

---

## 1b. Store everything in the cloud (free TiDB Cloud database)

Until now the database is XAMPP's MySQL on your laptop. This moves it to **TiDB Cloud
Starter**, a free, MySQL-compatible cloud database (no credit card). After this, every
sensor reading, user, attendance mark and prediction is stored in the cloud, and the
model reads its training data from the cloud.

1. **Create the database.** Go to **tidbcloud.com**, sign up (Google works), and create a
   free **Starter** instance (pick the region closest to India).
2. **Get the connection details.** On the instance page click **Connect** → choose the
   **public** endpoint → click **Generate Password**. Note **Host**, **Port** (4000),
   **User** (looks like `xxxxxxxx.root`) and the **password**.
3. **Point the backend at it** (in the `backend` folder):
   ```
   copy .env.example .env
   notepad .env
   ```
   Set these lines (remove the `#` in front of `DB_SSL`) and save:
   ```
   DB_HOST=gateway01....tidbcloud.com
   DB_PORT=4000
   DB_USER=xxxxxxxx.root
   DB_PASS=your-generated-password
   DB_NAME=nextjs_project
   DB_SSL=true
   ```
4. **Copy your existing data up** (XAMPP's MySQL still running):
   ```
   python copy_to_cloud.py
   ```
   It creates the database and tables in the cloud and prints how many rows of each
   table were copied. Running it again only adds missing rows.
5. **Restart the backend** (Ctrl+C, then `python app.py`). The website, the simulated Pi
   and Wokwi now all read and write the cloud database. Stop MySQL in XAMPP to prove it:
   everything keeps working.
6. **Check it in the cloud.** In the TiDB Cloud console open **SQL Editor** and run
   `SELECT COUNT(*) FROM nextjs_project.sensor_readings;` twice a minute apart: the
   number grows while a sensor is running.
7. **The model reads from the cloud too:** `python retrain_from_db.py --dry-run` in
   `ml-api` uses the same `backend/.env`, so it pairs predictions and measured waste
   stored in TiDB Cloud.

The backend and ML API still run on your laptop (or the Pi). To have no laptop in the
loop at all, run them on a cloud server too (section 5); the database settings stay the same.

---

## 2. Simulate the sensors on wokwi.com (no hardware)

`wokwi/` contains an ESP32 circuit with a **DHT22** and two **HX711 50 kg load cells**
(waste bin and rice drum), programmed in MicroPython. It uploads to the same
`/sensor_ingest` endpoint the Raspberry Pi uses.

**Why a tunnel is needed:** a Wokwi device reaches the internet through Wokwi's
servers, so it cannot see `localhost` on your laptop. (Wokwi's "Private Gateway"
can, but it is only for paying users.) A free Cloudflare quick tunnel gives your
backend a temporary public address.

1. Backend running (`python app.py` in `backend`).
2. Register the simulator:
   `python manage.py add-device wokwi-sim "Wokwi simulator" "Browser"` and copy the key.
3. Start a tunnel (new terminal):
   ```
   winget install --id Cloudflare.cloudflared
   cloudflared tunnel --url http://localhost:8000
   ```
   It prints an address like `https://random-words.trycloudflare.com`. Open
   `<that address>/health` in your browser: you should see `"status":"ok"`.
4. On **wokwi.com**, start a new **ESP32 → MicroPython** project.
   * Open the `diagram.json` tab and replace everything with `wokwi/diagram.json`.
   * Replace `main.py` with `wokwi/main.py` and fill in the three settings at the top:
     `SERVER_URL` (the trycloudflare address, no `/` at the end), `DEVICE_ID = "wokwi-sim"`, `API_KEY`.
5. Press the green **Play** button. The serial monitor shows
   `connected` → readings → `Uploaded ... stored ...`.
6. While it runs, click a load cell and drag its slider: raise **waste** (food thrown
   away), lower **rice** (rice taken for cooking). Click the DHT22 to change temperature.
   The dashboard's *Kitchen Sensors* panel shows "Wokwi simulator: online" and the new
   amounts within ~30 seconds.

Notes: the tunnel address changes every time you restart `cloudflared`, so update
`SERVER_URL`. Close the tunnel (Ctrl+C) when you're done: while it runs, anyone with the
address can reach your backend. Only send demo data through Wokwi's public gateway.

About the other simulators: **Tinkercad Circuits** only simulates Arduino boards and has
no internet, so it can show the wiring but can't send data. **Mokxi** lists a Raspberry
Pi 4 board and internet for Wi-Fi boards, but I couldn't confirm it has the HX711 or
DHT22 parts; Wokwi has both.

---

## 3. Hardware: Raspberry Pi wiring

| Part | Qty | Notes |
|---|---|---|
| Raspberry Pi 3B+, 4 or 5 + power supply + SD card | 1 | **Raspberry Pi OS 64-bit** |
| Load cell + HX711 board | 1 per scale | e.g. 100–200 kg platform/bar load cell for the waste bin |
| DHT22 module (or DHT11) | 1 | Kitchen temperature + humidity |

Power every HX711 and the DHT22 from **3.3 V** (their data pins go straight to GPIO).

| Sensor | VCC | GND | Data | Clock |
|---|---|---|---|---|
| DHT22 | 3.3 V (pin 1) | GND (9) | GPIO4 (7) | – |
| HX711 waste_bin | 3.3 V (17) | GND (30) | DT → GPIO5 (29) | SCK → GPIO6 (31) |
| HX711 leftover_pot | 3.3 V | GND (34) | DT → GPIO13 (33) | SCK → GPIO19 (35) |
| HX711 rice_store | 3.3 V | GND (39) | DT → GPIO20 (38) | SCK → GPIO21 (40) |
| HX711 dal_store | 3.3 V | GND (14) | DT → GPIO23 (16) | SCK → GPIO24 (18) |

Load cell → HX711 (common colours): red E+, black E−, white A−, green A+.

---

## 4. Run everything on the Raspberry Pi

This makes the Pi a complete edge server: sensors, backend, database and model on one board.

### 4.1 Base setup
```bash
sudo raspi-config        # Localisation → Timezone → Asia/Kolkata; connect Wi-Fi
echo "dtoverlay=dht11,gpiopin=4" | sudo tee -a /boot/firmware/config.txt   # older OS: /boot/config.txt
sudo apt update
sudo apt install -y mariadb-server python3-venv
sudo reboot
```
The `dht11` overlay also reads the DHT22. Test: `cat /sys/bus/iio/devices/iio:device0/in_temp_input`
(e.g. `27100` = 27.1 °C).

### 4.2 Database
```bash
sudo mysql -e "CREATE DATABASE nextjs_project;
  CREATE USER 'foodwaste'@'localhost' IDENTIFIED BY 'CHOOSE-A-PASSWORD';
  GRANT ALL PRIVILEGES ON nextjs_project.* TO 'foodwaste'@'localhost';"
# optional: bring your existing data from XAMPP (phpMyAdmin → Export → SQL)
mysql -u foodwaste -p nextjs_project < nextjs_project.sql
```

### 4.3 Project and Python packages
Copy the project to `/home/pi/foodwaste` (USB stick, `scp`, or git), then:
```bash
cd ~/foodwaste
python3 -m venv --system-site-packages ~/foodwaste-venv
~/foodwaste-venv/bin/pip install -r backend/requirements.txt -r ml-api/requirements.txt -r iot-device/requirements.txt
# GPIO library:  Pi 2/3/4 → sudo apt install -y python3-rpi.gpio
#                Pi 5     → ~/foodwaste-venv/bin/pip install rpi-lgpio
cp backend/.env.example backend/.env
nano backend/.env                      # DB_USER=foodwaste, DB_PASS=...
~/foodwaste-venv/bin/python backend/manage.py init-db
```

### 4.4 Start backend + ML API at boot
```bash
sudo cp deploy/foodwaste-backend.service deploy/foodwaste-ml.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now foodwaste-backend foodwaste-ml
curl http://localhost:8000/health
```
(The service files assume user `pi` and the folders above; edit them if yours differ.)

### 4.5 Sensors on the same Pi
```bash
cd ~/foodwaste/backend
~/foodwaste-venv/bin/python manage.py add-device kitchen-pi-1 "Mess kitchen Pi" "Kitchen"
cd ~/foodwaste/iot-device
cp config.example.json config.json
nano config.json         # server_url = http://localhost:8000, device_id, api_key
~/foodwaste-venv/bin/python calibrate_scale.py --name waste_bin     # repeat for every scale
~/foodwaste-venv/bin/python sensor_client.py --check
sudo cp foodwaste-sensor.service /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now foodwaste-sensor
```
Calibration: put the **empty** container on the scale → Enter; put a known weight on it
(a sealed 5 kg rice bag) → type `5`.

### 4.6 Website
Building the Next.js site on a Pi is slow, so run it on a laptop on the same Wi-Fi and
point `.env.local` at the Pi (find its address with `hostname -I`):
```
NEXT_PUBLIC_BACKEND_URL=http://192.168.1.50:8000
NEXT_PUBLIC_ATTENDANCE_API_URL=http://192.168.1.50:8000/attendance
NEXT_PUBLIC_DASHBOARD_API_URL=http://192.168.1.50:8000/dashboard-data
NEXT_PUBLIC_WASTE_API_URL=http://192.168.1.50:8000/predict
NEXT_PUBLIC_PREDICTION_SAVE_URL=http://192.168.1.50:8000/save_prediction
```
(A Pi 4/5 with 4 GB+ can also run `npm ci && npm run build && npm start` itself.)

### 4.7 Nightly retraining
```bash
crontab -e
30 23 * * * cd /home/pi/foodwaste/ml-api && /home/pi/foodwaste-venv/bin/python retrain_from_db.py >> retrain.log 2>&1
```

**Sensors on the Pi, backend on your laptop instead?** Run the backend on the laptop,
set `server_url` on the Pi to `http://<laptop-ip>:8000`, and allow the port once in an
Administrator PowerShell:
`New-NetFirewallRule -DisplayName "Food waste backend" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow`.
College Wi-Fi often blocks devices from reaching each other; a phone hotspot works.

---

## 5. Cloud server (optional)

An Ubuntu server is set up exactly like section 4 (skip the GPIO and DHT lines, change
`User=pi` and paths in the service files). Open port 8000 in the provider's firewall, or
put nginx with HTTPS (certbot) in front of it. Point the Pi's `server_url` and the
website's `NEXT_PUBLIC_*` addresses at the server. Never open ports 3306 or 5000.

---

## 6. How the numbers are worked out (for your report)

**On the Pi.** Each scale is read every 5 s (median of 7 HX711 samples). A reading is
kept only when the last 3 agree within 50 g, and uploaded when it changed by ≥ 100 g,
plus a heartbeat every 5 min. Temperature/humidity every 5 min. Everything goes into a
local SQLite queue first and is uploaded in batches every 30 s; each reading has a unique
id, so retried uploads never create duplicates. If the network is down, readings wait
and are sent later.

**In the backend.** Consecutive readings of each scale are compared (SQL `LAG`):
waste / leftovers for a meal = sum of **increases** ≥ 50 g (a drop = bin emptied);
raw material used = sum of **decreases** ≥ 50 g (a rise = restocked). In a test with
simulated data the totals were within 0.03 kg per meal of the true amounts. Devices
without a clock (the Wokwi ESP32) may omit the time; the backend then uses its own clock
and meal times (breakfast 05–10, lunch 10–16, dinner 16–24).

**Model feedback loop** (`retrain_from_db.py`): pairs each saved prediction with the
waste measured for that meal, reports live prediction error, retrains the same model
type on the CSV + real meals (each real meal counts 3×), and replaces
`waste_model.pkl` only if it is at least as accurate (backup in `model_backups/`).
The ML API picks up the new model automatically. Needs ≥ 5 matched meals.

---

## 7. API reference (backend, port 8000)

| Endpoint | Method | Purpose |
|---|---|---|
| `/login`, `/register` | POST | Same answers as the old PHP files |
| `/attendance?action=mark\|mine\|all` | POST/GET | Student attendance |
| `/predict` | POST | Forwards to the ML API |
| `/save_prediction` | POST | Stores a prediction in `waste_predictions` |
| `/dashboard-data` | GET | Forwards to the ML API |
| `/sensor_ingest` | POST | Device upload. Headers `X-Device-Id`, `X-Api-Key`. Body `{"readings":[{uid, sensor, type, item, value, recorded_at?, meal_date?, meal_type?}]}`, max 500. Empty list = ping |
| `/sensor_data?action=status` | GET | Devices online/offline, latest reading per sensor |
| `/sensor_data?action=meals&days=7` | GET | Per-meal waste, leftovers, raw material, temp/humidity + daily totals |
| `/prediction_context?date=YYYY-MM-DD&meal=lunch` | GET | Suggested form values with their sources |
| `/health` | GET | `{"status":"ok"}` |

---

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| `Database connection failed` / `Cannot connect to the database` | MySQL not running (XAMPP / `sudo systemctl status mariadb`), or wrong `DB_*` in `backend/.env` |
| Cloud database: `SSL is required` / `Access denied` / `insecure transport` | Set `DB_SSL=true`; copy user and password again from the cloud console (TiDB user names include the `xxxx.root` prefix) |
| Website: "Failed to fetch" / login does nothing | Backend not running on port 8000, or `.env.local` points elsewhere (restart `npm run dev` after editing) |
| Predict: "Could not reach model API" | Start the ML API (`python app.py` in `ml-api`) |
| `Server login: FAILED (401)` | `device_id`/`api_key` don't match; `python manage.py rotate-key <device_id>` |
| `ModuleNotFoundError: zoneinfo/tzdata` on Windows | `pip install -r requirements.txt` in `backend` |
| HX711 *not responding* | Wiring/power; DT and SCK swapped; BCM pin numbers |
| `No DHT sensor found` | Add the `dtoverlay=dht11,gpiopin=4` line and reboot |
| GPIO errors on a Pi 5 | `pip install rpi-lgpio` in the venv |
| Wokwi: `Upload failed` | Tunnel not running, or `SERVER_URL` is the old tunnel address |
| Times are 5.5 h off | Set Asia/Kolkata on the Pi/server; keep `DB_TIMEZONE=+05:30` |

**Security note.** Like the original PHP version, login has no server-side session, so
read endpoints (attendance, sensor data) answer anyone who can reach port 8000. Fine
for a project on a local network; add proper authentication before exposing real student
data on the internet.
