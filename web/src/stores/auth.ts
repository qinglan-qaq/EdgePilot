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
