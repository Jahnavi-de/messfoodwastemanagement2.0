"""
Retrain the waste model with REAL waste measured by the kitchen sensors.

How the feedback loop works
  1. Before a meal, staff run a prediction on the website. The backend's /save_prediction
     stores the inputs (students, attendance, menu ...) in `waste_predictions`.
  2. During the meal the Raspberry Pi weighs the waste bin; the readings land in
     `sensor_readings` in the cloud database.
  3. This script pairs each meal's inputs with the waste actually measured for
     that meal, adds those rows to Final_data_2years.csv (with extra weight, so
     the model adapts to *this* hostel), retrains the same model type, and only
     replaces waste_model.pkl if the new model is at least as accurate.
  4. app.py notices the new waste_model.pkl and starts using it, no restart needed.

Run it by hand, or nightly with cron on the cloud server:
    python retrain_from_db.py                       # reads DB settings from ../backend/.env
    python retrain_from_db.py --dry-run             # show what would happen, change nothing
    python retrain_from_db.py --sensor-weight 5     # trust sensor data more
    python retrain_from_db.py --force               # replace the model even if not better

Database settings: DB_HOST, DB_PORT, DB_USER, DB_PASS, DB_NAME (same names as the
PHP backend), from the environment or from --env-file.
"""

import argparse
import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

import joblib
import numpy as np
import pandas as pd
from sklearn.base import clone
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score
from sklearn.model_selection import train_test_split

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
MODEL_PATH = os.path.join(BASE_DIR, 'waste_model.pkl')
METADATA_PATH = os.path.join(BASE_DIR, 'model_metadata.json')
DATASET_PATH = os.path.join(BASE_DIR, 'Final_data_2years.csv')
SENSOR_ROWS_PATH = os.path.join(BASE_DIR, 'sensor_training_rows.csv')
BACKUP_DIR = os.path.join(BASE_DIR, 'model_backups')

# Same feature order as train_model.py / app.py
FEATURES = [
    'Students_enrolled', 'Attendance_percent', 'Special_event_enc', 'Menu_count',
    'Previous_day_leftover_kg', 'Nonveg_items', 'Meal_type_enc', 'Day_enc', 'People_served',
]
DAY_ORDER = {'Monday': 0, 'Tuesday': 1, 'Wednesday': 2, 'Thursday': 3, 'Friday': 4, 'Saturday': 5, 'Sunday': 6}

# Must match meal_summary() in backend/iot.py: plate waste per meal =
# sum of weight increases of the waste bin (drops = bin emptied, ignored).
MEASURED_WASTE_SQL = """
    SELECT meal_date, meal_type,
           SUM(CASE WHEN delta >= %(noise)s THEN delta ELSE 0 END) AS waste_kg,
           COUNT(*) AS readings
    FROM (
        SELECT meal_date, meal_type,
               value - LAG(value) OVER (PARTITION BY device_id, sensor_name, sensor_type ORDER BY recorded_at, id) AS delta
        FROM sensor_readings
        WHERE sensor_type = 'waste_weight'
    ) w
    WHERE delta IS NOT NULL AND meal_type IN ('breakfast', 'lunch', 'dinner')
    GROUP BY meal_date, meal_type
"""

# The latest prediction made for each date + meal holds the inputs the model saw.
PREDICTION_INPUTS_SQL = """
    SELECT p.date, LOWER(p.meal_type) AS meal_type, p.day_of_week, p.students_enrolled,
           p.average_attendance, p.special_events, p.number_of_menus, p.leftover_previous_day,
           p.menu_items, p.predicted_waste
    FROM waste_predictions p
    JOIN (SELECT MAX(id) AS id FROM waste_predictions GROUP BY date, LOWER(meal_type)) latest
      ON latest.id = p.id
"""


def load_env_file(path):
    if not path or not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            key, value = line.split('=', 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"\''))


def connect():
    try:
        import pymysql
    except ImportError:
        sys.exit("pymysql is missing: pip install -r requirements.txt")
    ssl = None
    if os.environ.get('DB_SSL_CA'):
        ssl = {'ca': os.environ['DB_SSL_CA']}
    elif os.environ.get('DB_SSL', '').lower() in ('1', 'true', 'yes', 'on'):
        import certifi  # cloud databases need an encrypted connection
        ssl = {'ca': certifi.where()}
    return pymysql.connect(
        host=os.environ.get('DB_HOST', 'localhost'),
        port=int(os.environ.get('DB_PORT', 3306)),
        user=os.environ.get('DB_USER', 'root'),
        password=os.environ.get('DB_PASS', ''),
        database=os.environ.get('DB_NAME', 'nextjs_project'),
        ssl=ssl,
        cursorclass=pymysql.cursors.DictCursor,
    )


def fetch_sensor_rows(conn, noise_kg):
    with conn.cursor() as cur:
        cur.execute(MEASURED_WASTE_SQL, {'noise': noise_kg})
        measured = pd.DataFrame(cur.fetchall())
        cur.execute(PREDICTION_INPUTS_SQL)
        inputs = pd.DataFrame(cur.fetchall())
    if measured.empty or inputs.empty:
        return pd.DataFrame(), len(measured)

    measured['meal_date'] = measured['meal_date'].astype(str)
    inputs['date'] = inputs['date'].astype(str)
    rows = inputs.merge(measured, left_on=['date', 'meal_type'], right_on=['meal_date', 'meal_type'])
    rows = rows[(rows['students_enrolled'] > 0) & (rows['waste_kg'] > 0)].copy()
    if rows.empty:
        return pd.DataFrame(), len(measured)

    def nonveg_count(items):
        try:
            return sum(1 for i in json.loads(items or '[]') if i == 'nonveg')
        except (TypeError, ValueError):
            return 0

    out = pd.DataFrame({
        'Date': rows['date'],
        'Meal_type': rows['meal_type'],
        'Students_enrolled': rows['students_enrolled'].astype(float),
        'Attendance_percent': rows['average_attendance'].astype(float),
        'Special_event_enc': rows['special_events'].astype(str).str.lower().isin(['yes', 'true', '1']).astype(int),
        'Menu_count': rows['number_of_menus'].astype(float),
        'Previous_day_leftover_kg': rows['leftover_previous_day'].astype(float),
        'Nonveg_items': rows['menu_items'].map(nonveg_count).astype(float),
        # app.py encodes dinner/snacks as 1 and everything else as 0
        'Meal_type_enc': rows['meal_type'].isin(['dinner', 'snacks']).astype(int),
        'Day_enc': rows['day_of_week'].astype(str).str.capitalize().map(DAY_ORDER).fillna(0).astype(int),
        'Predicted_waste_kg': rows['predicted_waste'].astype(float),
        'Total_Waste_kg': rows['waste_kg'].astype(float).round(2),
    })
    out['People_served'] = (out['Students_enrolled'] * out['Attendance_percent'] / 100).round()
    return out.reset_index(drop=True), len(measured)


def load_csv_rows():
    df = pd.read_csv(DATASET_PATH)
    df['Day_enc'] = df['Day'].map(DAY_ORDER)
    df['Meal_type_enc'] = (df['Meal_type'] == 'Dinner').astype(int)
    df['Special_event_enc'] = (df['Special_event'] == 'Yes').astype(int)
    df['People_served'] = (df['Students_enrolled'] * df['Attendance_percent'] / 100).round()
    return df


def fit(model, X, y, weights):
    try:
        model.fit(X, y, sample_weight=weights)
    except TypeError:  # e.g. KNN has no sample weights
        model.fit(X, y)
    return model


def scores(model, X, y):
    pred = model.predict(X)
    return {
        'mae': round(float(mean_absolute_error(y, pred)), 4),
        'rmse': round(float(np.sqrt(mean_squared_error(y, pred))), 4),
        'r2': round(float(r2_score(y, pred)), 4) if len(y) > 1 else None,
    }


def main():
    p = argparse.ArgumentParser(description='Retrain the waste model with sensor-measured waste')
    p.add_argument('--env-file', default=os.path.join(BASE_DIR, '..', 'backend', '.env'))
    p.add_argument('--sensor-weight', type=float, default=3.0, help='how much one real meal counts vs one CSV row')
    p.add_argument('--min-rows', type=int, default=5, help='need at least this many measured meals')
    p.add_argument('--noise-kg', type=float, default=float(os.environ.get('SENSOR_NOISE_KG', 0.05)))
    p.add_argument('--tolerance', type=float, default=0.02, help='accept a new model up to 2%% worse MAE')
    p.add_argument('--force', action='store_true', help='replace the model even if it is not better')
    p.add_argument('--dry-run', action='store_true', help='train and report, but do not replace any file')
    args = p.parse_args()

    load_env_file(args.env_file)
    conn = connect()
    try:
        sensor, measured_meals = fetch_sensor_rows(conn, args.noise_kg)
    finally:
        conn.close()

    print(f"Meals with measured waste: {measured_meals}; matched with a saved prediction: {len(sensor)}")
    if len(sensor) < args.min_rows:
        print(f"Need at least {args.min_rows} matched meals to retrain. Keep predicting before each meal "
              f"and let the sensors measure; nothing changed.")
        return 0

    live_mae = float(mean_absolute_error(sensor['Total_Waste_kg'], sensor['Predicted_waste_kg']))
    print(f"How far the live predictions were from measured waste (MAE): {live_mae:.2f} kg")

    csv = load_csv_rows()
    X_csv, y_csv = csv[FEATURES].to_numpy(float), csv['Total_Waste_kg'].to_numpy(float)
    X_sen, y_sen = sensor[FEATURES].to_numpy(float), sensor['Total_Waste_kg'].to_numpy(float)

    # Same CSV split as train_model.py, so the CSV test rows are unseen by the current model too.
    Xc_tr, Xc_te, yc_tr, yc_te = train_test_split(X_csv, y_csv, test_size=0.2, random_state=42)
    Xs_tr, Xs_te, ys_tr, ys_te = train_test_split(X_sen, y_sen, test_size=0.25, random_state=42)

    current = joblib.load(MODEL_PATH)
    candidate = fit(
        clone(current),
        np.vstack([Xc_tr, Xs_tr]),
        np.concatenate([yc_tr, ys_tr]),
        np.concatenate([np.ones(len(yc_tr)), np.full(len(ys_tr), args.sensor_weight)]),
    )

    X_te, y_te = np.vstack([Xc_te, Xs_te]), np.concatenate([yc_te, ys_te])
    old = {'all': scores(current, X_te, y_te), 'sensor': scores(current, Xs_te, ys_te)}
    new = {'all': scores(candidate, X_te, y_te), 'sensor': scores(candidate, Xs_te, ys_te)}

    print(f"\nModel: {type(current).__name__}   test rows: {len(yc_te)} CSV + {len(ys_te)} measured meals")
    print("                      current model    retrained model")
    print(f"  MAE, all test rows  {old['all']['mae']:>10.3f} kg   {new['all']['mae']:>10.3f} kg")
    print(f"  MAE, real meals     {old['sensor']['mae']:>10.3f} kg   {new['sensor']['mae']:>10.3f} kg")

    better = new['all']['mae'] <= old['all']['mae'] * (1 + args.tolerance)
    if not better and not args.force:
        print("\nThe retrained model is not more accurate, so the current model stays. (--force to override)")
        return 0
    if args.dry_run:
        print("\n--dry-run: the model would be replaced, but no files were changed.")
        return 0

    # Final model: learn from every row we have.
    final = fit(
        clone(current),
        np.vstack([X_csv, X_sen]),
        np.concatenate([y_csv, y_sen]),
        np.concatenate([np.ones(len(y_csv)), np.full(len(y_sen), args.sensor_weight)]),
    )

    with open(METADATA_PATH) as f:
        metadata = json.load(f)
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    os.makedirs(BACKUP_DIR, exist_ok=True)
    shutil.copy2(MODEL_PATH, os.path.join(BACKUP_DIR, f'waste_model_{stamp}.pkl'))
    shutil.copy2(METADATA_PATH, os.path.join(BACKUP_DIR, f'model_metadata_{stamp}.json'))

    metadata['metrics'] = {k: v for k, v in new['all'].items() if v is not None}
    metadata['retraining'] = {
        'trained_at': datetime.now().isoformat(timespec='seconds'),
        'csv_rows': int(len(y_csv)),
        'sensor_rows': int(len(y_sen)),
        'sensor_weight': args.sensor_weight,
        'live_prediction_mae_kg': round(live_mae, 3),
        'test_mae_before': old['all']['mae'],
        'test_mae_after': new['all']['mae'],
        'forced': bool(args.force and not better),
    }
    metadata['trained_at'] = metadata['retraining']['trained_at']
    if hasattr(final, 'feature_importances_'):
        metadata['feature_importances'] = dict(zip(FEATURES, final.feature_importances_.tolist()))

    # Metadata first, then the model (app.py reloads when the .pkl changes).
    # Write to a temp file and rename so app.py never reads a half-written file.
    for path, writer in (
        (METADATA_PATH, lambda f: f.write(json.dumps(metadata, indent=2).encode())),
        (MODEL_PATH, lambda f: joblib.dump(final, f)),
    ):
        fd, tmp = tempfile.mkstemp(dir=BASE_DIR, suffix='.tmp')
        with os.fdopen(fd, 'wb') as f:
            writer(f)
        os.replace(tmp, path)

    sensor.to_csv(SENSOR_ROWS_PATH, index=False)
    print(f"\nModel replaced (backup in model_backups/, training rows in {os.path.basename(SENSOR_ROWS_PATH)}).")
    print("app.py loads it automatically on the next request.")
    return 0


if __name__ == '__main__':
    sys.exit(main())
