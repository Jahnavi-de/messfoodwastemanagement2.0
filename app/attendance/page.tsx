'use client'

import { useEffect, useState } from 'react'
import { useRouter } from 'next/navigation'
import { CalendarCheck, CheckCircle2, GraduationCap, Loader2, LogOut } from 'lucide-react'
import { useAuth } from '@/lib/auth-context'

const ATTENDANCE_API_URL = process.env.NEXT_PUBLIC_ATTENDANCE_API_URL ?? 'http://localhost:8000/attendance'

type AttendanceRecord = {
  attendance_date: string
  status: 'present'
  marked_at: string
  username?: string
  email?: string
}

export default function AttendancePage() {
  const { user, isLoading, logout } = useAuth()
  const router = useRouter()
  const [records, setRecords] = useState<AttendanceRecord[]>([])
  const [loadingRecords, setLoadingRecords] = useState(true)
  const [marking, setMarking] = useState(false)
  const [error, setError] = useState('')

  const today = new Date().toISOString().slice(0, 10)
  const markedToday = records.some((record) => record.attendance_date === today)

  const loadAttendance = async (userId: string, role: 'admin' | 'student') => {
    try {
      const endpoint = role === 'admin'
        ? `${ATTENDANCE_API_URL}?action=all`
        : `${ATTENDANCE_API_URL}?action=mine&userId=${encodeURIComponent(userId)}`
      const response = await fetch(endpoint)
      const data = await response.json()
      if (!response.ok || !data.success) throw new Error(data.message || 'Could not load attendance')
      setRecords(data.records ?? [])
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not load attendance')
    } finally {
      setLoadingRecords(false)
    }
  }

  useEffect(() => {
    if (isLoading) return
    if (!user) {
      router.replace('/login')
      return
    }
    loadAttendance(user.id, user.role)
  }, [user, isLoading, router])

  const markAttendance = async () => {
    if (!user) return
    setMarking(true)
    setError('')
    try {
      const response = await fetch(`${ATTENDANCE_API_URL}?action=mark`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ userId: user.id }),
      })
      const data = await response.json()
      if (!response.ok || !data.success) throw new Error(data.message || 'Could not mark attendance')
      await loadAttendance(user.id, user.role)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not mark attendance')
    } finally {
      setMarking(false)
    }
  }

  if (isLoading || !user) return null

  const isAdmin = user.role === 'admin'

  return (
    <main className="min-h-screen bg-gradient-to-br from-emerald-50 via-white to-cyan-50 py-10">
      <div className="mx-auto max-w-3xl px-4 sm:px-6">
        <header className="mb-8 flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <div className="grid h-11 w-11 place-items-center bg-emerald-600 text-white"><GraduationCap className="h-6 w-6" /></div>
            <div>
              <h1 className="text-2xl font-bold text-gray-900">{isAdmin ? 'Attendance Management' : 'Student Attendance'}</h1>
              <p className="text-sm text-gray-600">Welcome, {user.name}</p>
            </div>
          </div>
          <button onClick={() => { logout(); router.push('/login') }} className="p-2 text-gray-600 hover:bg-white" title="Sign out">
            <LogOut className="h-5 w-5" />
          </button>
        </header>

        <section className="border border-emerald-200 bg-white p-6 shadow-sm">
          <div className="flex flex-col justify-between gap-6 sm:flex-row sm:items-center">
            <div>
              <p className="text-sm font-semibold uppercase tracking-wide text-emerald-700">Today</p>
              <h2 className="mt-1 text-2xl font-bold text-gray-900">{new Date().toLocaleDateString(undefined, { weekday: 'long', year: 'numeric', month: 'long', day: 'numeric' })}</h2>
              <p className="mt-2 text-gray-600">
                {isAdmin ? 'Review the latest attendance submitted by every student.' : 'Mark your presence for today\'s mess attendance.'}
              </p>
            </div>
            {!isAdmin && (markedToday ? (
              <div className="flex items-center gap-2 bg-emerald-50 px-4 py-3 font-semibold text-emerald-700">
                <CheckCircle2 className="h-5 w-5" /> Marked present
              </div>
            ) : (
              <button onClick={markAttendance} disabled={marking} className="inline-flex items-center justify-center gap-2 bg-emerald-600 px-5 py-3 font-semibold text-white transition hover:bg-emerald-700 disabled:cursor-not-allowed disabled:bg-gray-400">
                {marking ? <Loader2 className="h-5 w-5 animate-spin" /> : <CalendarCheck className="h-5 w-5" />}
                {marking ? 'Marking...' : 'Mark Present'}
              </button>
            ))}
          </div>
          {error && <p className="mt-5 border border-red-200 bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        </section>

        <section className="mt-8 bg-white p-6 shadow-sm">
          <h2 className="text-lg font-bold text-gray-900">{isAdmin ? 'Student Attendance List' : 'Recent Attendance'}</h2>
          {loadingRecords ? (
            <div className="flex justify-center py-10"><Loader2 className="h-6 w-6 animate-spin text-emerald-600" /></div>
          ) : records.length === 0 ? (
            <p className="py-8 text-center text-gray-500">No attendance has been marked yet.</p>
          ) : isAdmin ? (
            <div className="mt-4 overflow-x-auto">
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
                  {records.map((record) => (
                    <tr key={`${record.email}-${record.attendance_date}`}>
                      <td className="px-3 py-4 font-medium text-gray-900">{record.username}</td>
                      <td className="px-3 py-4 text-gray-600">{record.email}</td>
                      <td className="px-3 py-4 text-gray-600">{new Date(`${record.attendance_date}T00:00:00`).toLocaleDateString()}</td>
                      <td className="px-3 py-4"><span className="bg-emerald-50 px-2 py-1 text-xs font-semibold text-emerald-700">Present</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : (
            <div className="mt-4 divide-y divide-gray-100">
              {records.map((record) => (
                <div key={record.attendance_date} className="flex items-center justify-between py-3">
                  <span className="font-medium text-gray-800">{new Date(`${record.attendance_date}T00:00:00`).toLocaleDateString()}</span>
                  <span className="inline-flex items-center gap-1 text-sm font-semibold text-emerald-700"><CheckCircle2 className="h-4 w-4" /> Present</span>
                </div>
              ))}
            </div>
          )}
        </section>
      </div>
    </main>
  )
}
