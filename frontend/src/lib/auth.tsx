import * as React from 'react'
import { login as loginRequest, tokenStore } from './api'

interface AuthContextValue {
  isAuthenticated: boolean
  login: (username: string, password: string, rememberMe: boolean) => Promise<void>
  logout: () => void
}

const AuthContext = React.createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [token, setToken] = React.useState<string | null>(() => tokenStore.get())

  const login = React.useCallback(async (username: string, password: string, rememberMe: boolean) => {
    const accessToken = await loginRequest(username, password, rememberMe)
    tokenStore.set(accessToken, rememberMe)
    setToken(accessToken)
  }, [])

  const logout = React.useCallback(() => {
    tokenStore.clear()
    setToken(null)
  }, [])

  const value = React.useMemo(() => ({ isAuthenticated: !!token, login, logout }), [token, login, logout])

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth() {
  const ctx = React.useContext(AuthContext)
  if (!ctx) throw new Error('useAuth must be used within AuthProvider')
  return ctx
}
