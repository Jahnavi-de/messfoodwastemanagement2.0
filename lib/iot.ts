// Addresses of the IoT endpoints in the Python backend (backend/iot.py).
// Each can be overridden in .env.local; by default they sit next to the other PHP files.
const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ?? 'http://localhost:8000'

export const SENSOR_API_URL = process.env.NEXT_PUBLIC_SENSOR_API_URL ?? `${BACKEND_URL}/sensor_data`

export const PREDICTION_CONTEXT_URL =
  process.env.NEXT_PUBLIC_PREDICTION_CONTEXT_URL ?? `${BACKEND_URL}/prediction_context`
