'use client'

import { useCallback, useEffect, useState } from 'react'
import { Cpu, Droplets, RefreshCw, Scale, Thermometer, Trash2, Wheat } from 'lucide-react'
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'
import { SENSOR_API_URL } from '@/lib/iot'

type Device = {
  device_id: string
  name: string
  location: string | null
  online: boolean
  last_seen_at: string | null
  seconds_since_seen: number | null
}

type StatusPayload = {
  devices: Device[]
  environment: { temperature: number | null; humidity: number | null; recorded_at: string | null }
}

type Meal = {
  date: string
  meal_type: string
  waste_kg: number | null
  leftover_kg: number | null
  raw_material_kg: Record<string, number>
  raw_material_total_kg: number | null
  temperature_avg: number | null
  humidity_avg: number | null
}

type DailyPoint = {
  date: string
  waste_kg: number | null
  leftover_kg: number | null
  raw_material_total_kg: number | null
}

type MealsPayload = { meals: Meal[]; daily: DailyPoint[] }

const REFRESH_MS = 30_000

function kg(value: number | null | undefined) {
  return value === null || value === undefined ? '--' : `${value} kg`
}

function lastSeen(seconds: number | null) {
  if (seconds === null) return 'never'
  if (seconds < 60) return `${seconds}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`
  return `${Math.round(seconds / 86400)} days ago`
}

function todayIso() {
  const d = new Date()
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
}

export function SensorPanel() {
  const [status, setStatus] = useState<StatusPayload | null>(null)
  const [meals, setMeals] = useState<MealsPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const load = useCallback(async () => {
    setRefreshing(true)
    try {
      const [statusRes, mealsRes] = await Promise.all([
        fetch(`${SENSOR_API_URL}?action=status`),
        fetch(`${SENSOR_API_URL}?action=meals&days=7`),
      ])
      const statusData = await statusRes.json()
      const mealsData = await mealsRes.json()
      if (!statusRes.ok || !statusData.success) throw new Error(statusData.message || 'Could not load sensor status')
      if (!mealsRes.ok || !mealsData.success) throw new Error(mealsData.message || 'Could not load sensor data')
      setStatus(statusData)
      setMeals(mealsData)
      setError(null)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sensor API is not reachable')
    } finally {
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    load()
    const timer = setInterval(load, REFRESH_MS)
    return () => clearInterval(timer)
  }, [load])

  const today = todayIso()
  const todayMeals = meals?.meals.filter((m) => m.date === today) ?? []
  const sum = (field: 'waste_kg' | 'leftover_kg' | 'raw_material_total_kg') => {
    const values = todayMeals.map((m) => m[field]).filter((v): v is number => v !== null)
    return values.length ? Math.round(values.reduce((a, b) => a + b, 0) * 100) / 100 : null
  }
  const chartData = (meals?.daily ?? []).map((d) => ({
    day: new Date(`${d.date}T00:00:00`).toLocaleDateString(undefined, { weekday: 'short', day: 'numeric' }),
    waste: d.waste_kg ?? 0,
    leftover: d.leftover_kg ?? 0,
  }))
  const recentMeals = (meals?.meals ?? []).filter((m) => m.meal_type !== 'other').slice(0, 8)
  const devices = status?.devices ?? []
  const env = status?.environment

  return (
    <section className="mb-8 bg-white rounded-xl shadow p-6">
      <div className="flex flex-wrap items-center justify-between gap-4 mb-6">
        <div>
          <h2 className="text-xl font-bold text-gray-900">Kitchen Sensors (Live)</h2>
          <p className="text-sm text-gray-600 mt-1">Measured by the Raspberry Pi scales and temperature sensor</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {devices.map((d) => (
            <span
              key={d.device_id}
              title={d.location ?? undefined}
              className={`inline-flex items-center gap-2 rounded-full px-3 py-1 text-xs font-semibold ${
                d.online ? 'bg-emerald-50 text-emerald-700' : 'bg-gray-100 text-gray-600'
              }`}
            >
              <Cpu className="h-3.5 w-3.5" />
              {d.name}: {d.online ? 'online' : `offline (${lastSeen(d.seconds_since_seen)})`}
            </span>
          ))}
          <button
            onClick={load}
            className="inline-flex items-center gap-1 rounded-lg border border-gray-200 px-3 py-1 text-xs font-semibold text-gray-600 hover:bg-gray-50"
          >
            <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} /> Refresh
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-6 rounded-lg border border-amber-200 bg-amber-50 p-4 text-sm text-amber-800">
          {error}. The charts below use the training dataset until sensor data arrives.
        </div>
      )}

      {!error && status && devices.length === 0 && (
        <div className="mb-6 rounded-lg border border-gray-200 bg-gray-50 p-4 text-sm text-gray-600">
          No Raspberry Pi registered yet. Run <code className="font-mono">python manage.py add-device</code> in the backend folder, then
          start the sensor client on the Pi (see IOT_SETUP.md).
        </div>
      )}

      <div className="grid gap-4 md:grid-cols-4 mb-8">
        <div className="rounded-xl border border-gray-100 p-5">
          <p className="flex items-center gap-2 text-sm font-medium text-gray-600"><Trash2 className="h-4 w-4 text-red-500" /> Waste today</p>
          <p className="mt-2 text-3xl font-bold text-gray-900">{kg(sum('waste_kg'))}</p>
        </div>
        <div className="rounded-xl border border-gray-100 p-5">
          <p className="flex items-center gap-2 text-sm font-medium text-gray-600"><Scale className="h-4 w-4 text-amber-500" /> Leftovers today</p>
          <p className="mt-2 text-3xl font-bold text-gray-900">{kg(sum('leftover_kg'))}</p>
        </div>
        <div className="rounded-xl border border-gray-100 p-5">
          <p className="flex items-center gap-2 text-sm font-medium text-gray-600"><Wheat className="h-4 w-4 text-emerald-600" /> Raw material used today</p>
          <p className="mt-2 text-3xl font-bold text-gray-900">{kg(sum('raw_material_total_kg'))}</p>
        </div>
        <div className="rounded-xl border border-gray-100 p-5">
          <p className="flex items-center gap-2 text-sm font-medium text-gray-600"><Thermometer className="h-4 w-4 text-blue-500" /> Kitchen now</p>
          <p className="mt-2 text-3xl font-bold text-gray-900">
            {env?.temperature ?? '--'}°C
          </p>
          <p className="mt-1 flex items-center gap-1 text-sm text-gray-500">
            <Droplets className="h-4 w-4" /> {env?.humidity ?? '--'}% humidity
          </p>
        </div>
      </div>

      <div className="grid gap-8 lg:grid-cols-2">
        <div>
          <h3 className="mb-4 font-semibold text-gray-900">Measured waste, last 7 days</h3>
          <ResponsiveContainer width="100%" height={260}>
            <BarChart data={chartData}>
              <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
              <XAxis dataKey="day" />
              <YAxis unit=" kg" width={60} />
              <Tooltip formatter={(value: number) => `${value} kg`} />
              <Legend />
              <Bar dataKey="waste" name="Plate waste" fill="#ef4444" radius={[6, 6, 0, 0]} />
              <Bar dataKey="leftover" name="Leftovers" fill="#f59e0b" radius={[6, 6, 0, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>

        <div>
          <h3 className="mb-4 font-semibold text-gray-900">Recent meals</h3>
          {recentMeals.length === 0 ? (
            <p className="py-10 text-center text-sm text-gray-500">No meals measured yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[480px] text-left text-sm">
                <thead className="border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500">
                  <tr>
                    <th className="px-2 py-2 font-semibold">Meal</th>
                    <th className="px-2 py-2 font-semibold">Waste</th>
                    <th className="px-2 py-2 font-semibold">Leftover</th>
                    <th className="px-2 py-2 font-semibold">Raw material used</th>
                    <th className="px-2 py-2 font-semibold">Temp</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {recentMeals.map((m) => (
                    <tr key={`${m.date}-${m.meal_type}`}>
                      <td className="px-2 py-3 text-gray-900">
                        <span className="font-medium capitalize">{m.meal_type}</span>
                        <span className="block text-xs text-gray-500">
                          {new Date(`${m.date}T00:00:00`).toLocaleDateString()}
                        </span>
                      </td>
                      <td className="px-2 py-3 text-gray-700">{kg(m.waste_kg)}</td>
                      <td className="px-2 py-3 text-gray-700">{kg(m.leftover_kg)}</td>
                      <td className="px-2 py-3 text-gray-700">
                        {Object.keys(m.raw_material_kg).length === 0
                          ? '--'
                          : Object.entries(m.raw_material_kg)
                              .map(([item, amount]) => `${item} ${amount} kg`)
                              .join(', ')}
                      </td>
                      <td className="px-2 py-3 text-gray-700">
                        {m.temperature_avg === null ? '--' : `${m.temperature_avg}°C`}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </div>
    </section>
  )
}
