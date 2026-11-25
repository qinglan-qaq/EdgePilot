/**
 * 跨 API 模块共享的响应结构。
 * 单独成文件避免 runs.ts 与 workflows.ts 之间产生循环依赖。
 */

/** 后端统一分页信封解包后的结构。 */
export interface PageResult<T> {
  items: T[]
  total: number
  page: number
  size: number
}
