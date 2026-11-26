import { computed, ref } from 'vue'
import { defineStore } from 'pinia'

import { api, ApiError, type AuthUser, type TokenPair, type UserRole } from '@/api/http'
import { clearTokens, getAccessToken, setTokens } from '@/api/tokens'

/**
 * 认证状态仓库。
 * 只在 localStorage 中持久化令牌，user 对象仅保存在内存中，避免泄露用户 PII。
 */
export const useAuthStore = defineStore('auth', () => {
  const user = ref<AuthUser | null>(null)
  const initialized = ref(false)
  const loading = ref(false)
  const accessToken = ref<string | null>(getAccessToken())

  let bootstrapPromise: Promise<void> | null = null

  const isAuthenticated = computed(() => Boolean(accessToken.value))
  const role = computed<UserRole | null>(() => user.value?.role ?? null)

  /**
   * 拉取当前账号信息并同步最新令牌。
   *
   * 关键节点：401 可能已被 HTTP 拦截器静默刷新，因此请求结束后
   * 重新从 localStorage 读一次 access_token。
   *
   * @returns 当前用户信息。
   * @throws {ApiError} 未登录或后端拒绝时抛出，由调用方决定跳转。
   */
  async function fetchMe(): Promise<AuthUser | null> {
    loading.value = true
    try {
      const me = await api.get<AuthUser>('/auth/me')
      user.value = me
      // 拦截器可能已刷新令牌，这里同步最新值
      accessToken.value = getAccessToken()
      return me
    } finally {
      loading.value = false
    }
  }

  /**
   * 登录：换令牌 → 拉 /auth/me → 标记初始化完成。
   *
   * 关键节点：
   * 1. 令牌先落 localStorage，fetchMe 才能带上 Authorization；
   * 2. 令牌已拿到但 /auth/me 失败时回滚本地登录态，不保留半截会话。
   *
   * @param email 登录邮箱。
   * @param password 登录密码。
   * @returns 登录成功的用户信息。
   * @throws {ApiError} 凭证错误或账号信息拉取失败时抛出。
   */
  async function login(email: string, password: string): Promise<AuthUser> {
    loading.value = true
    let receivedPair = false
    try {
      const pair = await api.post<TokenPair>('/auth/login', { email, password })
      setTokens(pair)
      receivedPair = true
      accessToken.value = pair.access_token

      const me = await fetchMe()
      if (!me) {
        throw new ApiError(500, 500, '登录成功但未能获取账号信息', null)
      }
      initialized.value = true
      return me
    } catch (error) {
      if (receivedPair) {
        // 登录接口成功但 /auth/me 失败时不保留半截登录态
        clearTokens()
        accessToken.value = null
        user.value = null
      }
      throw error
    } finally {
      loading.value = false
    }
  }

  /**
   * 应用启动时恢复登录态；多次调用共享同一个进行中的 Promise。
   *
   * 关键节点：
   * 1. 有令牌才请求 /auth/me，无令牌直接清空内存用户；
   * 2. 任何失败都视为未登录并清理令牌，不阻塞路由渲染；
   * 3. initialized 只在流程结束时置位，路由守卫据此等待。
   *
   * @returns 恢复流程完成后 resolve。
   */
  async function bootstrap(): Promise<void> {
    if (initialized.value) {
      return
    }
    if (bootstrapPromise) {
      return bootstrapPromise
    }

    bootstrapPromise = (async () => {
      accessToken.value = getAccessToken()
      try {
        if (accessToken.value) {
          await fetchMe()
        } else {
          user.value = null
        }
      } catch {
        clearTokens()
        accessToken.value = null
        user.value = null
      } finally {
        initialized.value = true
      }
    })()

    try {
      await bootstrapPromise
    } finally {
      bootstrapPromise = null
    }
  }

  /**
   * 退出登录：尽力通知后端，无条件清理本地登录态。
   *
   * 关键节点：后端登出失败不能阻断本地退出，否则用户会被困在错误状态。
   */
  async function logout(): Promise<void> {
    loading.value = true
    try {
      if (getAccessToken()) {
        // 后端退出仅为尽力而为，失败也必须完成本地清理
        await api.post<null>('/auth/logout')
      }
    } catch {
      // 忽略后端退出失败
    } finally {
      clearTokens()
      accessToken.value = null
      user.value = null
      loading.value = false
      initialized.value = true
    }
  }

  /** 清理本地登录态但不请求后端，用于 auth:unauthorized 事件。 */
  function reset(): void {
    clearTokens()
    accessToken.value = null
    user.value = null
    loading.value = false
    initialized.value = true
  }

  return {
    user,
    initialized,
    loading,
    isAuthenticated,
    role,
    login,
    fetchMe,
    bootstrap,
    logout,
    reset,
  }
})
