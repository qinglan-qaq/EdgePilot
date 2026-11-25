<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { ApiError } from '@/api/http'
import {
  getSpan,
  getTrace,
  getTraceTree,
  type LLMCallRecord,
  type SpanDetail,
  type SpanNode,
  type TraceDetail,
} from '@/api/observability'
import AsyncState from '@/components/common/AsyncState.vue'
import TraceSpanTree from '@/components/runs/TraceSpanTree.vue'
import { formatJson } from '@/utils/report'
import { formatCost, formatDurationMs, formatTokens } from '@/utils/status'

const props = withDefaults(
  defineProps<{
    modelValue: boolean
    traceId: string | null
    focusSpanId?: string | null
  }>(),
  {
    focusSpanId: null,
  },
)

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const loading = ref(false)
const error = ref<string | null>(null)
const trace = ref<TraceDetail | null>(null)
const tree = ref<SpanNode[]>([])

const selectedSpanId = ref<string | null>(null)
const spanLoading = ref(false)
const spanError = ref<string | null>(null)
const spanDetail = ref<SpanDetail | null>(null)

const llmCalls = computed(() => spanDetail.value?.llm_calls ?? [])
const toolCalls = computed(() => spanDetail.value?.tool_calls ?? [])
const retrievals = computed(
  () => spanDetail.value?.retrievals ?? spanDetail.value?.retrieval_records ?? [],
)
const hasRelatedCalls = computed(
  () => llmCalls.value.length + toolCalls.value.length + retrievals.value.length > 0,
)

let loadToken = 0
let spanToken = 0

/** 只展示后端错误信息，不泄露堆栈。 */
function errorToMessage(value: unknown): string {
  if (value instanceof ApiError) {
    return value.message
  }
  if (value instanceof Error && value.message) {
    return value.message
  }
  return '请求失败，请稍后重试'
}

function reset(): void {
  loadToken += 1
  spanToken += 1
  loading.value = false
  error.value = null
  trace.value = null
  tree.value = []
  selectedSpanId.value = null
  spanLoading.value = false
  spanError.value = null
  spanDetail.value = null
}

async function load(): Promise<void> {
  const traceId = props.traceId
  if (!traceId) {
    reset()
    return
  }
  const token = ++loadToken
  loading.value = true
  error.value = null
  spanDetail.value = null
  spanError.value = null
  selectedSpanId.value = null
  try {
    const [detail, spanTree] = await Promise.all([
      getTrace(traceId),
      getTraceTree(traceId),
    ])
    if (token !== loadToken) {
      return
    }
    trace.value = detail
    tree.value = spanTree
    const focus = props.focusSpanId
    if (focus) {
      await selectSpan(focus)
    }
  } catch (cause) {
    if (token !== loadToken) {
      return
    }
    trace.value = null
    tree.value = []
    error.value = errorToMessage(cause)
  } finally {
    if (token === loadToken) {
      loading.value = false
    }
  }
}

async function selectSpan(spanId: string): Promise<void> {
  const token = ++spanToken
  selectedSpanId.value = spanId
  spanLoading.value = true
  spanError.value = null
  spanDetail.value = null
  try {
    const detail = await getSpan(spanId)
    if (token !== spanToken) {
      return
    }
    spanDetail.value = detail
  } catch (cause) {
    if (token !== spanToken) {
      return
    }
    spanError.value = errorToMessage(cause)
  } finally {
    if (token === spanToken) {
      spanLoading.value = false
    }
  }
}

/** cost_available 为 false 表示价格缺失，绝不估算。 */
function llmCostLabel(call: LLMCallRecord): string {
  if (call.cost_available === false || call.cost_available === null) {
    return '费用未上报'
  }
  return formatCost(call.cost ?? null)
}

watch(
  () => [props.modelValue, props.traceId] as const,
  ([open]) => {
    if (open) {
      void load()
    } else {
      reset()
    }
  },
  { immediate: true },
)

watch(
  () => props.focusSpanId,
  (spanId) => {
    // Trace 尚未加载完成时由 load() 统一聚焦，避免重复请求。
    if (props.modelValue && spanId && trace.value) {
      void selectSpan(spanId)
    }
  },
)
</script>

<template>
  <el-drawer v-model="visible" title="Trace 详情" size="62%" data-test="trace-drawer">
    <div class="trace-drawer">
      <el-alert
        v-if="!traceId"
        type="info"
        :closable="false"
        show-icon
        title="本次运行尚未采集到 Trace。"
        data-test="trace-drawer-missing"
      />

      <AsyncState v-else :loading="loading" :error="error" :empty="false" @retry="load()">
        <template v-if="trace">
          <div data-test="trace-summary">
            <el-descriptions :column="2" border size="small">
              <el-descriptions-item label="名称">{{ trace.name }}</el-descriptions-item>
              <el-descriptions-item label="状态">{{ trace.status }}</el-descriptions-item>
              <el-descriptions-item label="来源">{{ trace.source }}</el-descriptions-item>
              <el-descriptions-item label="Trace ID">
                <span class="trace-drawer__mono">{{ trace.trace_id }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="耗时">
                {{ formatDurationMs(trace.duration_ms) }}
              </el-descriptions-item>
              <el-descriptions-item label="开始时间">
                {{ trace.start_time ?? '未上报' }}
              </el-descriptions-item>
              <el-descriptions-item label="Span 数">{{ trace.total_spans }}</el-descriptions-item>
              <el-descriptions-item label="错误数">{{ trace.error_count }}</el-descriptions-item>
              <el-descriptions-item label="总 Token">
                <span data-test="trace-total-tokens">{{ formatTokens(trace.total_tokens) }}</span>
              </el-descriptions-item>
              <el-descriptions-item label="总费用">
                <span data-test="trace-total-cost">{{ formatCost(trace.total_cost) }}</span>
                <span v-if="trace.total_cost === null" class="trace-drawer__note">
                  （未上报，不估算）
                </span>
              </el-descriptions-item>
              <el-descriptions-item label="费用不可用调用">
                <span data-test="trace-cost-unavailable">
                  {{ trace.cost_unavailable_count ?? '未上报' }}
                </span>
              </el-descriptions-item>
            </el-descriptions>
          </div>

          <h3 class="trace-drawer__section-title">Span 树</h3>
          <TraceSpanTree
            v-if="tree.length > 0"
            :nodes="tree"
            :selected-span-id="selectedSpanId"
            @select="selectSpan"
          />
          <p v-else class="trace-drawer__muted">该 Trace 暂无 Span 数据。</p>

          <section v-if="selectedSpanId" class="trace-drawer__span" data-test="span-detail">
            <h3 class="trace-drawer__section-title">
              Span 详情
              <span class="trace-drawer__mono">{{ selectedSpanId }}</span>
            </h3>

            <div v-if="spanLoading" class="trace-drawer__muted">Span 详情加载中…</div>

            <template v-else-if="spanError">
              <el-alert
                :title="spanError"
                type="error"
                :closable="false"
                show-icon
                data-test="span-error"
              />
              <el-button class="trace-drawer__retry" @click="selectSpan(selectedSpanId)">
                重试
              </el-button>
            </template>

            <template v-else-if="spanDetail">
              <div class="trace-drawer__span-meta">
                <span>类型：{{ spanDetail.span_type }}</span>
                <span>状态：{{ spanDetail.status }}</span>
                <span>耗时：{{ formatDurationMs(spanDetail.duration_ms) }}</span>
                <span>node_id：{{ spanDetail.node_id ?? '未上报' }}</span>
                <span>tool_id：{{ spanDetail.tool_id ?? '未上报' }}</span>
                <span>重试：{{ spanDetail.retry_count ?? 0 }} 次</span>
              </div>

              <h4 class="trace-drawer__sub-title">输入</h4>
              <pre class="trace-drawer__json">{{ formatJson(spanDetail.input) }}</pre>

              <h4 class="trace-drawer__sub-title">输出</h4>
              <pre class="trace-drawer__json">{{ formatJson(spanDetail.output) }}</pre>

              <template v-if="spanDetail.error">
                <h4 class="trace-drawer__sub-title">错误</h4>
                <pre class="trace-drawer__json trace-drawer__json--error">{{
                  formatJson(spanDetail.error)
                }}</pre>
              </template>

              <template v-if="hasRelatedCalls">
                <h4 class="trace-drawer__sub-title">LLM 调用（{{ llmCalls.length }}）</h4>
                <ul v-if="llmCalls.length > 0" class="trace-drawer__calls">
                  <li v-for="(call, index) in llmCalls" :key="call.id ?? index">
                    <span class="trace-drawer__call-name">
                      {{ call.model_name ?? '模型未上报' }}
                    </span>
                    <span>provider：{{ call.provider ?? '未上报' }}</span>
                    <span>token：{{ formatTokens(call.total_tokens ?? null) }}</span>
                    <span data-test="llm-cost">{{ llmCostLabel(call) }}</span>
                  </li>
                </ul>
                <p v-else class="trace-drawer__muted">无 LLM 调用记录。</p>

                <h4 class="trace-drawer__sub-title">工具调用（{{ toolCalls.length }}）</h4>
                <ul v-if="toolCalls.length > 0" class="trace-drawer__calls">
                  <li v-for="(call, index) in toolCalls" :key="call.id ?? index">
                    <span class="trace-drawer__call-name">
                      {{ call.tool_name ?? call.tool_id ?? '工具未上报' }}
                    </span>
                    <span>tool_id：{{ call.tool_id ?? '未上报' }}</span>
                    <span>状态：{{ call.execution_status ?? '未上报' }}</span>
                    <span>耗时：{{ formatDurationMs(call.duration_ms ?? null) }}</span>
                  </li>
                </ul>
                <p v-else class="trace-drawer__muted">无工具调用记录。</p>

                <h4 class="trace-drawer__sub-title">检索记录（{{ retrievals.length }}）</h4>
                <ul v-if="retrievals.length > 0" class="trace-drawer__calls">
                  <li v-for="(item, index) in retrievals" :key="item.id ?? index">
                    <span class="trace-drawer__call-name">
                      {{ item.query ?? '查询未上报' }}
                    </span>
                    <span>策略：{{ item.retrieval_strategy ?? '未上报' }}</span>
                    <span>top_k：{{ item.top_k ?? '未上报' }}</span>
                    <span>耗时：{{ formatDurationMs(item.duration_ms ?? null) }}</span>
                  </li>
                </ul>
                <p v-else class="trace-drawer__muted">无检索记录。</p>
              </template>

              <p v-else class="trace-drawer__muted" data-test="span-no-calls">无调用记录</p>
            </template>
          </section>
        </template>
      </AsyncState>
    </div>
  </el-drawer>
</template>

<style scoped>
.trace-drawer {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.trace-drawer__section-title {
  margin: 16px 0 8px;
  font-size: 14px;
  font-weight: 600;
  color: #111827;
}

.trace-drawer__sub-title {
  margin: 14px 0 6px;
  font-size: 13px;
  font-weight: 600;
  color: #374151;
}

.trace-drawer__mono {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
}

.trace-drawer__note {
  color: #b45309;
  font-size: 12px;
}

.trace-drawer__muted {
  margin: 4px 0;
  color: #94a3b8;
  font-size: 12px;
}

.trace-drawer__span {
  margin-top: 12px;
  padding-top: 8px;
  border-top: 1px solid #eef1f6;
}

.trace-drawer__span-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  color: #6b7280;
  font-size: 12px;
}

.trace-drawer__json {
  margin: 0;
  max-height: 220px;
  overflow: auto;
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #f8fafc;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.trace-drawer__json--error {
  border-color: #fecaca;
  background: #fef2f2;
  color: #b91c1c;
}

.trace-drawer__calls {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.trace-drawer__calls li {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 16px;
  padding: 8px 10px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  color: #64748b;
  font-size: 12px;
}

.trace-drawer__call-name {
  font-weight: 600;
  color: #111827;
}

.trace-drawer__retry {
  margin-top: 10px;
}
</style>
