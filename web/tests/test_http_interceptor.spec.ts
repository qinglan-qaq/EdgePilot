import { describe, expect, it, vi } from 'vitest'
import {
  AxiosError,
  AxiosHeaders,
  type AxiosAdapter,
  type AxiosResponse,
  type InternalAxiosRequestConfig,
} from 'axios'

import {
  ApiError,
  createHttpClient,
  type HttpClientDeps,
  type TokenPair,
} from '../src/api/http'

interface AdapterReply {
  status: number
  data: unknown
}

function makeAdapter(
  handler: (config: InternalAxiosRequestConfig) => AdapterReply | Promise<AdapterReply>,
): AxiosAdapter {
  return async (config) => {
    const reply = await handler(config)
    const response: AxiosResponse = {
      data: reply.data,
      status: reply.status,
      statusText: String(reply.status),
      headers: {},
      config,
    }
    if (reply.status >= 200 && reply.status < 300) {
      return response
    }
    throw new AxiosError(
      `Request failed with status code ${reply.status}`,
      AxiosError.ERR_BAD_RESPONSE,
      config,
      null,
      response,
    )
  }
}

function authorizationOf(config: InternalAxiosRequestConfig): string | undefined {
  const value = AxiosHeaders.from(config.headers).get('Authorization')
  return typeof value === 'string' ? value : undefined
}

function okEnvelope<T>(data: T, traceId = 'trace-ok') {
  return { code: 0, message: 'ok', data, trace_id: traceId }
}

function errorEnvelope(status: number, message: string, traceId: string) {
  return { code: status, message, data: null, trace_id: traceId }
}

const newPair: TokenPair = {
  access_token: 'new-access-token',
  refresh_token: 'new-refresh-token',
  token_type: 'bearer',
}

function setup(options: {
  accessToken?: string | null
  refreshToken?: string | null
  refresh?: (refreshToken: string) => Promise<TokenPair>
} = {}) {
  const state = {
    accessToken: options.accessToken ?? null,
    refreshToken: options.refreshToken ?? null,
  }

  const defaultRefresh = async (): Promise<TokenPair> => newPair
  const refresh = vi.fn(options.refresh ?? defaultRefresh)

  const onRefreshed = vi.fn((pair: TokenPair) => {
    state.accessToken = pair.access_token
    state.refreshToken = pair.refresh_token
  })

  const onUnauthorized = vi.fn(() => {
    state.accessToken = null
    state.refreshToken = null
  })

  const deps: HttpClientDeps = {
    getAccessToken: () => state.accessToken,
    getRefreshToken: () => state.refreshToken,
    refresh,
    onRefreshed,
    onUnauthorized,
    baseURL: '/api/v1',
    timeout: 5000,
  }

  return {
    ...createHttpClient(deps),
    state,
    refresh,
    onRefreshed,
    onUnauthorized,
  }
}

describe('http 拦截器', () => {
  it('请求自动携带 Bearer 访问令牌', async () => {
    const { api } = setup({ accessToken: 'access-a' })
    let seenAuthorization: string | undefined

    const adapter = makeAdapter((config) => {
      seenAuthorization = authorizationOf(config)
      return { status: 200, data: okEnvelope({ ok: true }) }
    })

    await api.get('/auth/me', { adapter })

    expect(seenAuthorization).toBe('Bearer access-a')
  })

  it('自动解包统一响应信封的 data', async () => {
    const { api } = setup({ accessToken: 'access-a' })
    const payload = { id: 7, email: 'ops@example.com', role: 'admin' }

    const adapter = makeAdapter(() => ({
      status: 200,
      data: okEnvelope(payload),
    }))

    await expect(api.get('/auth/me', { adapter })).resolves.toEqual(payload)
  })

  it('两个并发 401 只刷新一次，并使用新令牌重放成功', async () => {
    let releaseRefresh: (() => void) | undefined
    const refreshDeferred = new Promise<TokenPair>((resolve) => {
      releaseRefresh = () => resolve(newPair)
    })

    const { api, refresh, onRefreshed, onUnauthorized } = setup({
      accessToken: 'old-access-token',
      refreshToken: 'old-refresh-token',
      refresh: () => refreshDeferred,
    })

    let unauthorizedResponses = 0
    const seen: string[] = []

    const adapter = makeAdapter((config) => {
      const authorization = authorizationOf(config)
      seen.push(`${config.url} ${authorization ?? 'none'}`)

      if (authorization === `Bearer ${newPair.access_token}`) {
        return { status: 200, data: okEnvelope({ url: config.url }) }
      }

      unauthorizedResponses += 1
      // 确保两个请求都已收到 401 后，再放行共享的刷新 Promise
      if (unauthorizedResponses === 2) {
        releaseRefresh?.()
      }
      return {
        status: 401,
        data: errorEnvelope(401, '访问令牌已过期', 'trace-401'),
      }
    })

    const [first, second] = await Promise.all([
      api.get<{ url: string }>('/secure/a', { adapter }),
      api.get<{ url: string }>('/secure/b', { adapter }),
    ])

    expect(first).toEqual({ url: '/secure/a' })
    expect(second).toEqual({ url: '/secure/b' })
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(onRefreshed).toHaveBeenCalledTimes(1)
    expect(onUnauthorized).not.toHaveBeenCalled()
    expect(seen.filter((item) => item.includes(`Bearer ${newPair.access_token}`))).toHaveLength(2)
  })

  it('刷新失败时只通知一次，返回 ApiError 401 且不再重试', async () => {
    const refreshError = new ApiError(401, 401, '刷新令牌无效', 'trace-refresh')
    const { api, refresh, onRefreshed, onUnauthorized } = setup({
      accessToken: 'old-access-token',
      refreshToken: 'old-refresh-token',
      refresh: async () => {
        throw refreshError
      },
    })

    let adapterCalls = 0
    const adapter = makeAdapter(() => {
      adapterCalls += 1
      return { status: 401, data: errorEnvelope(401, '访问令牌已过期', 'trace-401') }
    })

    await expect(api.get('/secure/resource', { adapter })).rejects.toMatchObject({
      name: 'ApiError',
      status: 401,
      code: 401,
      message: '刷新令牌无效',
      traceId: 'trace-refresh',
    })

    expect(refresh).toHaveBeenCalledTimes(1)
    expect(onRefreshed).not.toHaveBeenCalled()
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
    expect(adapterCalls).toBe(1)
  })

  it('登录接口 401 不触发刷新，保留后端提示', async () => {
    const { api, refresh, onUnauthorized } = setup({
      accessToken: null,
      refreshToken: 'leftover-refresh-token',
    })

    const adapter = makeAdapter(() => ({
      status: 401,
      data: errorEnvelope(401, '邮箱或密码错误', 'trace-login'),
    }))

    await expect(
      api.post('/auth/login', { email: 'ops@example.com', password: 'wrong' }, { adapter }),
    ).rejects.toMatchObject({
      status: 401,
      code: 401,
      message: '邮箱或密码错误',
      traceId: 'trace-login',
    })

    expect(refresh).not.toHaveBeenCalled()
    expect(onUnauthorized).not.toHaveBeenCalled()
  })

  it('无刷新令牌时通知一次并返回 401，不重放请求', async () => {
    const { api, refresh, onUnauthorized } = setup({
      accessToken: 'expired-access-token',
      refreshToken: null,
    })

    let adapterCalls = 0
    const adapter = makeAdapter(() => {
      adapterCalls += 1
      return { status: 401, data: errorEnvelope(401, '访问令牌已过期', 'trace-401') }
    })

    await expect(api.get('/agents', { adapter })).rejects.toMatchObject({
      status: 401,
      code: 401,
    })

    expect(refresh).not.toHaveBeenCalled()
    expect(onUnauthorized).toHaveBeenCalledTimes(1)
    expect(adapterCalls).toBe(1)
  })

  it('403/409 后端业务消息与 trace_id 保留为 ApiError', async () => {
    const { api, refresh, onUnauthorized } = setup({ accessToken: 'access-a' })

    const forbiddenAdapter = makeAdapter(() => ({
      status: 403,
      data: errorEnvelope(403, '仅管理员可访问', 'trace-403'),
    }))
    const conflictAdapter = makeAdapter(() => ({
      status: 409,
      data: errorEnvelope(409, '名称已存在', 'trace-409'),
    }))

    await expect(
      api.get('/settings/users', { adapter: forbiddenAdapter }),
    ).rejects.toMatchObject({
      status: 403,
      code: 403,
      message: '仅管理员可访问',
      traceId: 'trace-403',
    })

    await expect(
      api.post('/agents', { name: '重复名称' }, { adapter: conflictAdapter }),
    ).rejects.toMatchObject({
      status: 409,
      code: 409,
      message: '名称已存在',
      traceId: 'trace-409',
    })

    expect(refresh).not.toHaveBeenCalled()
    expect(onUnauthorized).not.toHaveBeenCalled()
  })
})
