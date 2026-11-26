<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRouter } from 'vue-router'

import { ApiError } from '@/api/http'
import { visionApi, type VisionTask, type VisionTaskListParams } from '@/api/vision'
import AsyncState from '@/components/common/AsyncState.vue'
import { formatDateTime } from '@/utils/datetime'
import { formatDurationMs } from '@/utils/status'

const PAGE_SIZE = 20
const SEARCH_DEBOUNCE_MS = 300

const STATUS_OPTIONS = [
  { value: 'pending', label: '排队中' },
  { value: 'running', label: '识别中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

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
  return '请求失败，请稍后重试'
}

const router = useRouter()

const items = ref<VisionTask[]>([])
const total = ref(0)
const page = ref(1)
const status = ref('')
const search = ref('')
const loading = ref(false)
const error = ref<string | null>(null)

let loadToken = 0
let debounceTimer: ReturnType<typeof setTimeout> | null = null

const empty = computed(() => items.value.length === 0)

/** 有筛选条件时区分“无记录”和“筛选无结果”。 */
const emptyDescription = computed(() =>
  status.value || search.value.trim()
    ? '当前筛选条件下暂无识别记录'
    : '暂无识别记录',
)

/** 只把有值的筛选发给后端，避免空 status/q 影响索引与分页。 */
function buildParams(): VisionTaskListParams {
  const params: VisionTaskListParams = { page: page.value, size: PAGE_SIZE }
  if (status.value) {
    params.status = status.value
  }
  const query = search.value.trim()
  if (query) {
    params.q = query
  }
  return params
}

/** 每次请求带单调令牌，旧筛选条件的响应不得覆盖新结果。 */
async function load(): Promise<void> {
  const token = ++loadToken
  loading.value = true
  error.value = null

  try {
    const result = await visionApi.tasks(buildParams())
    if (token !== loadToken) {
      return
    }
    items.value = result.items
    total.value = result.total
  } catch (cause) {
    if (token !== loadToken) {
      return
    }
    items.value = []
    total.value = 0
    error.value = errorToMessage(cause)
  } finally {
    if (token === loadToken) {
      loading.value = false
    }
  }
}

function handleStatusChange(value: string): void {
  status.value = value
  // 筛选条件变更必须回到第一页，避免落在超出新结果集的页码。
  page.value = 1
  void load()
}

function handlePageChange(nextPage: number): void {
  page.value = nextPage
  void load()
}

/** 文件名搜索 300ms 防抖，输入期间不发请求。 */
watch(search, () => {
  if (debounceTimer !== null) {
    clearTimeout(debounceTimer)
  }
  debounceTimer = setTimeout(() => {
    debounceTimer = null
    page.value = 1
    void load()
  }, SEARCH_DEBOUNCE_MS)
})

function statusLabel(value: string): string {
  return STATUS_LABELS[value] ?? value
}

function statusTagType(value: string): StatusTagType {
  return STATUS_TAG_TYPES[value] ?? 'info'
}

/** el-table 的行插槽类型是 DefaultRow（索引签名），这里只声明用到的字段。 */
interface TaskRowLike {
  processed_frames?: number | null
  detection_count?: number
  video_duration_ms?: number | null
}

function framesText(task: TaskRowLike): string {
  if (task.processed_frames === null || task.processed_frames === undefined) {
    return '—'
  }
  return `${task.processed_frames} 帧 / ${task.detection_count ?? 0} 目标`
}

function durationText(task: TaskRowLike): string {
  return task.video_duration_ms === null || task.video_duration_ms === undefined
    ? '—'
    : formatDurationMs(task.video_duration_ms)
}

function openTask(task: VisionTask): void {
  void router.push(`/vision/records/${task.task_uid}`)
}

onMounted(load)

onBeforeUnmount(() => {
  if (debounceTimer !== null) {
    clearTimeout(debounceTimer)
  }
})
</script>

<template>
  <section class="vision-records">
    <header class="vision-records__header">
      <div>
        <h2 class="vision-records__title">识别记录</h2>
        <p class="vision-records__subtitle">
          按创建时间倒序展示识别任务；状态与文件名筛选全部交给后端执行。
        </p>
      </div>
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
    </header>

    <div class="vision-records__filters">
      <el-select
        v-model="status"
        class="vision-records__status"
        placeholder="全部状态"
        @change="handleStatusChange"
      >
        <el-option
          v-for="option in STATUS_OPTIONS"
          :key="option.value"
          :label="option.label"
          :value="option.value"
        />
      </el-select>

      <el-input
        v-model="search"
        class="vision-records__search"
        placeholder="搜索文件名"
        clearable
      />
    </div>

    <AsyncState
      class="vision-records__body"
      :loading="loading"
      :error="error"
      :empty="empty"
      @retry="load"
    >
      <template #empty>
        <el-empty :description="emptyDescription" />
      </template>

      <el-table
        :data="items"
        row-key="task_uid"
        size="small"
        data-test="records-table"
        @row-click="openTask"
      >
        <el-table-column prop="video_filename" label="文件名" min-width="180" />
        <el-table-column label="状态" width="100">
          <template #default="{ row }">
            <el-tag :type="statusTagType(row.status)" size="small" effect="light">
              {{ statusLabel(row.status) }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="进度" width="90">
          <template #default="{ row }">{{ row.progress }}%</template>
        </el-table-column>
        <el-table-column label="时长" width="100">
          <template #default="{ row }">{{ durationText(row) }}</template>
        </el-table-column>
        <el-table-column label="处理帧 / 检测数" min-width="150">
          <template #default="{ row }">{{ framesText(row) }}</template>
        </el-table-column>
        <el-table-column prop="event_count" label="事件数" width="90" />
        <el-table-column label="模型版本" min-width="120">
          <template #default="{ row }">{{ row.model_version ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="创建时间" width="150">
          <template #default="{ row }">
            <span class="vision-records__muted">{{ formatDateTime(row.created_at) }}</span>
          </template>
        </el-table-column>
      </el-table>

      <div v-if="total > 0" class="vision-records__pagination">
        <el-pagination
          :current-page="page"
          :page-size="PAGE_SIZE"
          :total="total"
          layout="total, prev, pager, next"
          background
          @current-change="handlePageChange"
        />
      </div>
    </AsyncState>
  </section>
</template>

<style scoped>
.vision-records {
  width: 100%;
}

.vision-records__header {
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

.vision-records__title {
  margin: 0;
  color: #111827;
  font-size: 18px;
  font-weight: 600;
}

.vision-records__subtitle {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
}

.vision-records__filters {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 12px;
  padding: 12px 16px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.vision-records__status {
  width: 160px;
}

.vision-records__search {
  width: 240px;
}

.vision-records__body {
  padding: 16px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.vision-records__muted {
  color: #94a3b8;
  font-size: 13px;
}

.vision-records__pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
}

.vision-records__body :deep(.el-table__row) {
  cursor: pointer;
}
</style>
