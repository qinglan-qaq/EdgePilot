<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessageBox } from 'element-plus'

import { ApiError } from '@/api/http'
import {
  approveRun,
  cancelRun,
  getRun,
  rejectRun,
  type WorkflowRunDetail,
} from '@/api/runs'
import AsyncState from '@/components/common/AsyncState.vue'
import MarkdownBlock from '@/components/common/MarkdownBlock.vue'
import EvidenceDrawer from '@/components/runs/EvidenceDrawer.vue'
import RunApprovalCard from '@/components/runs/RunApprovalCard.vue'
import RunNodeDetail from '@/components/runs/RunNodeDetail.vue'
import RunNodeList from '@/components/runs/RunNodeList.vue'
import TraceDrawer from '@/components/runs/TraceDrawer.vue'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime } from '@/utils/datetime'
import { collectEvidence, extractRunReport, formatJson } from '@/utils/report'
import {
  formatDurationBetween,
  formatTriggerType,
  isTerminalRunStatus,
  runStatusLabel,
  runStatusTagType,
} from '@/utils/status'

/** 轮询间隔：后端没有运行级 WebSocket，这里明确使用轮询而非实时推送。 */
const POLL_INTERVAL = 3000

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

/** 从路由参数读取正整数运行 id；非法时返回 null。 */
function readRunId(): number | null {
  const raw = route.params.id
  const value = Array.isArray(raw) ? raw[0] : raw
  if (typeof value !== 'string' || !/^\d+$/.test(value)) {
    return null
  }
  const parsed = Number(value)
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null
}

const runId = computed(() => readRunId())
const invalidId = computed(() => runId.value === null)

const loading = ref(true)
const loadError = ref<string | null>(null)
const run = ref<WorkflowRunDetail | null>(null)
const pollWarning = ref<string | null>(null)
const actionError = ref<string | null>(null)
const acting = ref(false)

const selectedNodeId = ref<string | null>(null)
const traceVisible = ref(false)
const traceFocusSpanId = ref<string | null>(null)
const evidenceVisible = ref(false)

let loadToken = 0
let pollTimer: ReturnType<typeof setInterval> | null = null
let pollInFlight = false
/** 页面数据版本号：任何较新的加载/操作结果都会让在途轮询失效。 */
let stateVersion = 0

const nodes = computed(() => run.value?.nodes ?? [])
const terminal = computed(() =>
  run.value ? isTerminalRunStatus(run.value.status) : true,
)
const waitingNodes = computed(() =>
  nodes.value.filter((node) => node.status === 'waiting_approval'),
)
const selectedNode = computed(
  () => nodes.value.find((node) => node.node_id === selectedNodeId.value) ?? null,
)
const canWrite = computed(() => auth.role === 'operator' || auth.role === 'admin')
/** 取消仅 operator / admin，且运行必须非终态。 */
const canCancel = computed(() => canWrite.value && !terminal.value)
const durationLabel = computed(() =>
  formatDurationBetween(run.value?.started_at ?? null, run.value?.finished_at ?? null, {
    live: !terminal.value,
  }),
)
const report = computed(() => extractRunReport(run.value?.output))
/**
 * 从运行输出与所有节点输出递归收集证据，按 type:ref 去重保序。
 * 这里只读取接口真实返回值，不生成任何占位证据。
 */
const evidence = computed(() =>
  collectEvidence(
    run.value?.output,
    ...nodes.value.map((node) => node.output),
  ),
)

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

function stopPolling(): void {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

/** 仅非终态运行才启动轮询；已有定时器则保持原节流节奏。 */
function syncPolling(): void {
  if (!run.value || isTerminalRunStatus(run.value.status)) {
    stopPolling()
    return
  }
  if (pollTimer !== null) {
    return
  }
  pollTimer = setInterval(() => {
    void poll()
  }, POLL_INTERVAL)
}

/** 用接口返回的真实详情整体替换页面数据。 */
function applyRun(detail: WorkflowRunDetail): void {
  stateVersion += 1
  run.value = detail
  loadError.value = null
  pollWarning.value = null
  if (
    selectedNodeId.value !== null &&
    !(detail.nodes ?? []).some((node) => node.node_id === selectedNodeId.value)
  ) {
    selectedNodeId.value = null
  }
  syncPolling()
}

async function load(): Promise<void> {
  const id = runId.value
  if (id === null) {
    stateVersion += 1
    loading.value = false
    return
  }
  const token = ++loadToken
  loading.value = true
  loadError.value = null

  try {
    const detail = await getRun(id)
    if (token !== loadToken) {
      return
    }
    applyRun(detail)
  } catch (cause) {
    if (token !== loadToken) {
      return
    }
    stateVersion += 1
    run.value = null
    loadError.value = errorToMessage(cause)
    stopPolling()
  } finally {
    if (token === loadToken) {
      loading.value = false
    }
  }
}

/** 轮询失败时保留旧数据，只显示轻量告警并继续下一轮。 */
async function poll(): Promise<void> {
  const id = runId.value
  if (id === null || pollInFlight) {
    return
  }
  if (run.value && isTerminalRunStatus(run.value.status)) {
    stopPolling()
    return
  }
  const version = stateVersion
  pollInFlight = true
  try {
    const detail = await getRun(id)
    // 期间发生过加载、审批/取消或路由切换时，丢弃这次过期轮询。
    if (version !== stateVersion) {
      return
    }
    applyRun(detail)
  } catch (cause) {
    if (version === stateVersion) {
      pollWarning.value = `自动刷新失败：${errorToMessage(cause)}。已保留上次数据，稍后继续重试。`
    }
  } finally {
    pollInFlight = false
  }
}

async function handleApprove(comment: string): Promise<void> {
  if (!run.value || acting.value) {
    return
  }
  acting.value = true
  actionError.value = null
  try {
    const detail = await approveRun(run.value.id, comment)
    applyRun(detail)
  } catch (cause) {
    actionError.value = errorToMessage(cause)
  } finally {
    acting.value = false
  }
}

async function handleReject(comment: string): Promise<void> {
  if (!run.value || acting.value) {
    return
  }
  acting.value = true
  actionError.value = null
  try {
    const detail = await rejectRun(run.value.id, comment)
    applyRun(detail)
  } catch (cause) {
    actionError.value = errorToMessage(cause)
  } finally {
    acting.value = false
  }
}

async function handleCancel(): Promise<void> {
  if (!run.value || !canCancel.value || acting.value) {
    return
  }
  try {
    await ElMessageBox.confirm(
      '取消后该运行将标记为已取消，且无法恢复。',
      '取消运行',
      {
        confirmButtonText: '确认取消',
        cancelButtonText: '返回',
        type: 'warning',
      },
    )
  } catch {
    return
  }

  acting.value = true
  actionError.value = null
  try {
    const detail = await cancelRun(run.value.id)
    applyRun(detail)
  } catch (cause) {
    actionError.value = errorToMessage(cause)
  } finally {
    acting.value = false
  }
}

function handleSelectNode(nodeId: string): void {
  selectedNodeId.value = nodeId
}

function openTrace(): void {
  traceFocusSpanId.value = null
  traceVisible.value = true
}

function openTraceAt(spanId: string): void {
  traceFocusSpanId.value = spanId
  traceVisible.value = true
}

function goBack(): void {
  void router.push({ name: 'runs' })
}

function openWorkflow(): void {
  if (!run.value) {
    return
  }
  void router.push({ name: 'workflow-editor', params: { id: run.value.workflow_id } })
}

// 路由参数变化时重新加载（同一组件被复用时不会重新挂载）。
watch(runId, (id) => {
  if (id === null) {
    stateVersion += 1
    loading.value = false
    stopPolling()
    return
  }
  selectedNodeId.value = null
  void load()
})

onMounted(() => {
  if (runId.value === null) {
    loading.value = false
    return
  }
  void load()
})

onBeforeUnmount(() => {
  stopPolling()
  // 使仍在途的加载与轮询响应失效
  loadToken += 1
  stateVersion += 1
})
</script>

<template>
  <section class="run-detail-page">
    <el-card
      v-if="invalidId"
      shadow="never"
      class="run-detail-page__card"
      data-test="invalid-run-id"
    >
      <el-result icon="error" title="无效的运行 ID" sub-title="路由参数必须是正整数。">
        <template #extra>
          <el-button type="primary" @click="goBack">返回运行列表</el-button>
        </template>
      </el-result>
    </el-card>

    <el-card v-else shadow="never" class="run-detail-page__card">
      <AsyncState :loading="loading" :error="loadError" :empty="false" @retry="load()">
        <div v-if="run" class="run-detail" data-test="run-detail">
          <header class="run-detail__header">
            <div class="run-detail__heading">
              <el-button link type="primary" data-test="back-to-runs" @click="goBack">
                返回列表
              </el-button>
              <el-link
                type="primary"
                underline="never"
                class="run-detail__workflow"
                data-test="run-workflow-link"
                @click="openWorkflow"
              >
                {{ run.workflow_name }}
              </el-link>
              <el-tag
                :type="runStatusTagType(run.status)"
                effect="light"
                data-test="run-status"
              >
                {{ runStatusLabel(run.status) }}
              </el-tag>
              <span class="run-detail__meta">运行 #{{ run.id }}</span>
              <span class="run-detail__meta">版本 v{{ run.workflow_version }}</span>
              <span class="run-detail__meta">
                触发方式：{{ formatTriggerType(run.trigger_type) }}
              </span>
              <span class="run-detail__meta" data-test="run-duration">
                耗时：{{ durationLabel }}
              </span>
            </div>

            <div class="run-detail__actions">
              <el-button data-test="refresh-run" :loading="loading" @click="load()">
                刷新
              </el-button>
              <el-button
                v-if="canCancel"
                type="danger"
                plain
                data-test="cancel-run"
                :loading="acting"
                @click="handleCancel"
              >
                取消运行
              </el-button>
            </div>
          </header>

          <div class="run-detail__meta-grid">
            <span>创建时间：{{ formatDateTime(run.created_at) }}</span>
            <span>开始时间：{{ formatDateTime(run.started_at) }}</span>
            <span>结束时间：{{ formatDateTime(run.finished_at) }}</span>
            <span v-if="run.trace_id">
              Trace：
              <el-button
                link
                type="primary"
                data-test="open-trace"
                @click="openTrace"
              >
                {{ run.trace_id }}
              </el-button>
            </span>
            <span v-else data-test="trace-missing">Trace：未采集</span>
          </div>

          <el-alert
            v-if="!terminal"
            class="run-detail__alert"
            type="info"
            :closable="false"
            show-icon
            data-test="run-auto-refresh"
            title="每 3 秒自动刷新（轮询，非实时推送）"
          />
          <el-alert
            v-if="run.status === 'pending'"
            class="run-detail__alert"
            type="info"
            :closable="false"
            show-icon
            data-test="run-pending-hint"
            title="已创建，等待 worker 领取；尚未采集到执行数据。"
          />
          <el-alert
            v-if="run.error"
            class="run-detail__alert"
            type="error"
            :closable="false"
            show-icon
            data-test="run-error"
            :title="run.error"
          />
          <el-alert
            v-if="pollWarning"
            class="run-detail__alert"
            type="warning"
            show-icon
            data-test="poll-warning"
            :title="pollWarning"
            @close="pollWarning = null"
          />
          <el-alert
            v-if="actionError"
            class="run-detail__alert"
            type="error"
            show-icon
            data-test="action-error"
            :title="actionError"
            @close="actionError = null"
          />

          <RunApprovalCard
            v-for="node in waitingNodes"
            :key="node.id"
            :node="node"
            :role="auth.role"
            :submitting="acting"
            @approve="handleApprove"
            @reject="handleReject"
          />

          <section class="run-detail__section">
            <header class="run-detail__section-header">
              <h3>节点时间线</h3>
              <span class="run-detail__muted">按接口返回顺序展示；点击节点查看详情</span>
            </header>
            <RunNodeList
              :nodes="nodes"
              :selected-node-id="selectedNodeId"
              @select="handleSelectNode"
            />
            <RunNodeDetail
              v-if="selectedNode"
              :node="selectedNode"
              @open-trace="openTraceAt"
            />
          </section>

          <section class="run-detail__section">
            <header class="run-detail__section-header">
              <h3>运行输出</h3>
              <el-button
                v-if="evidence.length > 0"
                type="primary"
                plain
                size="small"
                data-test="open-evidence"
                @click="evidenceVisible = true"
              >
                查看证据 ({{ evidence.length }})
              </el-button>
            </header>

            <template v-if="report">
              <div class="run-detail__report">
                <h4 v-if="report.title" class="run-detail__report-title">
                  {{ report.title }}
                </h4>
                <p class="run-detail__muted" data-test="finding-count">
                  发现
                  {{ report.findingCount === null ? '未上报' : report.findingCount }}
                  项
                </p>
                <MarkdownBlock :content="report.markdown" />
                <ul
                  v-if="report.highlights.length > 0"
                  class="run-detail__highlights"
                  data-test="report-highlights"
                >
                  <li v-for="(item, index) in report.highlights" :key="index">
                    {{ item.statement }}
                    <span v-if="item.evidence.length > 0" class="run-detail__muted">
                      （证据：<template
                        v-for="(ref, refIndex) in item.evidence"
                        :key="`${ref.type}:${ref.ref}`"
                        >{{ ref.type }} {{ ref.ref
                        }}<template v-if="refIndex < item.evidence.length - 1">、</template></template
                      >）
                    </span>
                  </li>
                </ul>
              </div>
            </template>
            <pre
              v-else-if="run.output !== null && run.output !== undefined"
              class="run-detail__json"
              data-test="run-output-json"
            >{{ formatJson(run.output) }}</pre>
            <p v-else class="run-detail__muted" data-test="run-output-empty">
              本次运行未产生输出。
            </p>
          </section>

          <TraceDrawer
            v-model="traceVisible"
            :trace-id="run.trace_id"
            :focus-span-id="traceFocusSpanId"
          />
          <EvidenceDrawer v-model="evidenceVisible" :items="evidence" />
        </div>
      </AsyncState>
    </el-card>
  </section>
</template>

<style scoped>
.run-detail-page {
  width: 100%;
}

.run-detail-page__card {
  border-radius: 12px;
}

.run-detail {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.run-detail__header {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding-bottom: 12px;
  border-bottom: 1px solid #eef1f6;
}

.run-detail__heading {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.run-detail__workflow {
  font-size: 18px;
  font-weight: 600;
}

.run-detail__meta {
  color: #6b7280;
  font-size: 12px;
}

.run-detail__actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.run-detail__meta-grid {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 20px;
  color: #6b7280;
  font-size: 12px;
}

.run-detail__alert {
  margin: 0;
}

.run-detail__section {
  padding-top: 6px;
}

.run-detail__section-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
}

.run-detail__section-header h3 {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}

.run-detail__muted {
  color: #94a3b8;
  font-size: 12px;
}

.run-detail__report {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.run-detail__report-title {
  margin: 0;
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}

.run-detail__highlights {
  margin: 4px 0 0;
  padding-left: 18px;
  color: #334155;
  font-size: 13px;
  line-height: 1.8;
}

.run-detail__json {
  margin: 0;
  max-height: 320px;
  overflow: auto;
  padding: 12px 14px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #f8fafc;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}
</style>
