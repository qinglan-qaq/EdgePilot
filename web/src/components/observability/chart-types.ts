/** 观测图表组件的共享类型（无图表库依赖）。 */

/** 一个横轴点；values 用 series.key 取值，缺失或 null 表示未上报。 */
export interface TrendPoint {
  label: string
  values: Record<string, number | null>
}

/** 一条折线 / 一组柱子的定义。 */
export interface TrendSeries {
  key: string
  label: string
  color?: string
}

/** 横向条形列表的色调。 */
export type BarTone = 'default' | 'success' | 'warning' | 'danger' | 'info'

/** 横向条形列表的一行。value 为 null 表示未上报，不画条。 */
export interface BarListItem {
  label: string
  value: number | null
  detail?: string | null
  tone?: BarTone
}
