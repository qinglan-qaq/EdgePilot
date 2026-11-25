/**
 * 访问令牌与刷新令牌的本地存储。
 * 仅持久化令牌，不存储用户资料等敏感信息。
 */

const ACCESS_TOKEN_KEY = 'edgepilot.access_token'
const REFRESH_TOKEN_KEY = 'edgepilot.refresh_token'

/** 在 localStorage 不可用（隐私模式、SSR、异常）时返回 null，调用方无需额外判断。 */
function getStorage(): Storage | null {
  try {
    if (typeof window === 'undefined' || !window.localStorage) {
      return null
    }
    return window.localStorage
  } catch {
    return null
  }
}

function readItem(key: string): string | null {
  try {
    return getStorage()?.getItem(key) ?? null
  } catch {
    return null
  }
}

function writeItem(key: string, value: string): void {
  try {
    getStorage()?.setItem(key, value)
  } catch {
    // 存储失败不影响内存中的登录态，静默忽略
  }
}

function removeItem(key: string): void {
  try {
    getStorage()?.removeItem(key)
  } catch {
    // 忽略清理异常
  }
}

export function getAccessToken(): string | null {
  return readItem(ACCESS_TOKEN_KEY)
}

export function getRefreshToken(): string | null {
  return readItem(REFRESH_TOKEN_KEY)
}

export function setAccessToken(token: string): void {
  writeItem(ACCESS_TOKEN_KEY, token)
}

export function setRefreshToken(token: string): void {
  writeItem(REFRESH_TOKEN_KEY, token)
}

export function setTokens(pair: { access_token: string; refresh_token: string }): void {
  setAccessToken(pair.access_token)
  setRefreshToken(pair.refresh_token)
}

export function clearTokens(): void {
  removeItem(ACCESS_TOKEN_KEY)
  removeItem(REFRESH_TOKEN_KEY)
}
