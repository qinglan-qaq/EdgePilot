/**
 * 运行报告与证据的只读解析工具。
 * 仅从真实接口返回的 output 中提取字段，不生成任何占位证据。
 */

export interface ReportEvidence {
  type: string
  ref: string
  snippet: string | null
}

/** 报告中的可回溯结论：statement 至少关联一条证据。 */
export interface ReportHighlight {
  statement: string
  evidence: ReportEvidence[]
}

export interface RunReport {
  title: string | null
  markdown: string
  highlights: ReportHighlight[]
  findingCount: number | null
  evidence: ReportEvidence[]
}

function normalizeEvidence(raw: unknown): ReportEvidence | null {
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return null
  }
  const record = raw as Record<string, unknown>
  const type = typeof record.type === 'string' ? record.type.trim() : ''
  const rawRef = record.ref
  const ref =
    typeof rawRef === 'string'
      ? rawRef
      : typeof rawRef === 'number' && Number.isFinite(rawRef)
        ? String(rawRef)
        : ''
  if (!type || !ref) {
    return null
  }
  return {
    type,
    ref,
    snippet: typeof record.snippet === 'string' ? record.snippet : null,
  }
}

/**
 * 递归收集 output 中任意层级的 evidence 数组。
 * 以 `type:ref` 去重并保持首次出现顺序；只遍历对象/数组，不处理循环引用。
 */
export function collectEvidence(...sources: unknown[]): ReportEvidence[] {
  const result: ReportEvidence[] = []
  const seen = new Set<string>()
  const visited = new WeakSet<object>()

  const walk = (value: unknown): void => {
    if (Array.isArray(value)) {
      for (const item of value) {
        walk(item)
      }
      return
    }
    if (typeof value !== 'object' || value === null) {
      return
    }
    if (visited.has(value)) {
      return
    }
    visited.add(value)

    const record = value as Record<string, unknown>
    if (Array.isArray(record.evidence)) {
      for (const raw of record.evidence) {
        const item = normalizeEvidence(raw)
        if (!item) {
          continue
        }
        const key = `${item.type}:${item.ref}`
        if (seen.has(key)) {
          continue
        }
        seen.add(key)
        result.push(item)
      }
    }

    for (const [key, child] of Object.entries(record)) {
      if (key === 'evidence') {
        continue
      }
      walk(child)
    }
  }

  for (const source of sources) {
    walk(source)
  }
  return result
}

/** 兼容后端对象结构与历史字符串结构；无 statement 的条目直接丢弃。 */
function normalizeHighlight(raw: unknown): ReportHighlight | null {
  if (typeof raw === 'string') {
    const statement = raw.trim()
    return statement ? { statement, evidence: [] } : null
  }
  if (typeof raw !== 'object' || raw === null || Array.isArray(raw)) {
    return null
  }
  const record = raw as Record<string, unknown>
  const statement =
    typeof record.statement === 'string' ? record.statement.trim() : ''
  if (!statement) {
    return null
  }
  return {
    statement,
    evidence: Array.isArray(record.evidence)
      ? record.evidence
          .map((item) => normalizeEvidence(item))
          .filter((item): item is ReportEvidence => item !== null)
      : [],
  }
}

/** 识别 run.output 中的报告结构；没有 markdown 字段时返回 null。 */
export function extractRunReport(output: unknown): RunReport | null {
  if (typeof output !== 'object' || output === null || Array.isArray(output)) {
    return null
  }
  const record = output as Record<string, unknown>
  if (typeof record.markdown !== 'string') {
    return null
  }
  return {
    title: typeof record.title === 'string' ? record.title : null,
    markdown: record.markdown,
    highlights: Array.isArray(record.highlights)
      ? record.highlights
          .map((item) => normalizeHighlight(item))
          .filter((item): item is ReportHighlight => item !== null)
      : [],
    findingCount:
      typeof record.finding_count === 'number' && Number.isFinite(record.finding_count)
        ? record.finding_count
        : null,
    evidence: Array.isArray(record.evidence)
      ? record.evidence
          .map((item) => normalizeEvidence(item))
          .filter((item): item is ReportEvidence => item !== null)
      : [],
  }
}

/** JSON 展示：字符串尽量解析为结构化 JSON，解析失败则原样展示。 */
export function formatJson(value: unknown): string {
  if (value === undefined) {
    return '未上报'
  }
  if (value === null) {
    return 'null'
  }
  if (typeof value === 'string') {
    const text = value.trim()
    if (!text) {
      return '无内容'
    }
    try {
      return JSON.stringify(JSON.parse(text), null, 2)
    } catch {
      return value
    }
  }
  try {
    const serialized = JSON.stringify(value, null, 2)
    return serialized === undefined ? String(value) : serialized
  } catch {
    return String(value)
  }
}
