'use client'

import React, { createContext, useContext, useState, useEffect } from 'react'

interface User {
  id: string
  name: string
  email: string
  role: 'admin' | 'student'
  predictions: Array<{
    date: string
    waste: number
    cost: number
  }>
}

interface AuthContextType {
  user: User | null
  isLoading: boolean
  login: (email: string, password: string, role: User['role']) => Promise<void>
  signup: (name: string, email: string, password: string, role: User['role']) => Promise<void>
  logout: () => void
  addPrediction: (waste: number, cost: number) => void
}

const AuthContext = createContext<AuthContextType | undefined>(undefined)
const BACKEND_URL = process.env.NEXT_PUBLIC_BACKEND_URL ?? 'http://localhost:8000'

const DEMO_USER = {
  id: '1',
  name: 'Demo User',
  email: 'demo@hostel.com',
  role: 'admin' as const,
  hostelName: 'Central Hostel',
  predictions: [
    { date: '2024-01-15', waste: 45, cost: 1125 },
    { date: '2024-01-14', waste: 38, cost: 950 },
    { date: '2024-01-13', waste: 52, cost: 1300 },
  ],
}

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<User | null>(null)
  const [isLoading, setIsLoading] = useState(true)

  useEffect(() => {
    const storedUser = localStorage.getItem('foodwaste_user')
    if (storedUser) {
      try {
        const parsedUser = JSON.parse(storedUser)
        setUser({ ...parsedUser, role: parsedUser.role ?? 'admin' })
      } catch (error) {
        console.error('Failed to parse stored user')
      }
    }
    setIsLoading(false)
  }, [])

  const login = async (email: string, password: string, role: User['role']) => {
    const res = await fetch(`${BACKEND_URL}/login`,{
      method:"POST",
      headers:{
        "Content-Type":"application/json"
      },
      body:JSON.stringify({
        email,
        password,
        role,
      })
    })
    const data = await res.json()

    if(data.status==="success"){
      const loggedUser:User={
      id: data.user.id,
      name: data.user.name,
      email: data.user.email,
      role: data.user.role,
      predictions:[]

    }
    setUser(loggedUser)
    localStorage.setItem("foodwaste_user",JSON.stringify(loggedUser))
  }

    else if(data.status==="wrong_password")
    {
      throw new Error("Incorrect password")
      
    }
    else if(data.status==="no_user")
    {
      throw new Error("User not found")
    }
    else if(data.status==="wrong_role")
    {
      throw new Error("This account is registered with a different role")
    }
    else if(data.status==="invalid_role")
    {
      throw new Error("Choose whether you are signing in as an admin or student")
    }
    else
    {
      throw new Error("login failed")

    }
  }

  const signup = async (name: string, email: string, password: string, role: User['role']) => {
    const response = await fetch(`${BACKEND_URL}/register`,{
      method:"POST",
      headers:{
        "Content-Type":"application/json",
      },
      body:JSON.stringify({
        username:name,
        email:email,
        password:password,
        role,
      }),
    })
    if(!response.ok){
      throw new Error("Server error")
    }

    const data = await response.json()

    if(data.status==="success"){
      const newUser : User={
        id: String(data.user.id),
        name: data.user.name,
        email: data.user.email,
        role: data.user.role,
        predictions:[],
      }
      setUser(newUser)
      localStorage.setItem('foodwaste_user', JSON.stringify(newUser))
    }
    else if(data.status==="email_exists"){
      throw new Error("Email already exists")
    }
    else if(data.status==="invalid_role"){
      throw new Error("Choose an account role")
    }
    else
    {
      throw new Error("Signup Failed")
    }
  }

  const logout = () => {
    setUser(null)
    localStorage.removeItem('foodwaste_user')
  }

  const addPrediction = (waste: number, cost: number) => {
    if (user) {
      const updatedUser = {
        ...user,
        predictions: [
          { date: new Date().toISOString().split('T')[0], waste, cost },
          ...user.predictions,
        ],
      }
      setUser(updatedUser)
      localStorage.setItem('foodwaste_user', JSON.stringify(updatedUser))
    }
  }

  return (
    <AuthContext.Provider value={{ user, isLoading, login, signup, logout, addPrediction }}>
      {children}
    </AuthContext.Provider>
  )
}

export function useAuth() {
  const context = useContext(AuthContext)
  if (!context) {
    throw new Error('useAuth must be used within AuthProvider')
  }
  return context
}
