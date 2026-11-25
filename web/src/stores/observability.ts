import { ref } from 'vue'
import { defineStore } from 'pinia'

import { ApiError } from '@/api/http'
import {
  getAnalyticsAgents,
  getAnalyticsModels,
  getAnalyticsOverview,
  getAnalyticsTools,
  getObservabilitySettings,
  getPromptTemplate,
  getSpan,
  getTrace,
  getTraceTree,
  listPromptExecutions,
  listPromptTemplates,
  listTraceEvents,
  listTraceSpans,
  listTraces,
  type AgentAnalyticsItem,
  type AnalyticsOverview,
  type AnalyticsQuery,
  type ListTracesParams,
  type ModelAnalyticsItem,
  type ObservabilitySettings,
  type PromptExecutionItem,
  type PromptTemplateDetail,
  type PromptTemplateSummary,
  type SpanDetail,
  type SpanEvent,
  type SpanNode,
  type ToolAnalyticsItem,
  type TraceDetail,
  type TraceSummary,
} from '@/api/observability'

/** 只展示后端返回的错误信息，避免把 axios 细节暴露给用户。 */
function errorToMessage(value: unknown): string {
  if (value instanceof ApiError) {
    return value.message
  }
  if (value instanceof Error && value.message) {
    return value.message
  }
  return '请求失败，请稍后重试'
}

/**
 * 可观测状态仓库，按域拆分。
 * - overview：概览指标卡与图表（Task 14）。
 * - traces：Trace Explorer 列表（Task 15）；请求参数全部由页面从 URL 解析。
 * - traceDetail：Trace 三栏详情（Task 16）；树/扁平 Span/事件并行加载，
 *   选中 Span 后按 span_id 懒加载详情。
 * - analyticsAgents / analyticsTools / analyticsModels：三个分析页，各自独立
 *   单调令牌，旧响应不得覆盖新窗口的结果。
 * - prompts：模板分页、当前模板详情与执行历史。
 * - settings：只读可观测设置。
 */
export const useObservabilityStore = defineStore('observability', () => {
  // ---- Overview ---------------------------------------------------------
  const overview = ref<AnalyticsOverview | null>(null)
  const loading = ref(false)
  const error = ref<string | null>(null)

  // ---- Trace 列表 -------------------------------------------------------
  const traces = ref<TraceSummary[]>([])
  const tracesTotal = ref(0)
  const tracesLoading = ref(false)
  const tracesError = ref<string | null>(null)

  // ---- Trace 详情 -------------------------------------------------------
  const traceDetail = ref<TraceDetail | null>(null)
  const traceTree = ref<SpanNode[]>([])
  const traceSpans = ref<SpanNode[]>([])
  const traceEvents = ref<SpanEvent[]>([])
  const traceDetailLoading = ref(false)
  const traceDetailError = ref<string | null>(null)

  const selectedSpanId = ref<string | null>(null)
  const spanDetail = ref<SpanDetail | null>(null)
  const spanDetailLoading = ref(false)
  const spanDetailError = ref<string | null>(null)

  // ---- 分析页：agents / tools / models ---------------------------------
  // null 表示尚未成功加载；失败时也回到 null，避免展示过期窗口的数据。
  const analyticsAgents = ref<AgentAnalyticsItem[] | null>(null)
  const analyticsAgentsLoading = ref(false)
  const analyticsAgentsError = ref<string | null>(null)

  const analyticsTools = ref<ToolAnalyticsItem[] | null>(null)
  const analyticsToolsLoading = ref(false)
  const analyticsToolsError = ref<string | null>(null)

  const analyticsModels = ref<ModelAnalyticsItem[] | null>(null)
  const analyticsModelsLoading = ref(false)
  const analyticsModelsError = ref<string | null>(null)

  // ---- Prompt 模板 / 详情 / 执行历史 -----------------------------------
  const promptTemplates = ref<PromptTemplateSummary[]>([])
  const promptTemplatesTotal = ref(0)
  const promptTemplatesLoading = ref(false)
  const promptTemplatesError = ref<string | null>(null)

  const promptTemplate = ref<PromptTemplateDetail | null>(null)
  const promptTemplateLoading = ref(false)
  const promptTemplateError = ref<string | null>(null)

  const promptExecutions = ref<PromptExecutionItem[]>([])
  const promptExecutionsTotal = ref(0)
  const promptExecutionsLoading = ref(false)
  const promptExecutionsError = ref<string | null>(null)

  // ---- 可观测设置（只读） ----------------------------------------------
  const settings = ref<ObservabilitySettings | null>(null)
  const settingsLoading = ref(false)
  const settingsError = ref<string | null>(null)

  // 单调递增令牌：旧请求返回时不得覆盖更新的结果。各域独立递增。
  let overviewToken = 0
  let tracesToken = 0
  let traceDetailToken = 0
  let spanDetailToken = 0
  let agentsToken = 0
  let toolsToken = 0
  let modelsToken = 0
  let promptTemplatesToken = 0
  let promptTemplateToken = 0
  let promptExecutionsToken = 0
  let settingsToken = 0

  async function loadOverview(query?: AnalyticsQuery): Promise<void> {
    const token = ++overviewToken
    loading.value = true
    error.value = null

    try {
      const result = await getAnalyticsOverview(query)
      if (token !== overviewToken) {
        return
      }
      overview.value = result
    } catch (cause) {
      if (token !== overviewToken) {
        return
      }
      overview.value = null
      error.value = errorToMessage(cause)
    } finally {
      if (token === overviewToken) {
        loading.value = false
      }
    }
  }

  /** 按 URL 解析出的参数加载 Trace 列表；失败时清空旧数据，避免展示过期结果。 */
  async function loadTraces(params: ListTracesParams = {}): Promise<void> {
    const token = ++tracesToken
    tracesLoading.value = true
    tracesError.value = null

    try {
      const result = await listTraces(params)
      if (token !== tracesToken) {
        return
      }
      traces.value = result.items
      tracesTotal.value = result.total
    } catch (cause) {
      if (token !== tracesToken) {
        return
      }
      traces.value = []
      tracesTotal.value = 0
      tracesError.value = errorToMessage(cause)
    } finally {
      if (token === tracesToken) {
        tracesLoading.value = false
      }
    }
  }

  /**
   * 并行加载 Trace 概要、Span 树、扁平 Span 与事件。
   * 任一请求失败都视为整页失败：清空旧数据，避免混入上一次 Trace 的结果。
   */
  async function loadTraceDetail(traceId: string): Promise<void> {
    const token = ++traceDetailToken
    // 新 Trace 开始时让旧的 Span 详情请求失效。
    spanDetailToken += 1
    traceDetailLoading.value = true
    traceDetailError.value = null
    selectedSpanId.value = null
    spanDetail.value = null
    spanDetailLoading.value = false
    spanDetailError.value = null

    try {
      const [detail, tree, spans, events] = await Promise.all([
        getTrace(traceId),
        getTraceTree(traceId),
        listTraceSpans(traceId),
        listTraceEvents(traceId),
      ])
      if (token !== traceDetailToken) {
        return
      }
      traceDetail.value = detail
      traceTree.value = tree
      traceSpans.value = spans
      traceEvents.value = events
    } catch (cause) {
      if (token !== traceDetailToken) {
        return
      }
      traceDetail.value = null
      traceTree.value = []
      traceSpans.value = []
      traceEvents.value = []
      traceDetailError.value = errorToMessage(cause)
    } finally {
      if (token === traceDetailToken) {
        traceDetailLoading.value = false
      }
    }
  }

  /** 选中 Span 并懒加载详情；快速切换时旧响应不会覆盖新选择。 */
  async function selectTraceSpan(spanId: string): Promise<void> {
    const token = ++spanDetailToken
    selectedSpanId.value = spanId
    spanDetail.value = null
    spanDetailLoading.value = true
    spanDetailError.value = null

    try {
      const detail = await getSpan(spanId)
      // 期间已切到别的 Span（或整页重载）时丢弃这次过期响应。
      if (token !== spanDetailToken || selectedSpanId.value !== spanId) {
        return
      }
      spanDetail.value = detail
    } catch (cause) {
      if (token !== spanDetailToken || selectedSpanId.value !== spanId) {
        return
      }
      spanDetailError.value = errorToMessage(cause)
    } finally {
      if (token === spanDetailToken) {
        spanDetailLoading.value = false
      }
    }
  }

  /** 清空选中态与 Span 详情，并让在途的详情请求失效。 */
  function clearSelectedSpan(): void {
    spanDetailToken += 1
    selectedSpanId.value = null
    spanDetail.value = null
    spanDetailLoading.value = false
    spanDetailError.value = null
  }

  /** Agent 分析：失败时清空旧列表，避免把上个窗口的数据留在页面上。 */
  async function loadAnalyticsAgents(query?: AnalyticsQuery): Promise<void> {
    const token = ++agentsToken
    analyticsAgentsLoading.value = true
    analyticsAgentsError.value = null

    try {
      const result = await getAnalyticsAgents(query)
      if (token !== agentsToken) {
        return
      }
      analyticsAgents.value = result.items
    } catch (cause) {
      if (token !== agentsToken) {
        return
      }
      analyticsAgents.value = null
      analyticsAgentsError.value = errorToMessage(cause)
    } finally {
      if (token === agentsToken) {
        analyticsAgentsLoading.value = false
      }
    }
  }

  /** 工具分析：失败时清空旧列表。 */
  async function loadAnalyticsTools(query?: AnalyticsQuery): Promise<void> {
    const token = ++toolsToken
    analyticsToolsLoading.value = true
    analyticsToolsError.value = null

    try {
      const result = await getAnalyticsTools(query)
      if (token !== toolsToken) {
        return
      }
      analyticsTools.value = result.items
    } catch (cause) {
      if (token !== toolsToken) {
        return
      }
      analyticsTools.value = null
      analyticsToolsError.value = errorToMessage(cause)
    } finally {
      if (token === toolsToken) {
        analyticsToolsLoading.value = false
      }
    }
  }

  /** 模型分析：失败时清空旧列表。 */
  async function loadAnalyticsModels(query?: AnalyticsQuery): Promise<void> {
    const token = ++modelsToken
    analyticsModelsLoading.value = true
    analyticsModelsError.value = null

    try {
      const result = await getAnalyticsModels(query)
      if (token !== modelsToken) {
        return
      }
      analyticsModels.value = result.items
    } catch (cause) {
      if (token !== modelsToken) {
        return
      }
      analyticsModels.value = null
      analyticsModelsError.value = errorToMessage(cause)
    } finally {
      if (token === modelsToken) {
        analyticsModelsLoading.value = false
      }
    }
  }

  /** 分页加载 Prompt 模板；失败时清空列表与总数。 */
  async function loadPromptTemplates(
    params: { page?: number; size?: number } = {},
  ): Promise<void> {
    const token = ++promptTemplatesToken
    promptTemplatesLoading.value = true
    promptTemplatesError.value = null

    try {
      const result = await listPromptTemplates(params)
      if (token !== promptTemplatesToken) {
        return
      }
      promptTemplates.value = result.items
      promptTemplatesTotal.value = result.total
    } catch (cause) {
      if (token !== promptTemplatesToken) {
        return
      }
      promptTemplates.value = []
      promptTemplatesTotal.value = 0
      promptTemplatesError.value = errorToMessage(cause)
    } finally {
      if (token === promptTemplatesToken) {
        promptTemplatesLoading.value = false
      }
    }
  }

  /** 加载当前模板详情（含全部版本）；失败时清空旧详情。 */
  async function loadPromptTemplate(templatePk: number): Promise<void> {
    const token = ++promptTemplateToken
    promptTemplateLoading.value = true
    promptTemplateError.value = null

    try {
      const result = await getPromptTemplate(templatePk)
      if (token !== promptTemplateToken) {
        return
      }
      promptTemplate.value = result
    } catch (cause) {
      if (token !== promptTemplateToken) {
        return
      }
      promptTemplate.value = null
      promptTemplateError.value = errorToMessage(cause)
    } finally {
      if (token === promptTemplateToken) {
        promptTemplateLoading.value = false
      }
    }
  }

  /** 分页加载执行历史；失败时清空旧记录。 */
  async function loadPromptExecutions(
    templatePk: number,
    params: { page?: number; size?: number } = {},
  ): Promise<void> {
    const token = ++promptExecutionsToken
    promptExecutionsLoading.value = true
    promptExecutionsError.value = null

    try {
      const result = await listPromptExecutions(templatePk, params)
      if (token !== promptExecutionsToken) {
        return
      }
      promptExecutions.value = result.items
      promptExecutionsTotal.value = result.total
    } catch (cause) {
      if (token !== promptExecutionsToken) {
        return
      }
      promptExecutions.value = []
      promptExecutionsTotal.value = 0
      promptExecutionsError.value = errorToMessage(cause)
    } finally {
      if (token === promptExecutionsToken) {
        promptExecutionsLoading.value = false
      }
    }
  }

  /** 读取可观测设置；失败时清空旧配置。 */
  async function loadSettings(): Promise<void> {
    const token = ++settingsToken
    settingsLoading.value = true
    settingsError.value = null

    try {
      const result = await getObservabilitySettings()
      if (token !== settingsToken) {
        return
      }
      settings.value = result
    } catch (cause) {
      if (token !== settingsToken) {
        return
      }
      settings.value = null
      settingsError.value = errorToMessage(cause)
    } finally {
      if (token === settingsToken) {
        settingsLoading.value = false
      }
    }
  }

  /** 清空状态并让任何在途请求失效。 */
  function reset(): void {
    overviewToken += 1
    overview.value = null
    loading.value = false
    error.value = null

    tracesToken += 1
    traces.value = []
    tracesTotal.value = 0
    tracesLoading.value = false
    tracesError.value = null

    traceDetailToken += 1
    spanDetailToken += 1
    traceDetail.value = null
    traceTree.value = []
    traceSpans.value = []
    traceEvents.value = []
    traceDetailLoading.value = false
    traceDetailError.value = null
    selectedSpanId.value = null
    spanDetail.value = null
    spanDetailLoading.value = false
    spanDetailError.value = null

    agentsToken += 1
    analyticsAgents.value = null
    analyticsAgentsLoading.value = false
    analyticsAgentsError.value = null

    toolsToken += 1
    analyticsTools.value = null
    analyticsToolsLoading.value = false
    analyticsToolsError.value = null

    modelsToken += 1
    analyticsModels.value = null
    analyticsModelsLoading.value = false
    analyticsModelsError.value = null

    promptTemplatesToken += 1
    promptTemplates.value = []
    promptTemplatesTotal.value = 0
    promptTemplatesLoading.value = false
    promptTemplatesError.value = null

    promptTemplateToken += 1
    promptTemplate.value = null
    promptTemplateLoading.value = false
    promptTemplateError.value = null

    promptExecutionsToken += 1
    promptExecutions.value = []
    promptExecutionsTotal.value = 0
    promptExecutionsLoading.value = false
    promptExecutionsError.value = null

    settingsToken += 1
    settings.value = null
    settingsLoading.value = false
    settingsError.value = null
  }

  return {
    overview,
    loading,
    error,
    loadOverview,
    traces,
    tracesTotal,
    tracesLoading,
    tracesError,
    loadTraces,
    traceDetail,
    traceTree,
    traceSpans,
    traceEvents,
    traceDetailLoading,
    traceDetailError,
    selectedSpanId,
    spanDetail,
    spanDetailLoading,
    spanDetailError,
    loadTraceDetail,
    selectTraceSpan,
    clearSelectedSpan,
    analyticsAgents,
    analyticsAgentsLoading,
    analyticsAgentsError,
    loadAnalyticsAgents,
    analyticsTools,
    analyticsToolsLoading,
    analyticsToolsError,
    loadAnalyticsTools,
    analyticsModels,
    analyticsModelsLoading,
    analyticsModelsError,
    loadAnalyticsModels,
    promptTemplates,
    promptTemplatesTotal,
    promptTemplatesLoading,
    promptTemplatesError,
    loadPromptTemplates,
    promptTemplate,
    promptTemplateLoading,
    promptTemplateError,
    loadPromptTemplate,
    promptExecutions,
    promptExecutionsTotal,
    promptExecutionsLoading,
    promptExecutionsError,
    loadPromptExecutions,
    settings,
    settingsLoading,
    settingsError,
    loadSettings,
    reset,
  }
})
