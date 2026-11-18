import { appConfig } from '../config/appConfig'
import { storageGet, storageRemove, storageSet } from './storage'

export const API_BASE = appConfig.apiBase

export const ACCESS_TOKEN_KEY = 'flowshorts_access_token'
export const REFRESH_TOKEN_KEY = 'flowshorts_refresh_token'

export type AuthUser = {
  user_id: string
  email: string
  display_name: string
  role: 'admin' | 'user' | 'viewer'
  email_verified: boolean
}

export type TokenPair = {
  access_token: string
  refresh_token: string
  token_type: string
  expires_in: number
}

export class ApiError extends Error {
  status: number
  body: string
  retryAfterSeconds: number | null

  constructor(status: number, body: string, retryAfterSeconds: number | null = null) {
    super(buildApiErrorMessage(status, body, retryAfterSeconds))
    this.name = 'ApiError'
    this.status = status
    this.body = body
    this.retryAfterSeconds = retryAfterSeconds
  }
}

function parseRetryAfter(header: string | null): number | null {
  if (!header) {
    return null
  }
  const parsed = Number.parseInt(header.trim(), 10)
  if (!Number.isFinite(parsed) || parsed <= 0) {
    return null
  }
  return parsed
}

export const GATEWAY_ERROR_MESSAGE = '서버에 잠시 연결할 수 없어요. 잠시 후 다시 시도해 주세요.'
export const SERVER_ERROR_MESSAGE = '서버에서 오류가 났어요. 잠시 후 다시 시도해 주세요.'

/** nginx/Caddy error pages (502 while the api restarts, 504 on a timeout) are HTML, not JSON. */
function isHtmlPage(body: string): boolean {
  return /^\s*</.test(body)
}

function buildApiErrorMessage(
  status: number,
  body: string,
  retryAfterSeconds: number | null,
): string {
  const fromProxy = isHtmlPage(body)
  const detail = fromProxy ? '' : formatApiErrorBody(body)
  if (status === 502 || status === 504 || (status === 503 && fromProxy)) {
    return koreanOr(detail, GATEWAY_ERROR_MESSAGE)
  }
  if (status === 429) {
    const base =
      detail ||
      '요청이 많습니다. 잠시 후 다시 시도해주세요.'
    // The server already said how long ("15분 후 다시 시도해주세요"): show it as is.
    if (/\d+\s*(분|초|시간) 후/.test(base)) {
      return base
    }
    if (retryAfterSeconds !== null) {
      if (base.includes('잠시 후')) {
        return base.replace('잠시 후', `${retryAfterSeconds}초 후`)
      }
      return `${base} ${retryAfterSeconds}초 후 다시 시도해주세요.`
    }
    return base
  }
  if (status === 401) {
    return koreanOr(detail, '로그인이 필요합니다. 다시 로그인해 주세요.')
  }
  if (status === 403) {
    return koreanOr(detail, '권한이 없습니다.')
  }
  if (status === 413) {
    return koreanOr(detail, '파일 용량이 너무 큽니다.')
  }
  if (status === 507) {
    return koreanOr(detail, '서버 디스크가 부족합니다. 파일을 줄이거나 나중에 다시 시도해 주세요.')
  }
  if (status === 503) {
    return koreanOr(detail, 'AI 서비스를 지금은 사용할 수 없습니다. 잠시 후 다시 시도해 주세요.')
  }
  if (status >= 400 && status < 500) {
    return koreanOr(detail, '요청을 처리할 수 없습니다.')
  }
  return detail.trim() ? `API ${status}: ${detail}` : SERVER_ERROR_MESSAGE
}

function koreanOr(detail: string, fallback: string): string {
  const trimmed = detail.trim()
  if (trimmed) {
    return trimmed
  }
  return fallback
}

function formatApiErrorBody(body: string): string {
  try {
    const parsed: unknown = JSON.parse(body)
    if (typeof parsed === 'object' && parsed !== null && 'detail' in parsed) {
      const detail = (parsed as { detail: unknown }).detail
      if (typeof detail === 'string' && detail.trim()) {
        return detail
      }
      if (Array.isArray(detail)) {
        const parts = detail.map((item) => {
          if (typeof item === 'string') {
            return item
          }
          if (typeof item === 'object' && item !== null) {
            const loc = Array.isArray((item as { loc?: unknown }).loc)
              ? (item as { loc: unknown[] }).loc.filter((part) => part !== 'body').join('.')
              : ''
            const msg = typeof (item as { msg?: unknown }).msg === 'string' ? (item as { msg: string }).msg : ''
            return loc && msg ? `${loc}: ${msg}` : msg || loc
          }
          return ''
        }).filter(Boolean)
        if (parts.length > 0) {
          return parts.join('; ')
        }
      }
    }
  } catch {
    // Keep the raw body when it is not JSON.
  }
  return body
}

function createApiError(status: number, body: string, response: Response): ApiError {
  const retryAfterSeconds = status === 429 ? parseRetryAfter(response.headers.get('Retry-After')) : null
  return new ApiError(status, body, retryAfterSeconds)
}

export function authHeaders(): HeadersInit {
  return { ...AUTH_COOKIE_HEADER }
}

let refreshInFlight: Promise<boolean> | null = null

const AUTH_COOKIE_HEADER = { 'X-Auth-Cookie': '1' }

/** Old builds kept JWTs in localStorage. Drop them on every load; HttpOnly cookies remain. */
let migrateCookieSession = false

export function clearLegacyAuthStorage(): void {
  const hadLegacy = Boolean(storageGet(ACCESS_TOKEN_KEY) || storageGet(REFRESH_TOKEN_KEY))
  storageRemove(ACCESS_TOKEN_KEY)
  storageRemove(REFRESH_TOKEN_KEY)
  if (hadLegacy) {
    migrateCookieSession = true
  }
}

clearLegacyAuthStorage()

export function hasBrowserSession(): boolean {
  if (migrateCookieSession) {
    return true
  }
  if (typeof document === 'undefined') {
    return false
  }
  return document.cookie.split(';').some((part) => part.trim().startsWith('signed_in='))
}

/** Tabs share one refresh cookie, and each refresh token works once: tabs take turns. */
export const REFRESH_LOCK_NAME = 'flow-auth-refresh'
/** When some tab last refreshed (ms). A tab that waited for the lock reads it to skip a second refresh. */
export const LAST_REFRESH_KEY = 'flow_auth_refreshed_at'

/** A hung refresh must not hold the cross-tab lock (and every other tab) forever. */
const REFRESH_TIMEOUT_MS = 15_000

function refreshTimeoutSignal(): AbortSignal | undefined {
  return typeof AbortSignal !== 'undefined' && typeof AbortSignal.timeout === 'function'
    ? AbortSignal.timeout(REFRESH_TIMEOUT_MS)
    : undefined
}

async function postRefresh(): Promise<boolean> {
  try {
    const response = await fetch(`${API_BASE}/auth/refresh`, {
      method: 'POST',
      credentials: 'include',
      headers: { 'Content-Type': 'application/json', ...AUTH_COOKIE_HEADER },
      body: JSON.stringify({}),
      signal: refreshTimeoutSignal(),
    })
    if (response.status === 401 || response.status === 403) {
      clearAuthTokens()
      return false
    }
    if (!response.ok) {
      return false
    }
    clearLegacyAuthStorage()
    storageSet(LAST_REFRESH_KEY, String(Date.now()))
    return true
  } catch {
    return false
  }
}

function refreshAcrossTabs(): Promise<boolean> {
  if (typeof navigator === 'undefined' || !('locks' in navigator) || !navigator.locks) {
    return postRefresh()
  }
  const askedAt = Date.now()
  return navigator.locks.request(REFRESH_LOCK_NAME, async () => {
    // Another tab refreshed while this one waited: the shared cookies are already the new ones.
    if (Number(storageGet(LAST_REFRESH_KEY) ?? 0) >= askedAt) {
      return true
    }
    return postRefresh()
  })
}

export async function refreshAccessToken(): Promise<boolean> {
  // One in-flight refresh for every 401. Callers that arrive while it runs share the same promise.
  if (!refreshInFlight) {
    refreshInFlight = refreshAcrossTabs().finally(() => {
      refreshInFlight = null
    })
  }

  return refreshInFlight
}

export async function apiFetch(path: string, init?: RequestInit, retried = false): Promise<Response> {
  const response = await fetch(`${API_BASE}${path}`, {
    ...init,
    credentials: 'include',
    headers: {
      'Content-Type': 'application/json',
      ...AUTH_COOKIE_HEADER,
      ...authHeaders(),
      ...(init?.headers ?? {}),
    },
  })

  if (
    response.status === 401 &&
    !retried &&
    !path.startsWith('/auth/login') &&
    !path.startsWith('/auth/register') &&
    !path.startsWith('/auth/password-reset') &&
    !path.startsWith('/auth/refresh')
  ) {
    const refreshed = await refreshAccessToken()
    if (refreshed) {
      return apiFetch(path, init, true)
    }
  }

  return response
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await apiFetch(path, init)
  if (!response.ok) {
    const text = await response.text()
    if (response.status >= 500) {
      const { reportApiError } = await import('./errorReporter')
      reportApiError(`${path} ${response.status}: ${text.slice(0, 240)}`)
    }
    throw createApiError(response.status, text, response)
  }
  return response.json() as Promise<T>
}

/** Like request(), for endpoints that return a file. */
export async function requestBlob(path: string, init?: RequestInit): Promise<Blob> {
  const response = await apiFetch(path, init)
  if (!response.ok) {
    throw createApiError(response.status, await response.text(), response)
  }
  return response.blob()
}

/** Multipart upload — do not set Content-Type; the browser adds the boundary. */
export async function uploadForm<T>(path: string, form: FormData, retried = false): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, {
    method: 'POST',
    credentials: 'include',
    headers: { ...AUTH_COOKIE_HEADER, ...authHeaders() },
    body: form,
  })
  if (
    response.status === 401 &&
    !retried &&
    !path.startsWith('/auth/login') &&
    !path.startsWith('/auth/register') &&
    !path.startsWith('/auth/password-reset') &&
    !path.startsWith('/auth/refresh')
  ) {
    const refreshed = await refreshAccessToken()
    if (refreshed) {
      return uploadForm(path, form, true)
    }
  }
  if (!response.ok) {
    throw createApiError(response.status, await response.text(), response)
  }
  return response.json() as Promise<T>
}

export async function login(email: string, password: string) {
  const data = await request<{ user: AuthUser; tokens: TokenPair }>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ email, password }),
  })
  clearLegacyAuthStorage()
  return data
}

export async function register(email: string, password: string, displayName = '') {
  const data = await request<{
    user: AuthUser
    tokens: TokenPair
    verification_message?: string | null
    dev_verify_url?: string | null
  }>('/auth/register', {
    method: 'POST',
    body: JSON.stringify({ email, password, display_name: displayName }),
  })
  clearLegacyAuthStorage()
  return data
}

export async function requestVerifyEmail() {
  return request<{
    ok: boolean
    message: string
    already_verified?: boolean
    dev_verify_url?: string | null
  }>('/auth/verify-email/request', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export async function confirmVerifyEmail(token: string) {
  // Body, not query string: a query string ends up in proxy access logs.
  return request<{ ok: boolean; message: string }>('/auth/verify-email/confirm', {
    method: 'POST',
    body: JSON.stringify({ token }),
  })
}

export async function requestPasswordReset(email: string) {
  return request<{ ok: boolean; message: string; dev_reset_url?: string | null }>(
    '/auth/password-reset/request',
    {
      method: 'POST',
      body: JSON.stringify({ email }),
    },
  )
}

export async function confirmPasswordReset(token: string, newPassword: string) {
  return request<{ ok: boolean; message: string }>('/auth/password-reset/confirm', {
    method: 'POST',
    body: JSON.stringify({ token, new_password: newPassword }),
  })
}

/** Signed-in password change. Other devices are signed out; this one keeps working with new tokens. */
export async function changePassword(currentPassword: string, newPassword: string) {
  const data = await request<{ user: AuthUser; tokens: TokenPair }>('/auth/password/change', {
    method: 'POST',
    body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
  })
  clearLegacyAuthStorage()
  return data
}

/** Permanently delete the account with all projects and renders. Clears local tokens on success. */
export async function deleteAccount(password: string) {
  const data = await request<{ ok: boolean; message: string; projects_removed: number }>('/auth/account/delete', {
    method: 'POST',
    body: JSON.stringify({ password }),
  })
  clearAuthTokens()
  return data
}

export function hasRefreshToken(): boolean {
  return false
}

export async function fetchMe() {
  return request<{ user: AuthUser }>('/auth/me')
}

/** This device only: the server ends this sign-in (its refresh and access tokens). Other devices stay signed in. */
export async function logout() {
  return request('/auth/logout', {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export async function logoutAll() {
  return request('/auth/logout-all', { method: 'POST', body: JSON.stringify({}) })
}

export function clearAuthTokens() {
  clearLegacyAuthStorage()
}

export function hasAccessToken(): boolean {
  return false
}
