import axios, {
  AxiosHeaders,
  type AxiosInstance,
  type AxiosRequestConfig,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'

import {
  clearTokens,
  getAccessToken as readAccessToken,
  getRefreshToken as readRefreshToken,
  setTokens,
} from './tokens'

/** 默认 API 基础路径，可通过 VITE_API_BASE_URL 覆盖。 */
export const DEFAULT_BASE_URL = import.meta.env.VITE_API_BASE_URL || '/api/v1'

export type UserRole = 'viewer' | 'operator' | 'admin'

/** 后端返回的令牌对，字段名固定为 snake_case。 */
export interface TokenPair {
  access_token: string
  refresh_token: string
  token_type: string
}

/** GET /auth/me 返回的当前用户信息。 */
export interface AuthUser {
  id: number
  email: string
  display_name: string | null
  role: UserRole
  tenant_id: string
}

/** 后端统一响应信封。 */
export interface ApiEnvelope<T> {
  code: number
  message: string
  data: T
  trace_id?: string | null
}

/**
 * 前端统一的业务异常。
 * status 为 HTTP 状态码，网络错误时为 0；code 为后端信封中的业务码。
 */
export class ApiError extends Error {
  readonly status: number
  readonly code: number
  readonly traceId: string | null

  constructor(status: number, code: number, message: string, traceId: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.traceId = traceId
  }
}

export interface HttpClientDeps {
  getAccessToken: () => string | null
  getRefreshToken: () => string | null
  refresh: (refreshToken: string) => Promise<TokenPair>
  onRefreshed: (pair: TokenPair) => void
  onUnauthorized: () => void
  baseURL?: string
  timeout?: number
}

/** 页面层使用的请求配置；silent 仅作为调用方标记，HTTP 层不弹任何 UI 提示。 */
export interface ApiRequestConfig extends AxiosRequestConfig {
  silent?: boolean
}

/** 返回已解包 data 的类型化快捷方法。 */
export interface ApiClient {
  get<T>(url: string, config?: ApiRequestConfig): Promise<T>
  post<T>(url: string, data?: unknown, config?: ApiRequestConfig): Promise<T>
  patch<T>(url: string, data?: unknown, config?: ApiRequestConfig): Promise<T>
  delete<T>(url: string, config?: ApiRequestConfig): Promise<T>
}

export interface HttpClient {
  http: AxiosInstance
  api: ApiClient
}

interface RetriableConfig extends InternalAxiosRequestConfig {
  _retry?: boolean
  silent?: boolean
}

function isEnvelope(value: unknown): value is ApiEnvelope<unknown> {
  if (typeof value !== 'object' || value === null) {
    return false
  }
  const candidate = value as { code?: unknown; message?: unknown }
  return typeof candidate.code === 'number' && typeof candidate.message === 'string'
}

/** 把 axios 异常转换为携带后端 message / trace_id 的 ApiError。 */
function toApiError(error: unknown): ApiError {
  if (error instanceof ApiError) {
    return error
  }

  if (axios.isAxiosError(error)) {
    const response = error.response
    if (!response) {
      if (error.code === 'ECONNABORTED' || error.code === 'ETIMEDOUT') {
        return new ApiError(0, 0, '请求超时，请稍后重试', null)
      }
      return new ApiError(0, 0, '网络连接失败，请检查网络或稍后重试', null)
    }

    const status = response.status
    const payload = response.data
    if (isEnvelope(payload)) {
      return new ApiError(
        status,
        payload.code || status,
        payload.message || `请求失败（HTTP ${status}）`,
        payload.trace_id ?? null,
      )
    }

    let message = `请求失败（HTTP ${status}）`
    if (payload && typeof payload === 'object' && 'detail' in payload) {
      const detail = (payload as { detail?: unknown }).detail
      if (typeof detail === 'string' && detail.trim()) {
        message = detail
      }
    }
    return new ApiError(status, status, message, null)
  }

  if (error instanceof Error && error.message) {
    return new ApiError(0, 0, error.message, null)
  }
  return new ApiError(0, 0, '发生未知错误，请稍后重试', null)
}

/** 登录与刷新接口自身的 401 不参与自动刷新，避免死循环。 */
function isAuthRefreshExcluded(url: string | undefined): boolean {
  if (!url) {
    return false
  }
  return /\/auth\/(?:login|refresh)(?:[/?#]|$)/.test(url)
}

/** 读取请求头中实际携带的 Bearer 令牌，用于判断请求是否已使用旧令牌。 */
function bearerTokenOf(config: InternalAxiosRequestConfig): string | null {
  const raw = AxiosHeaders.from(config.headers).get('Authorization')
  if (typeof raw !== 'string') {
    return null
  }
  const match = /^Bearer\s+(.+)$/i.exec(raw)
  return match?.[1] ?? null
}

function withBearer(config: RetriableConfig, accessToken: string): RetriableConfig {
  const headers = AxiosHeaders.from(config.headers)
  headers.set('Authorization', `Bearer ${accessToken}`)
  config.headers = headers
  return config
}

/**
 * 解开后端统一信封，直接把 data 交给页面层。
 *
 * 关键节点：
 * 1. `code === 0` 才视为成功，其余情况抛 ApiError（保留 trace_id 供排障）；
 * 2. 非信封响应（例如网关直出）按原样返回，保证兼容性。
 *
 * @param response axios 原始响应。
 * @returns 信封中的 data，或非信封响应的原始 payload。
 * @throws {ApiError} 信封 code 非 0 时抛出，携带业务码与后端 message。
 */
function unwrapResponse<T>(response: AxiosResponse<unknown>): T {
  const payload = response.data
  if (isEnvelope(payload)) {
    if (payload.code === 0) {
      return payload.data as T
    }
    const status = response.status >= 400 ? response.status : payload.code
    throw new ApiError(status, payload.code, payload.message || '请求失败', payload.trace_id ?? null)
  }
  // 非信封响应（例如网关直出）按原样返回，保证兼容性
  return payload as T
}

/**
 * 创建可注入依赖的 HTTP 客户端，便于单测替换刷新逻辑与适配器。
 *
 * 关键节点：
 * 1. 请求拦截器统一补 Bearer 令牌；
 * 2. 响应拦截器解包统一信封，并把 401 交给单飞刷新逻辑；
 * 3. 多个并发 401 共享同一个 refreshPromise，避免刷新风暴。
 *
 * @param deps 令牌读取、刷新、失效回调与基础配置。
 * @returns 原始 axios 实例与解包后的快捷方法。
 */
export function createHttpClient(deps: HttpClientDeps): HttpClient {
  const http = axios.create({
    baseURL: deps.baseURL ?? DEFAULT_BASE_URL,
    timeout: deps.timeout ?? 20000,
  })

  // 多个并发 401 共享同一次刷新，避免刷新风暴
  let refreshPromise: Promise<string> | null = null

  /**
   * 用 refresh_token 换新令牌；并发的 401 共用同一次刷新。
   *
   * 关键节点：
   * 1. refreshPromise 作为单飞锁，成功后写入新令牌并返回 access_token；
   * 2. 刷新失败立即清空本地登录态（onUnauthorized），页面据此跳登录；
   * 3. finally 释放锁，保证下一次失效时可以重新刷新。
   *
   * @param refreshToken 本地保存的刷新令牌。
   * @returns 新 access_token，供原请求重放使用。
   * @throws {ApiError} 刷新失败时抛出 401，并已触发本地登出。
   */
  function ensureRefreshed(refreshToken: string): Promise<string> {
    if (!refreshPromise) {
      refreshPromise = deps
        .refresh(refreshToken)
        .then((pair) => {
          if (!pair?.access_token) {
            throw new ApiError(401, 401, '刷新登录状态失败，请重新登录', null)
          }
          deps.onRefreshed(pair)
          return pair.access_token
        })
        .catch((refreshError: unknown) => {
          deps.onUnauthorized()
          const apiError = toApiError(refreshError)
          throw new ApiError(
            401,
            401,
            apiError.message || '登录已过期，请重新登录',
            apiError.traceId,
          )
        })
        .finally(() => {
          refreshPromise = null
        })
    }
    return refreshPromise
  }

  http.interceptors.request.use((config) => {
    const token = deps.getAccessToken()
    if (token) {
      config.headers.set('Authorization', `Bearer ${token}`)
    }
    return config
  })

  http.interceptors.response.use(
    (response) => unwrapResponse<unknown>(response) as unknown as AxiosResponse,
    async (error: unknown) => {
      const apiError = toApiError(error)

      if (!axios.isAxiosError(error)) {
        return Promise.reject(apiError)
      }

      // 关键节点 1：只有 401 且尚未重放过才进入刷新分支，
      // 避免刷新失败后无限重试同一条请求。
      const status = error.response?.status
      const config = error.config as RetriableConfig | undefined
      if (status !== 401 || !config || config._retry) {
        return Promise.reject(apiError)
      }

      // 登录/刷新接口的 401 直接交给页面展示，例如“邮箱或密码错误”
      if (isAuthRefreshExcluded(config.url)) {
        return Promise.reject(apiError)
      }

      config._retry = true

      // 关键节点 2：若刷新已由别的并发请求完成，直接用新令牌重放，
      // 不再触发第二次刷新。
      const currentToken = deps.getAccessToken()
      if (currentToken && currentToken !== bearerTokenOf(config)) {
        return http.request(withBearer(config, currentToken)) as unknown as AxiosResponse
      }

      const refreshToken = deps.getRefreshToken()
      if (!refreshToken) {
        deps.onUnauthorized()
        return Promise.reject(
          new ApiError(401, 401, '登录状态已失效，请重新登录', apiError.traceId),
        )
      }

      const accessToken = await ensureRefreshed(refreshToken)

      // 关键节点 3：重放原请求；_retry 已置位，若再次 401 会直接失败，
      // 不会二次刷新。
      return http.request(withBearer(config, accessToken)) as unknown as AxiosResponse
    },
  )

  const api: ApiClient = {
    get<T>(url: string, config?: ApiRequestConfig): Promise<T> {
      return http.get(url, config) as unknown as Promise<T>
    },
    post<T>(url: string, data?: unknown, config?: ApiRequestConfig): Promise<T> {
      return http.post(url, data, config) as unknown as Promise<T>
    },
    patch<T>(url: string, data?: unknown, config?: ApiRequestConfig): Promise<T> {
      return http.patch(url, data, config) as unknown as Promise<T>
    },
    delete<T>(url: string, config?: ApiRequestConfig): Promise<T> {
      return http.delete(url, config) as unknown as Promise<T>
    },
  }

  return { http, api }
}

/**
 * 直接使用 axios 调刷新接口，避免默认客户端拦截器造成递归刷新。
 *
 * 关键节点：
 * 1. 不走带拦截器的默认实例，防止刷新请求自身 401 时再次递归刷新；
 * 2. 响应必须符合统一信封且 code === 0，否则视为刷新失败。
 *
 * @param refreshToken 本地保存的刷新令牌。
 * @returns 新的令牌对。
 * @throws {ApiError} 响应格式不正确或业务码非 0 时抛出。
 */
async function refreshWithBackend(refreshToken: string): Promise<TokenPair> {
  const response = await axios.post<unknown>(
    `${DEFAULT_BASE_URL}/auth/refresh`,
    { refresh_token: refreshToken },
    { timeout: 15000 },
  )
  const payload = response.data
  if (!isEnvelope(payload)) {
    throw new ApiError(500, 500, '刷新登录状态失败：响应格式不正确', null)
  }
  if (payload.code !== 0 || !payload.data) {
    throw new ApiError(
      401,
      payload.code || 401,
      payload.message || '刷新登录状态失败',
      payload.trace_id ?? null,
    )
  }
  return payload.data as TokenPair
}

const defaultClient = createHttpClient({
  getAccessToken: readAccessToken,
  getRefreshToken: readRefreshToken,
  refresh: refreshWithBackend,
  onRefreshed: (pair) => setTokens(pair),
  onUnauthorized: () => {
    clearTokens()
    if (typeof window !== 'undefined') {
      window.dispatchEvent(new CustomEvent('auth:unauthorized'))
    }
  },
  baseURL: DEFAULT_BASE_URL,
})

/** 默认 axios 实例（拦截器已配置）。 */
export const http = defaultClient.http

/** 页面层推荐使用的快捷方法，返回值即信封中的 data。 */
export const api = defaultClient.api
