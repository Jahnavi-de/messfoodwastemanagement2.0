'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import Link from 'next/link'
import { ArrowLeft, TrendingDown, AlertCircle, CheckCircle2, CalendarCheck } from 'lucide-react'
import { LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, Legend, ResponsiveContainer } from 'recharts'
import { useAuth } from '@/lib/auth-context'
import { SensorPanel } from '@/components/sensor-panel'

const DASHBOARD_API_URL = process.env.NEXT_PUBLIC_DASHBOARD_API_URL ?? 'http://localhost:8000/dashboard-data'
const ATTENDANCE_API_URL = process.env.NEXT_PUBLIC_ATTENDANCE_API_URL ?? 'http://localhost:8000/attendance'

type WeeklyTrendPoint = {
  date: string
  waste: number
  target: number
}

type MealWastePoint = {
  meal: string
  waste: number
}

type DashboardAlert = {
  title: string
  message: string
  severity: 'high' | 'medium'
}

type DashboardRecommendation = {
  title: string
  message: string
}

type DashboardPayload = {
  summary: {
    total_waste: number
    average_daily_waste: number
    estimated_cost: number
    efficiency_score: number
    confidence: number
  }
  weekly_trend: WeeklyTrendPoint[]
  meal_waste: MealWastePoint[]
  alerts: DashboardAlert[]
  recommendations: DashboardRecommendation[]
}

type AttendanceRecord = {
  id: string
  username: string
  email: string
  attendance_date: string
  status: 'present'
  marked_at: string
}

export default function DashboardPage() {
  const { user, isLoading } = useAuth()
  const router = useRouter()
  const [dashboardData, setDashboardData] = useState<DashboardPayload | null>(null)
  const [attendanceRecords, setAttendanceRecords] = useState<AttendanceRecord[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (isLoading) return // wait until the saved login is read, otherwise a page refresh bounces to /login
    if (!user) {
      router.push('/login')
      return
    }
    if (user.role === 'student') {
      router.replace('/attendance')
      return
    }

    let cancelled = false

    const loadDashboard = async () => {
      try {
        setLoading(true)
        setError(null)

        const response = await fetch(DASHBOARD_API_URL)
        const data = await response.json()

        if (!response.ok) {
          throw new Error(data?.error || 'Could not load dashboard data')
        }

        if (!cancelled) {
          setDashboardData(data)
        }
      } catch (err: any) {
        if (!cancelled) {
          setError(err.message ?? 'Could not load dashboard data')
        }
      } finally {
        if (!cancelled) {
          setLoading(false)
        }
      }
    }

    loadDashboard()

    return () => {
      cancelled = true
    }
  }, [user, isLoading, router])

  useEffect(() => {
    if (!user || user.role === 'student') return

    const loadAttendance = async () => {
      try {
        const response = await fetch(`${ATTENDANCE_API_URL}?action=all`)
        const data = await response.json()
        if (response.ok && data.success) setAttendanceRecords(data.records ?? [])
      } catch {
        // Dashboard analytics remain available if attendance API is offline.
      }
    }

    loadAttendance()
  }, [user])

  if (!user) return null

  const userPredictionCount = user.predictions?.length || 0
  const summary = dashboardData?.summary
  const weeklyTrend = dashboardData?.weekly_trend ?? []
  const mealWaste = dashboardData?.meal_waste ?? []
  const alerts = dashboardData?.alerts ?? []
  const recommendations = dashboardData?.recommendations ?? []

  return (
    <main className="min-h-screen bg-gradient-to-br from-emerald-50 via-white to-emerald-50 py-12">
      <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8">
        <div className="mb-8">
          <div className="flex items-center justify-between mb-4">
            <div>
              <h1 className="text-4xl font-bold text-gray-900">
                Welcome, <span className="text-emerald-600">{user.name}</span>
              </h1>
              <p className="text-gray-600 text-lg mt-1">
                Dashboard for <span className="font-semibold">{(user as any).hostelName ?? 'Your Hostel'}</span>
              </p>
            </div>
            <div className="flex items-center gap-5">
              <Link href="/attendance" className="inline-flex items-center gap-2 border border-emerald-600 px-4 py-2 text-sm font-semibold text-emerald-700 transition hover:bg-emerald-50">
                <CalendarCheck className="h-4 w-4" /> Attendance
              </Link>
              <div className="text-right">
                <p className="text-sm text-gray-600 mb-1">Predictions Made</p>
                <p className="text-3xl font-bold text-emerald-600">{userPredictionCount}</p>
              </div>
            </div>
          </div>
        </div>

        {error && (
          <div className="mb-8 rounded-xl border border-red-200 bg-red-50 p-4 text-red-700">
            {error}
          </div>
        )}

        <div className="grid md:grid-cols-4 gap-4 mb-8">
          <div className="bg-white rounded-xl shadow p-6 border-l-4 border-emerald-500">
            <p className="text-gray-600 text-sm font-medium mb-2">Total Waste</p>
            <p className="text-3xl font-bold text-gray-900">{loading ? '--' : `${summary?.total_waste ?? 0} kg`}</p>
            <p className="text-emerald-600 text-sm mt-2 flex items-center gap-1">
              <TrendingDown className="w-4 h-4" /> Dataset-driven model graph
            </p>
          </div>

          <div className="bg-white rounded-xl shadow p-6 border-l-4 border-blue-500">
            <p className="text-gray-600 text-sm font-medium mb-2">Average Daily Waste</p>
            <p className="text-3xl font-bold text-gray-900">{loading ? '--' : `${summary?.average_daily_waste ?? 0} kg`}</p>
            <p className="text-gray-500 text-sm mt-2">Target: 30 kg/day</p>
          </div>

          <div className="bg-white rounded-xl shadow p-6 border-l-4 border-amber-500">
            <p className="text-gray-600 text-sm font-medium mb-2">Estimated Cost</p>
            <p className="text-3xl font-bold text-gray-900">{loading ? '--' : `Rs. ${summary?.estimated_cost ?? 0}`}</p>
            <p className="text-amber-600 text-sm mt-2">Rs. 25 per kg</p>
          </div>

          <div className="bg-white rounded-xl shadow p-6 border-l-4 border-purple-500">
            <p className="text-gray-600 text-sm font-medium mb-2">Efficiency Score</p>
            <p className="text-3xl font-bold text-gray-900">{loading ? '--' : `${summary?.efficiency_score ?? 0}%`}</p>
            <p className="text-purple-600 text-sm mt-2">
              <CheckCircle2 className="w-4 h-4 inline mr-1" /> Model confidence {summary?.confidence ?? 0}%
            </p>
          </div>
        </div>

        <SensorPanel />

        <div className="grid md:grid-cols-2 gap-8 mb-8">
          <div className="bg-white rounded-xl shadow p-6">
            <h2 className="text-xl font-bold text-gray-900 mb-6">Weekly Waste Trend</h2>
            <ResponsiveContainer width="100%" height={300}>
              <LineChart data={weeklyTrend}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
                <XAxis dataKey="date" />
                <YAxis />
                <Tooltip />
                <Legend />
                <Line
                  type="monotone"
                  dataKey="waste"
                  stroke="#10b981"
                  strokeWidth={2}
                  name="Average Waste"
                  dot={{ fill: '#10b981', r: 5 }}
                  activeDot={{ r: 7 }}
                />
                <Line
                  type="monotone"
                  dataKey="target"
                  stroke="#d1d5db"
                  strokeWidth={2}
                  strokeDasharray="5 5"
                  name="Target"
                  dot={{ fill: '#d1d5db', r: 4 }}
                />
              </LineChart>
            </ResponsiveContainer>
          </div>

          <div className="bg-white rounded-xl shadow p-6">
            <h2 className="text-xl font-bold text-gray-900 mb-6">Waste by Meal Type</h2>
            <ResponsiveContainer width="100%" height={300}>
              <BarChart data={mealWaste}>
                <CartesianGrid strokeDasharray="3 3" stroke="#e5e7eb" />
                <XAxis dataKey="meal" />
                <YAxis />
                <Tooltip />
                <Bar dataKey="waste" fill="#10b981" radius={[8, 8, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>

        <div className="grid md:grid-cols-2 gap-8">
          <div className="bg-white rounded-xl shadow p-6">
            <h2 className="text-xl font-bold text-gray-900 mb-6">Alerts</h2>
            <div className="space-y-4">
              {alerts.map((alert) => (
                <div
                  key={alert.title}
                  className={`flex gap-4 p-4 rounded-lg border ${
                    alert.severity === 'high'
                      ? 'bg-red-50 border-red-200'
                      : 'bg-yellow-50 border-yellow-200'
                  }`}
                >
                  <AlertCircle
                    className={`w-6 h-6 flex-shrink-0 mt-0.5 ${
                      alert.severity === 'high' ? 'text-red-600' : 'text-yellow-600'
                    }`}
                  />
                  <div>
                    <p className={`font-semibold ${alert.severity === 'high' ? 'text-red-900' : 'text-yellow-900'}`}>
                      {alert.title}
                    </p>
                    <p className={`text-sm ${alert.severity === 'high' ? 'text-red-700' : 'text-yellow-700'}`}>
                      {alert.message}
                    </p>
                  </div>
                </div>
              ))}
            </div>
          </div>

          <div className="bg-white rounded-xl shadow p-6">
            <h2 className="text-xl font-bold text-gray-900 mb-6">Recommendations</h2>
            <div className="space-y-4">
              {recommendations.map((item) => (
                <div key={item.title} className="flex gap-4 p-4 bg-emerald-50 rounded-lg border border-emerald-200">
                  <CheckCircle2 className="w-6 h-6 text-emerald-600 flex-shrink-0 mt-0.5" />
                  <div>
                    <p className="font-semibold text-emerald-900">{item.title}</p>
                    <p className="text-emerald-700 text-sm">{item.message}</p>
                  </div>
                </div>
              ))}
            </div>
          </div>
        </div>

        <section className="mt-8 bg-white p-6 shadow">
          <div className="mb-5 flex items-center justify-between gap-4">
            <div>
              <h2 className="text-xl font-bold text-gray-900">Student Attendance</h2>
              <p className="mt-1 text-sm text-gray-600">Latest student check-ins</p>
            </div>
            <span className="text-sm font-semibold text-emerald-700">{attendanceRecords.length} records</span>
          </div>
          {attendanceRecords.length === 0 ? (
            <p className="py-8 text-center text-gray-500">No student attendance has been marked yet.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full min-w-[620px] text-left text-sm">
                <thead className="border-b border-gray-200 text-xs uppercase tracking-wide text-gray-500">
                  <tr>
                    <th className="px-3 py-3 font-semibold">Student</th>
                    <th className="px-3 py-3 font-semibold">Email</th>
                    <th className="px-3 py-3 font-semibold">Date</th>
                    <th className="px-3 py-3 font-semibold">Status</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-gray-100">
                  {attendanceRecords.map((record) => (
                    <tr key={`${record.id}-${record.attendance_date}`}>
                      <td className="px-3 py-4 font-medium text-gray-900">{record.username}</td>
                      <td className="px-3 py-4 text-gray-600">{record.email}</td>
                      <td className="px-3 py-4 text-gray-600">{new Date(`${record.attendance_date}T00:00:00`).toLocaleDateString()}</td>
                      <td className="px-3 py-4"><span className="bg-emerald-50 px-2 py-1 text-xs font-semibold text-emerald-700">Present</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <div className="mt-8 flex justify-center">
          <Link href="/predict" className="px-8 py-4 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 transition-colors font-semibold text-lg">
            Make New Prediction
          </Link>
        </div>
      </div>
    </main>
  )
}
