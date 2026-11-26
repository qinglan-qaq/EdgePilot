<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'

import { ApiError } from '@/api/http'
import { visionApi } from '@/api/vision'
import AsyncState from '@/components/common/AsyncState.vue'
import VisionPlayer from '@/components/vision/VisionPlayer.vue'
import { useAuthStore } from '@/stores/auth'
import { useVisionStore } from '@/stores/vision'
import { formatDateTime } from '@/utils/datetime'
import { formatDurationMs } from '@/utils/status'

const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled']

const STATUS_LABELS: Record<string, string> = {
  pending: '排队中',
  running: '识别中',
  completed: '已完成',
  failed: '失败',
  cancelled: '已取消',
}

type StatusTagType = 'success' | 'info' | 'warning' | 'danger' | undefined

const STATUS_TAG_TYPES: Record<string, StatusTagType> = {
  pending: 'info',
  running: 'warning',
  completed: 'success',
  failed: 'danger',
  cancelled: 'info',
}

function errorToMessage(value: unknown): string {
  if (value instanceof ApiError) {
    return value.message
  }
  if (value instanceof Error && value.message) {
    return value.message
  }
  return '操作失败，请稍后重试'
}

const route = useRoute()
const auth = useAuthStore()
const store = useVisionStore()

const uid = computed(() => String(route.params.taskUid ?? ''))
const task = computed(() => store.task)
const result = computed(() => store.task?.result ?? null)
const classCounts = computed(() => result.value?.class_counts ?? {})
const acting = ref(false)
const actionError = ref<string | null>(null)

const canWrite = computed(
  () => auth.role === 'admin' || auth.role === 'operator',
)

const isTerminal = computed(
  () => TERMINAL_STATUSES.includes(task.value?.status ?? ''),
)

/** 失败/取消的任务允许重试；其他状态重试按钮没有意义。 */
const canRetry = computed(
  () => task.value?.status === 'failed' || task.value?.status === 'cancelled',
)

/** 已完成或有结果摘要的任务可发起研判（后端会复用/创建 Agent 运行）。 */
const canAnalyze = computed(
  () => task.value?.status === 'completed' || task.value?.status === 'failed',
)

/**
 * 结果不可用的明确提示：
 * 1. 优先展示后端 message（例如 404 artifact_missing 的“结果文件已清理”）；
 * 2. 任务被清理时即使没有请求结果也要提示；
 * 3. 视频缺失但结果仍在时只提示无法回放。
 */
const artifactNotice = computed(() => {
  if (store.resultsError) {
    return store.resultsError
  }
  if (task.value?.artifact_purged) {
    return '结果文件已清理，无法回放帧级结果'
  }
  if (task.value?.video_missing && !task.value?.has_video) {
    return '视频文件不存在或已被清理，无法回放'
  }
  return null
})

/** 只有视频可播放且帧结果加载成功才挂载播放器，避免出现空画布假框。 */
const playerVisible = computed(
  () =>
    Boolean(task.value?.has_video) &&
    Boolean(task.value?.video_url) &&
    store.results !== null &&
    !store.resultsError,
)

function statusLabel(value: string): string {
  return STATUS_LABELS[value] ?? value
}

function statusTagType(value: string): StatusTagType {
  return STATUS_TAG_TYPES[value] ?? 'info'
}

function formatConfidence(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return '—'
  }
  return `${(value * 100).toFixed(1)}%`
}

function formatMetric(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : String(value)
}

function load(): void {
  if (!uid.value) {
    return
  }
  actionError.value = null
  store.startPolling(uid.value)
  void store.loadEvents(uid.value)
}

async function runAction(action: () => Promise<unknown>): Promise<void> {
  if (acting.value) {
    return
  }
  acting.value = true
  actionError.value = null
  try {
    await action()
    // 写操作成功后重新拉取详情与事件，展示后端最新状态。
    store.startPolling(uid.value)
    void store.loadEvents(uid.value)
  } catch (cause) {
    actionError.value = errorToMessage(cause)
  } finally {
    acting.value = false
  }
}

function handleCancel(): void {
  void runAction(() => visionApi.cancel(uid.value))
}

function handleRetry(): void {
  void runAction(() => visionApi.retry(uid.value))
}

function handleAnalyze(): void {
  void runAction(() => visionApi.analyze(uid.value))
}

onMounted(load)

watch(uid, (next, previous) => {
  if (next && next !== previous) {
    load()
  }
})

onBeforeUnmount(() => {
  store.stopPolling()
})
</script>

<template>
  <section class="task-detail">
    <header class="task-detail__header">
      <div>
        <router-link class="task-detail__back" to="/vision/records">
          ← 返回识别记录
        </router-link>
        <h2 class="task-detail__title">
          {{ task?.video_filename ?? '识别任务' }}
        </h2>
        <p class="task-detail__meta">
          任务 UID：<span class="task-detail__mono">{{ uid }}</span>
          <el-tag
            v-if="task"
            class="task-detail__status"
            :type="statusTagType(task.status)"
            size="small"
            effect="light"
          >
            {{ statusLabel(task.status) }}
          </el-tag>
          <span v-if="task" class="task-detail__progress">
            进度 {{ task.progress }}%
          </span>
        </p>
      </div>

      <div v-if="canWrite" class="task-detail__actions">
        <el-button
          v-if="!isTerminal"
          :loading="acting"
          data-test="cancel"
          @click="handleCancel"
        >
          取消
        </el-button>
        <el-button
          v-if="canRetry"
          type="primary"
          plain
          :loading="acting"
          data-test="retry"
          @click="handleRetry"
        >
          重试
        </el-button>
        <el-button
          v-if="canAnalyze"
          type="success"
          plain
          :loading="acting"
          data-test="analyze"
          @click="handleAnalyze"
        >
          研判
        </el-button>
      </div>
    </header>

    <el-alert
      v-if="actionError"
      class="task-detail__alert"
      type="error"
      :title="actionError"
      :closable="false"
      show-icon
    />

    <AsyncState :loading="store.loading" :error="store.error" @retry="load">
      <template v-if="task">
        <el-alert
          v-if="artifactNotice"
          class="task-detail__alert"
          data-test="artifact-notice"
          type="warning"
          :title="artifactNotice"
          :closable="false"
          show-icon
        />

        <div class="task-detail__panels">
          <el-card class="task-detail__panel" shadow="never">
            <template #header>
              <h3 class="task-detail__panel-title">视频与检测框</h3>
            </template>

            <VisionPlayer
              v-if="playerVisible"
              :src="task.video_url ?? ''"
              :frames="store.frames"
              :video-width="task.video_width"
              :video-height="task.video_height"
            />
            <p v-else class="task-detail__placeholder">
              {{ artifactNotice ?? '暂无可播放的视频' }}
            </p>
          </el-card>

          <el-card class="task-detail__panel" shadow="never">
            <template #header>
              <h3 class="task-detail__panel-title">识别汇总</h3>
            </template>

            <template v-if="result">
              <el-descriptions :column="1" size="small">
                <el-descriptions-item label="处理帧数">
                  {{ formatMetric(result.frame_count) }}
                </el-descriptions-item>
                <el-descriptions-item label="检测目标数">
                  {{ result.detection_count }}
                </el-descriptions-item>
                <el-descriptions-item label="峰值置信度">
                  {{ formatConfidence(result.peak_confidence) }}
                </el-descriptions-item>
                <el-descriptions-item label="推理耗时">
                  {{ result.inference_ms === null ? '—' : formatDurationMs(result.inference_ms) }}
                </el-descriptions-item>
                <el-descriptions-item label="联动状态">
                  <span data-test="trigger-status">{{ result.trigger_status }}</span>
                </el-descriptions-item>
                <el-descriptions-item label="Agent 状态">
                  <span data-test="agent-status">{{ result.agent_status }}</span>
                </el-descriptions-item>
                <el-descriptions-item label="Agent 错误">
                  {{ result.agent_error ?? '—' }}
                </el-descriptions-item>
                <el-descriptions-item label="Trace ID">
                  <span class="task-detail__mono">{{ result.agent_trace_id ?? '—' }}</span>
                </el-descriptions-item>
              </el-descriptions>

              <div v-if="Object.keys(classCounts).length > 0" class="task-detail__classes">
                <el-tag
                  v-for="(count, name) in classCounts"
                  :key="name"
                  size="small"
                  effect="plain"
                >
                  {{ name }} {{ count }}
                </el-tag>
              </div>

              <a
                v-if="result.workflow_run_id"
                class="task-detail__run-link"
                :href="`/runs/${result.workflow_run_id}`"
              >
                工作流运行 #{{ result.workflow_run_id }}
              </a>
            </template>
            <p v-else class="task-detail__placeholder">暂无识别结果</p>
          </el-card>
        </div>

        <el-card class="task-detail__events" shadow="never">
          <template #header>
            <h3 class="task-detail__panel-title">视觉事件</h3>
          </template>

          <el-alert
            v-if="store.eventsError"
            type="warning"
            :title="store.eventsError"
            :closable="false"
            show-icon
          />
          <p v-else-if="store.events.length === 0" class="task-detail__placeholder">
            暂无事件
          </p>
          <ul v-else class="task-detail__event-list">
            <li
              v-for="event in store.events"
              :key="event.id"
              class="task-detail__event"
            >
              <el-tag size="small" effect="light">{{ event.event_type }}</el-tag>
              <span class="task-detail__event-time">
                {{ formatDateTime(event.occurred_at) }}
              </span>
              <span v-if="event.confidence !== null" class="task-detail__event-meta">
                置信度 {{ formatConfidence(event.confidence) }}
              </span>
              <span v-if="event.severity" class="task-detail__event-meta">
                级别 {{ event.severity }}
              </span>
            </li>
          </ul>
        </el-card>

        <el-card class="task-detail__events" shadow="never">
          <template #header>
            <h3 class="task-detail__panel-title">任务信息</h3>
          </template>
          <el-descriptions :column="2" size="small">
            <el-descriptions-item label="模型版本">
              {{ task.model_version ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="运行设备">
              {{ task.device ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="视频时长">
              {{ task.video_duration_ms === null ? '—' : formatDurationMs(task.video_duration_ms) }}
            </el-descriptions-item>
            <el-descriptions-item label="错误码">
              {{ task.error_code ?? '—' }}
            </el-descriptions-item>
            <el-descriptions-item label="创建时间">
              {{ formatDateTime(task.created_at) }}
            </el-descriptions-item>
            <el-descriptions-item label="完成时间">
              {{ formatDateTime(task.finished_at) }}
            </el-descriptions-item>
            <el-descriptions-item v-if="task.error_message" label="错误信息" :span="2">
              <span class="task-detail__error-text">{{ task.error_message }}</span>
            </el-descriptions-item>
          </el-descriptions>
        </el-card>
      </template>
    </AsyncState>
  </section>
</template>

<style scoped>
.task-detail {
  width: 100%;
}

.task-detail__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 12px;
  padding: 18px 20px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.task-detail__back {
  color: #2563eb;
  font-size: 12px;
  text-decoration: none;
}

.task-detail__title {
  margin: 6px 0 0;
  color: #111827;
  font-size: 18px;
  font-weight: 600;
}

.task-detail__meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 12px;
}

.task-detail__mono {
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.task-detail__status {
  margin-left: 2px;
}

.task-detail__progress {
  color: #94a3b8;
}

.task-detail__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.task-detail__alert {
  margin-bottom: 12px;
}

.task-detail__panels {
  display: grid;
  grid-template-columns: minmax(0, 1.6fr) minmax(0, 1fr);
  gap: 16px;
}

.task-detail__panel {
  border-radius: 12px;
}

.task-detail__panel-title {
  margin: 0;
  color: #111827;
  font-size: 15px;
  font-weight: 600;
}

.task-detail__placeholder {
  margin: 0;
  padding: 24px 0;
  color: #94a3b8;
  font-size: 13px;
  text-align: center;
}

.task-detail__classes {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
}

.task-detail__run-link {
  display: inline-block;
  margin-top: 12px;
  color: #2563eb;
  font-size: 13px;
  text-decoration: none;
}

.task-detail__events {
  margin-top: 16px;
  border-radius: 12px;
}

.task-detail__event-list {
  margin: 0;
  padding: 0;
  list-style: none;
}

.task-detail__event {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 8px 0;
  border-bottom: 1px solid #f1f5f9;
  font-size: 13px;
}

.task-detail__event-time,
.task-detail__event-meta {
  color: #94a3b8;
  font-size: 12px;
}

.task-detail__error-text {
  color: #dc2626;
}

@media (max-width: 1100px) {
  .task-detail__panels {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
