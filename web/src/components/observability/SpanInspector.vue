<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import type {
  LLMCallRecord,
  PromptExecutionRecord,
  RetrievalRecord,
  SpanDetail,
  SpanEvent,
  ToolCallRecord,
} from '@/api/observability'
import JsonViewer from '@/components/observability/JsonViewer.vue'
import { formatCost, formatDurationMs, runStatusLabel, spanTypeLabel } from '@/utils/status'

/**
 * Span 检查器：10 个懒加载 Tab，全部只展示接口真实返回的字段。
 * 缺失值统一显示“未上报 / 未记录 / 不可用”，绝不补 0 或估算。
 */
const props = withDefaults(
  defineProps<{
    span: SpanDetail | null
    events: SpanEvent[]
    selectedSpanId?: string | null
    loading?: boolean
    error?: string | null
  }>(),
  {
    selectedSpanId: null,
    loading: false,
    error: null,
  },
)

const emit = defineEmits<{
  retry: [spanId: string]
}>()

const activeTab = ref('overview')

// 切换 Span 后回到“概览”，避免停留在上一个 Span 的 Tab。
watch(
  () => props.span?.span_id,
  () => {
    activeTab.value = 'overview'
  },
)

const llmCalls = computed<LLMCallRecord[]>(() => props.span?.llm_calls ?? [])
const toolCalls = computed<ToolCallRecord[]>(() => props.span?.tool_calls ?? [])
const retrievals = computed<RetrievalRecord[]>(
  () => props.span?.retrieval_records ?? props.span?.retrievals ?? [],
)
const promptExecutions = computed<PromptExecutionRecord[]>(
  () => props.span?.prompt_executions ?? [],
)

const sortedEvents = computed(() =>
  [...props.events].sort((a, b) => a.sequence_number - b.sequence_number),
)

function unknownText(value: unknown): string {
  if (value === null || value === undefined) {
    return '未上报'
  }
  if (typeof value === 'string') {
    return value.trim() ? value : '未上报'
  }
  return String(value)
}

function numberOrUnknown(value: unknown): string {
  return typeof value === 'number' && Number.isFinite(value) ? String(value) : '未上报'
}

/** cost 为 null 或 cost_available=false 时只提示不可用，绝不显示 0 或估算。 */
function llmCostLabel(call: LLMCallRecord): string {
  if (call.cost_available === false || call.cost === null || call.cost === undefined) {
    return '费用不可用'
  }
  return formatCost(call.cost)
}

function usageLabel(call: LLMCallRecord, key: string): string {
  return numberOrUnknown(call[key])
}

/** 失败调用历史：只挑出 error 非空的行，按 attempt 展示。 */
interface FailureRow {
  key: string
  kind: string
  attempt: number | null
  label: string
  error: unknown
}

const failureRows = computed<FailureRow[]>(() => {
  const rows: FailureRow[] = []
  for (const call of llmCalls.value) {
    if (call.error === null || call.error === undefined) {
      continue
    }
    rows.push({
      key: `llm-${call.id ?? rows.length}`,
      kind: 'LLM',
      attempt: typeof call.attempt === 'number' ? call.attempt : null,
      label: [call.provider, call.model_name].filter(Boolean).join(' / ') || '模型未上报',
      error: call.error,
    })
  }
  for (const call of toolCalls.value) {
    if (call.error === null || call.error === undefined) {
      continue
    }
    rows.push({
      key: `tool-${call.id ?? rows.length}`,
      kind: '工具',
      attempt: typeof call.attempt === 'number' ? call.attempt : null,
      label: call.tool_name ?? call.tool_id ?? '工具未上报',
      error: call.error,
    })
  }
  return rows
})

interface DocRow {
  text: string
  docId: string
  score: string
}

function firstString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) {
      return value
    }
  }
  return null
}

function firstScalar(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value === 'string' && value.trim()) {
      return value
    }
    if (typeof value === 'number' && Number.isFinite(value)) {
      return String(value)
    }
  }
  return null
}

/** 文档条目字段名以真实返回为准；缺失字段显示未上报，不猜测内容。 */
function normalizeDocs(value: unknown): DocRow[] {
  if (!Array.isArray(value)) {
    return []
  }
  return value.map((item) => {
    if (typeof item === 'string') {
      return { text: item, docId: '未上报', score: '未上报' }
    }
    if (typeof item !== 'object' || item === null) {
      return { text: String(item), docId: '未上报', score: '未上报' }
    }
    const record = item as Record<string, unknown>
    const text =
      firstString(record.text, record.content, record.snippet, record.page_content) ??
      '未上报'
    const docId = firstScalar(record.doc_id, record.id, record.chunk_id) ?? '未上报'
    const rawScore = record.score
    const score =
      typeof rawScore === 'number' && Number.isFinite(rawScore)
        ? String(rawScore)
        : rawScore === null || rawScore === undefined
          ? '未上报'
          : String(rawScore)
    return { text, docId, score }
  })
}

function retrievedDocs(record: RetrievalRecord): DocRow[] {
  return normalizeDocs(record.retrieved_documents)
}

function rerankedDocs(record: RetrievalRecord): DocRow[] {
  return normalizeDocs(record.reranked_documents)
}

function finalContextText(record: RetrievalRecord): string {
  return typeof record.final_context === 'string' && record.final_context.trim()
    ? record.final_context
    : '未记录'
}

/** Prompt variables 的 key 级差异；只比较真实记录，不伪造 diff。 */
interface PromptDiff {
  fromIndex: number
  toIndex: number
  fromVersion: number | null
  toVersion: number | null
  added: string[]
  removed: string[]
  changed: string[]
}

function asRecord(value: unknown): Record<string, unknown> {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return {}
  }
  return value as Record<string, unknown>
}

const promptDiffs = computed<PromptDiff[]>(() => {
  const list = promptExecutions.value
  if (list.length < 2) {
    return []
  }
  const diffs: PromptDiff[] = []
  for (let index = 1; index < list.length; index += 1) {
    const previous = list[index - 1]
    const current = list[index]
    const before = asRecord(previous.variables)
    const after = asRecord(current.variables)
    const added = Object.keys(after).filter((key) => !(key in before))
    const removed = Object.keys(before).filter((key) => !(key in after))
    const changed = Object.keys(after).filter(
      (key) =>
        key in before &&
        JSON.stringify(before[key]) !== JSON.stringify(after[key]),
    )
    diffs.push({
      fromIndex: index,
      toIndex: index + 1,
      fromVersion: previous.template_version ?? null,
      toVersion: current.template_version ?? null,
      added,
      removed,
      changed,
    })
  }
  return diffs
})

function diffEmpty(diff: PromptDiff): boolean {
  return diff.added.length + diff.removed.length + diff.changed.length === 0
}

function isCurrentEvent(event: SpanEvent): boolean {
  return props.selectedSpanId !== null && event.span_id === props.selectedSpanId
}

function retry(spanId: string): void {
  emit('retry', spanId)
}
</script>

<template>
  <section class="span-inspector" data-test="span-inspector">
    <header class="span-inspector__header">
      <div class="span-inspector__title-row">
        <h3 class="span-inspector__title" data-test="span-inspector-title">
          {{ span?.name ?? 'Span 详情' }}
        </h3>
        <el-tag v-if="span" size="small" type="info" effect="plain">
          {{ spanTypeLabel(span.span_type) }}
        </el-tag>
      </div>
      <p v-if="selectedSpanId" class="span-inspector__id" data-test="span-inspector-id">
        {{ selectedSpanId }}
      </p>
    </header>

    <div v-if="loading" class="span-inspector__state" data-test="span-inspector-loading">
      加载 Span 详情…
    </div>

    <div v-else-if="error" class="span-inspector__state" data-test="span-inspector-error">
      <el-alert :title="error" type="error" :closable="false" show-icon />
      <el-button
        v-if="selectedSpanId"
        class="span-inspector__retry"
        type="primary"
        plain
        data-test="span-inspector-retry"
        @click="retry(selectedSpanId)"
      >
        重试
      </el-button>
    </div>

    <div
      v-else-if="!span"
      class="span-inspector__state"
      data-test="span-inspector-empty"
    >
      在左侧 Span 树或中间时间线选择一个 Span 查看详情。
    </div>

    <template v-else>
      <div class="span-inspector__meta">
        <span>状态：{{ runStatusLabel(span.status) }}</span>
        <span>耗时：{{ formatDurationMs(span.duration_ms) }}</span>
        <span>attempt：{{ numberOrUnknown(span.attempt) }}</span>
      </div>

      <el-tabs v-model="activeTab" class="span-inspector__tabs">
        <el-tab-pane label="概览" name="overview" lazy>
          <el-descriptions :column="1" border size="small" data-test="span-overview">
            <el-descriptions-item label="Span ID">
              <span class="span-inspector__mono">{{ span.span_id }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="名称">{{ span.name }}</el-descriptions-item>
            <el-descriptions-item label="类型">
              {{ spanTypeLabel(span.span_type) }}
            </el-descriptions-item>
            <el-descriptions-item label="状态">
              {{ runStatusLabel(span.status) }}
            </el-descriptions-item>
            <el-descriptions-item label="耗时">
              {{ formatDurationMs(span.duration_ms) }}
            </el-descriptions-item>
            <el-descriptions-item label="attempt">
              {{ numberOrUnknown(span.attempt) }}
            </el-descriptions-item>
            <el-descriptions-item label="parent_span_id">
              {{ unknownText(span.parent_span_id) }}
            </el-descriptions-item>
            <el-descriptions-item label="agent">
              <template v-if="span.agent_id === null || span.agent_id === undefined">
                未上报
              </template>
              <template v-else>
                {{ span.agent_id }}
                <span v-if="span.agent_version !== null && span.agent_version !== undefined">
                  （v{{ span.agent_version }}）
                </span>
              </template>
            </el-descriptions-item>
            <el-descriptions-item label="workflow / node">
              {{ unknownText(span.workflow_id) }} / {{ unknownText(span.node_id) }}
            </el-descriptions-item>
            <el-descriptions-item label="tool_id">
              {{ unknownText(span.tool_id) }}
            </el-descriptions-item>
            <el-descriptions-item label="retry_count">
              {{ numberOrUnknown(span.retry_count) }}
            </el-descriptions-item>
            <el-descriptions-item label="开始时间">
              {{ unknownText(span.start_time) }}
            </el-descriptions-item>
            <el-descriptions-item label="结束时间">
              {{ unknownText(span.end_time) }}
            </el-descriptions-item>
          </el-descriptions>
        </el-tab-pane>

        <el-tab-pane label="输入" name="input" lazy>
          <JsonViewer :value="span.input" :filename="`span-${span.span_id}-input.json`" />
        </el-tab-pane>

        <el-tab-pane label="输出" name="output" lazy>
          <JsonViewer :value="span.output" :filename="`span-${span.span_id}-output.json`" />
        </el-tab-pane>

        <el-tab-pane label="模型" name="model" lazy>
          <p v-if="llmCalls.length === 0" class="span-inspector__muted" data-test="no-llm-calls">
            该 Span 没有模型调用记录
          </p>
          <div v-else class="span-inspector__list">
            <article
              v-for="(call, index) in llmCalls"
              :key="call.id ?? index"
              class="span-inspector__card"
              data-test="llm-call"
            >
              <p class="span-inspector__card-title">
                {{ unknownText(call.provider) }} / {{ unknownText(call.model_name) }}
              </p>
              <div class="span-inspector__fields">
                <span>attempt：{{ numberOrUnknown(call.attempt) }}</span>
                <span>temperature：{{ numberOrUnknown(call.temperature) }}</span>
                <span>top_p：{{ numberOrUnknown(call.top_p) }}</span>
                <span>max_tokens：{{ numberOrUnknown(call.max_tokens) }}</span>
                <span>prompt_tokens：{{ usageLabel(call, 'prompt_tokens') }}</span>
                <span>completion_tokens：{{ usageLabel(call, 'completion_tokens') }}</span>
                <span>cached_tokens：{{ usageLabel(call, 'cached_tokens') }}</span>
                <span>total_tokens：{{ usageLabel(call, 'total_tokens') }}</span>
                <span class="span-inspector__cost" data-test="llm-cost">
                  cost：{{ llmCostLabel(call) }}
                </span>
              </div>
              <template v-if="call.error !== null && call.error !== undefined">
                <p class="span-inspector__label">error</p>
                <JsonViewer :value="call.error" />
              </template>
            </article>
          </div>
        </el-tab-pane>

        <el-tab-pane label="工具" name="tool" lazy>
          <p v-if="toolCalls.length === 0" class="span-inspector__muted" data-test="no-tool-calls">
            该 Span 没有工具调用记录
          </p>
          <div v-else class="span-inspector__list">
            <article
              v-for="(call, index) in toolCalls"
              :key="call.id ?? index"
              class="span-inspector__card"
              data-test="tool-call"
            >
              <p class="span-inspector__card-title">
                {{ unknownText(call.tool_name) }}
                <span class="span-inspector__mono">{{ unknownText(call.tool_id) }}</span>
              </p>
              <div class="span-inspector__fields">
                <span>execution_status：{{ unknownText(call.execution_status) }}</span>
                <span>permission_result：{{ unknownText(call.permission_result) }}</span>
                <span>attempt：{{ numberOrUnknown(call.attempt) }}</span>
                <span>retry_count：{{ numberOrUnknown(call.retry_count) }}</span>
                <span>duration_ms：{{ formatDurationMs(call.duration_ms ?? null) }}</span>
              </div>

              <p class="span-inspector__label">input_arguments</p>
              <JsonViewer :value="call.input_arguments" />

              <p class="span-inspector__label">output_result</p>
              <JsonViewer :value="call.output_result" />

              <p class="span-inspector__label">input_schema（仅展示 Schema，不推断校验结果）</p>
              <JsonViewer :value="call.input_schema" />

              <p class="span-inspector__label">output_schema（仅展示 Schema，不推断校验结果）</p>
              <JsonViewer :value="call.output_schema" />

              <template v-if="call.error !== null && call.error !== undefined">
                <p class="span-inspector__label">error</p>
                <JsonViewer :value="call.error" />
              </template>
            </article>
          </div>
        </el-tab-pane>

        <el-tab-pane label="检索" name="retrieval" lazy>
          <p v-if="retrievals.length === 0" class="span-inspector__muted" data-test="no-retrievals">
            该 Span 没有检索记录
          </p>
          <div v-else class="span-inspector__list">
            <article
              v-for="(record, index) in retrievals"
              :key="record.id ?? index"
              class="span-inspector__card"
              data-test="retrieval-record"
            >
              <p class="span-inspector__card-title">{{ unknownText(record.query) }}</p>
              <div class="span-inspector__fields">
                <span>rewritten_query：{{ unknownText(record.rewritten_query) }}</span>
                <span>strategy：{{ unknownText(record.retrieval_strategy) }}</span>
                <span>embedding_model：{{ unknownText(record.embedding_model) }}</span>
                <span>vector_store：{{ unknownText(record.vector_store) }}</span>
                <span>reranker_model：{{ unknownText(record.reranker_model) }}</span>
                <span>top_k：{{ numberOrUnknown(record.top_k) }}</span>
                <span>similarity_threshold：{{ numberOrUnknown(record.similarity_threshold) }}</span>
                <span>duration_ms：{{ formatDurationMs(record.duration_ms ?? null) }}</span>
              </div>

              <p class="span-inspector__label">retrieved_documents</p>
              <table
                v-if="retrievedDocs(record).length > 0"
                class="span-inspector__docs"
                data-test="retrieved-docs"
              >
                <thead>
                  <tr>
                    <th>doc_id</th>
                    <th>score</th>
                    <th>文本</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(doc, docIndex) in retrievedDocs(record)" :key="docIndex">
                    <td class="span-inspector__mono">{{ doc.docId }}</td>
                    <td>{{ doc.score }}</td>
                    <td>{{ doc.text }}</td>
                  </tr>
                </tbody>
              </table>
              <p v-else class="span-inspector__muted">未上报候选文档</p>

              <p class="span-inspector__label">reranked_documents</p>
              <table
                v-if="rerankedDocs(record).length > 0"
                class="span-inspector__docs"
                data-test="reranked-docs"
              >
                <thead>
                  <tr>
                    <th>doc_id</th>
                    <th>score</th>
                    <th>文本</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="(doc, docIndex) in rerankedDocs(record)" :key="docIndex">
                    <td class="span-inspector__mono">{{ doc.docId }}</td>
                    <td>{{ doc.score }}</td>
                    <td>{{ doc.text }}</td>
                  </tr>
                </tbody>
              </table>
              <p v-else class="span-inspector__muted">未上报重排文档</p>

              <p class="span-inspector__label">final_context</p>
              <pre class="span-inspector__context">{{ finalContextText(record) }}</pre>

              <template v-if="record.error !== null && record.error !== undefined">
                <p class="span-inspector__label">error</p>
                <JsonViewer :value="record.error" />
              </template>
            </article>
          </div>
        </el-tab-pane>

        <el-tab-pane label="Prompt" name="prompt" lazy>
          <p
            v-if="promptExecutions.length === 0"
            class="span-inspector__muted"
            data-test="no-prompt-executions"
          >
            该 Span 没有 Prompt 执行记录
          </p>
          <template v-else>
            <div class="span-inspector__list">
              <article
                v-for="(execution, index) in promptExecutions"
                :key="execution.id ?? index"
                class="span-inspector__card"
                data-test="prompt-execution"
              >
                <p class="span-inspector__card-title">
                  模板 {{ unknownText(execution.template_id) }}
                  <span data-test="prompt-template-version">
                    v{{ unknownText(execution.template_version) }}
                  </span>
                </p>
                <div class="span-inspector__fields">
                  <span>render_status：{{ unknownText(execution.render_status) }}</span>
                  <span>model_name：{{ unknownText(execution.model_name) }}</span>
                  <span class="span-inspector__mono">
                    prompt_hash：{{ unknownText(execution.prompt_hash) }}
                  </span>
                </div>

                <p class="span-inspector__label">variables</p>
                <JsonViewer :value="execution.variables" />

                <p class="span-inspector__label">rendered_messages</p>
                <JsonViewer :value="execution.rendered_messages" />
              </article>
            </div>

            <section v-if="promptDiffs.length > 0" class="span-inspector__diff" data-test="prompt-diff">
              <h4 class="span-inspector__label">variables 差异（按执行顺序）</h4>
              <div v-for="diff in promptDiffs" :key="`${diff.fromIndex}-${diff.toIndex}`">
                <p class="span-inspector__diff-title">
                  第 {{ diff.fromIndex }} 次（v{{ unknownText(diff.fromVersion) }}）→
                  第 {{ diff.toIndex }} 次（v{{ unknownText(diff.toVersion) }}）
                </p>
                <p v-if="diffEmpty(diff)" class="span-inspector__muted">variables 无变化</p>
                <ul v-else class="span-inspector__diff-list">
                  <li v-if="diff.added.length > 0">新增 key：{{ diff.added.join('、') }}</li>
                  <li v-if="diff.removed.length > 0">删除 key：{{ diff.removed.join('、') }}</li>
                  <li v-if="diff.changed.length > 0">值变化 key：{{ diff.changed.join('、') }}</li>
                </ul>
              </div>
            </section>
          </template>
        </el-tab-pane>

        <el-tab-pane label="事件" name="events" lazy>
          <p v-if="sortedEvents.length === 0" class="span-inspector__muted" data-test="no-events">
            该 Trace 暂无事件
          </p>
          <table v-else class="span-inspector__events" data-test="event-list">
            <thead>
              <tr>
                <th>序号</th>
                <th>事件类型</th>
                <th>span_id</th>
                <th>时间</th>
                <th>event_data</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="event in sortedEvents"
                :key="event.id"
                :class="{ 'span-inspector__event--current': isCurrentEvent(event) }"
                data-test="event-row"
              >
                <td>{{ event.sequence_number }}</td>
                <td>{{ event.event_type }}</td>
                <td class="span-inspector__mono">{{ unknownText(event.span_id) }}</td>
                <td>{{ unknownText(event.created_at) }}</td>
                <td>
                  <JsonViewer :value="event.event_data" />
                </td>
              </tr>
            </tbody>
          </table>
        </el-tab-pane>

        <el-tab-pane label="错误" name="error" lazy>
          <section data-test="span-error-tab">
            <h4 class="span-inspector__label">span.error</h4>
            <p v-if="!span.error" class="span-inspector__muted" data-test="no-span-error">
              无错误
            </p>
            <JsonViewer v-else :value="span.error" />

            <h4 class="span-inspector__label">调用失败历史（error 非空）</h4>
            <p
              v-if="failureRows.length === 0"
              class="span-inspector__muted"
              data-test="no-call-failures"
            >
              无失败调用记录
            </p>
            <template v-else>
              <article
                v-for="row in failureRows"
                :key="row.key"
                class="span-inspector__card"
                data-test="failure-row"
              >
                <p class="span-inspector__card-title">
                  {{ row.kind }} · {{ row.label }}
                </p>
                <div class="span-inspector__fields">
                  <span>attempt：{{ row.attempt === null ? '未上报' : row.attempt }}</span>
                </div>
                <JsonViewer :value="row.error" />
              </article>
            </template>
          </section>
        </el-tab-pane>

        <el-tab-pane label="元数据" name="metadata" lazy>
          <h4 class="span-inspector__label">meta</h4>
          <JsonViewer :value="span.meta" :filename="`span-${span.span_id}-meta.json`" />

          <h4 class="span-inspector__label">links</h4>
          <JsonViewer :value="span.links" :filename="`span-${span.span_id}-links.json`" />

          <h4 class="span-inspector__label">完整 Span 原始 JSON</h4>
          <JsonViewer :value="span" :filename="`span-${span.span_id}.json`" />
        </el-tab-pane>
      </el-tabs>
    </template>
  </section>
</template>

<style scoped>
.span-inspector {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 10px;
}

.span-inspector__header {
  padding-bottom: 6px;
  border-bottom: 1px solid #eef1f6;
}

.span-inspector__title-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.span-inspector__title {
  margin: 0;
  overflow: hidden;
  color: #111827;
  font-size: 14px;
  font-weight: 600;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.span-inspector__id {
  margin: 4px 0 0;
  color: #94a3b8;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 11px;
}

.span-inspector__state {
  padding: 16px 4px;
  color: #64748b;
  font-size: 13px;
}

.span-inspector__retry {
  margin-top: 10px;
}

.span-inspector__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 14px;
  color: #64748b;
  font-size: 12px;
}

.span-inspector__tabs {
  min-width: 0;
}

.span-inspector__muted {
  margin: 4px 0;
  color: #94a3b8;
  font-size: 12px;
}

.span-inspector__list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.span-inspector__card {
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #ffffff;
}

.span-inspector__card-title {
  margin: 0 0 6px;
  color: #111827;
  font-size: 13px;
  font-weight: 600;
  word-break: break-word;
}

.span-inspector__fields {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 14px;
  margin-bottom: 6px;
  color: #64748b;
  font-size: 12px;
}

.span-inspector__cost {
  color: #b45309;
  font-weight: 600;
}

.span-inspector__label {
  margin: 10px 0 4px;
  color: #475569;
  font-size: 12px;
  font-weight: 600;
}

.span-inspector__mono {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 11px;
  word-break: break-all;
}

.span-inspector__context {
  margin: 0;
  max-height: 180px;
  overflow: auto;
  padding: 8px 10px;
  border: 1px solid #eef1f6;
  border-radius: 6px;
  background: #f8fafc;
  color: #334155;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
}

.span-inspector__docs,
.span-inspector__events {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
  table-layout: fixed;
}

.span-inspector__docs th,
.span-inspector__docs td,
.span-inspector__events th,
.span-inspector__events td {
  padding: 5px 6px;
  border-bottom: 1px solid #eef1f6;
  color: #475569;
  text-align: left;
  vertical-align: top;
  word-break: break-word;
}

.span-inspector__events th,
.span-inspector__events td {
  padding: 4px;
}

.span-inspector__events td:last-child {
  width: 46%;
}

.span-inspector__event--current {
  background: #eff6ff;
}

.span-inspector__event--current td {
  color: #1d4ed8;
  font-weight: 600;
}

.span-inspector__diff {
  margin-top: 12px;
  padding: 10px 12px;
  border: 1px dashed #cbd5e1;
  border-radius: 8px;
  background: #f8fafc;
}

.span-inspector__diff-title {
  margin: 6px 0 2px;
  color: #475569;
  font-size: 12px;
}

.span-inspector__diff-list {
  margin: 0;
  padding-left: 18px;
  color: #64748b;
  font-size: 12px;
}
</style>
