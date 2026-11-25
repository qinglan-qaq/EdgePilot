import type { UserRole } from '@/api/http'

/** 路由与导航共用的角色过滤规则：未声明角色即所有已登录角色可访问。 */
export function canAccessRoles(
  requiredRoles: UserRole[] | undefined,
  currentRole: UserRole | null,
): boolean {
  if (!requiredRoles?.length) {
    return true
  }
  return currentRole !== null && requiredRoles.includes(currentRole)
}
