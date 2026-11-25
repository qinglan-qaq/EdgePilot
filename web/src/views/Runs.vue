<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'

import { ApiError } from '@/api/http'
import { listRuns, type ListRunsParams, type WorkflowRun } from '@/api/runs'
import AsyncState from '@/components/common/AsyncState.vue'
import { formatDateTime } from '@/utils/datetime'
import {
  formatDurationBetween,
  formatTriggerType,
  isTerminalRunStatus,
  runStatusLabel,
  runStatusTagType,
} from '@/utils/status'

const PAGE_SIZE = 20

const STATUS_OPTIONS = [
  { value: '', label: '全部' },
  { value: 'pending', label: runStatusLabel('pending') },
  { value: 'running', label: runStatusLabel('running') },
  { value: 'waiting_approval', label: runStatusLabel('waiting_approval') },
  { value: 'succeeded', label: runStatusLabel('succeeded') },
  { value: 'failed', label: runStatusLabel('failed') },
  { value: 'timeout', label: runStatusLabel('timeout') },
  { value: 'cancelled', label: runStatusLabel('cancelled') },
]

const router = useRouter()

const loading = ref(true)
const error = ref<string | null>(null)
const items = ref<WorkflowRun[]>([])
const total = ref(0)
const page = ref(1)
const statusFilter = ref('')

const empty = computed(() => items.value.length === 0)

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

// 用递增令牌丢弃过期响应，避免快速翻页/筛选时旧请求覆盖新结果。
let loadToken = 0

async function load(targetPage: number = page.value): Promise<void> {
  const token = ++loadToken
  loading.value = true
  error.value = null

  try {
    const params: ListRunsParams = { page: targetPage, size: PAGE_SIZE }
    // 未选择筛选时不携带 status，避免把 undefined 序列化进请求。
    if (statusFilter.value) {
      params.status = statusFilter.value
    }

    const result = await listRuns(params)
    if (token !== loadToken) {
      return
    }
    items.value = result.items
    total.value = result.total
    page.value = targetPage
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

function handleStatusChange(): void {
  // 筛选条件变化后回到第一页，避免落在超出结果集的页码。
  void load(1)
}

function handlePageChange(nextPage: number): void {
  void load(nextPage)
}

function openDetail(id: number): void {
  void router.push({ name: 'run-detail', params: { id } })
}

function handleRowClick(row: WorkflowRun): void {
  openDetail(row.id)
}

onMounted(() => {
  void load(1)
})
</script>

<template>
  <section class="runs-page">
    <el-card shadow="never" class="runs-page__card">
      <header class="runs-page__toolbar">
        <div>
          <h2 class="runs-page__title">运行追踪</h2>
          <p class="runs-page__subtitle">
            查看真实运行记录与节点执行情况；运行中的详情页每 3 秒自动刷新。
          </p>
        </div>

        <div class="runs-page__actions">
          <el-select
            v-model="statusFilter"
            class="runs-page__filter"
            data-test="run-status-filter"
            placeholder="状态筛选"
            @change="handleStatusChange"
          >
            <el-option
              v-for="option in STATUS_OPTIONS"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
          <el-button data-test="runs-refresh" :loading="loading" @click="load()">
            刷新
          </el-button>
        </div>
      </header>

      <AsyncState :loading="loading" :error="error" :empty="empty" @retry="load()">
        <template #empty>
          <el-empty description="暂无运行记录">
            <p class="runs-page__empty-hint">
              {{
                statusFilter
                  ? '当前筛选条件下暂无运行记录，可切换状态后重试。'
                  : '还没有任何运行记录；可在工作流编辑器中试跑创建运行。'
              }}
            </p>
          </el-empty>
        </template>

        <el-table
          :data="items"
          row-key="id"
          class="runs-page__table"
          @row-click="handleRowClick"
        >
          <el-table-column prop="id" label="运行 ID" width="100" />

          <el-table-column label="工作流名称" min-width="180">
            <template #default="{ row }">
              <span class="runs-page__name">{{ row.workflow_name }}</span>
            </template>
          </el-table-column>

          <el-table-column label="版本" width="80">
            <template #default="{ row }">
              <span>{{ row.workflow_version > 0 ? `v${row.workflow_version}` : '—' }}</span>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag :type="runStatusTagType(row.status)" effect="light">
                {{ runStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="触发方式" width="100">
            <template #default="{ row }">
              {{ formatTriggerType(row.trigger_type) }}
            </template>
          </el-table-column>

          <el-table-column label="创建时间" width="160">
            <template #default="{ row }">
              <span class="runs-page__muted">{{ formatDateTime(row.created_at) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="运行耗时" width="120">
            <template #default="{ row }">
              <span class="runs-page__muted">
                {{
                  formatDurationBetween(row.started_at, row.finished_at, {
                    live: !isTerminalRunStatus(row.status),
                  })
                }}
              </span>
            </template>
          </el-table-column>

          <el-table-column label="Trace" width="100">
            <template #default="{ row }">
              <span v-if="row.trace_id" class="runs-page__trace" :title="row.trace_id">
                已采集
              </span>
              <span v-else class="runs-page__muted">未采集</span>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="90" align="right">
            <template #default="{ row }">
              <el-button link type="primary" @click.stop="openDetail(row.id)">详情</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="total > 0" class="runs-page__pagination">
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
    </el-card>
  </section>
</template>

<style scoped>
.runs-page {
  width: 100%;
}

.runs-page__card {
  border-radius: 12px;
}

.runs-page__toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 16px;
  border-bottom: 1px solid #eef1f6;
}

.runs-page__title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #111827;
}

.runs-page__subtitle {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
}

.runs-page__actions {
  display: flex;
  align-items: center;
  gap: 10px;
}

.runs-page__filter {
  width: 140px;
}

.runs-page__table {
  margin-top: 8px;
}

.runs-page :deep(.el-table__row) {
  cursor: pointer;
}

.runs-page__name {
  font-weight: 500;
}

.runs-page__muted {
  color: #94a3b8;
  font-size: 13px;
}

.runs-page__trace {
  color: #2563eb;
  font-size: 13px;
  font-weight: 500;
}

.runs-page__pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
}

.runs-page__empty-hint {
  margin: 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.7;
}
</style>
