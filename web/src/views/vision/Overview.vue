<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '@/api/http'
import { visionApi, type VisionHealth, type VisionOverview } from '@/api/vision'
import AsyncState from '@/components/common/AsyncState.vue'
import { formatDateTime } from '@/utils/datetime'

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

interface StatCard {
  label: string
  value: string
  hint: string
}

const overview = ref<VisionOverview | null>(null)
const health = ref<VisionHealth | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

let token = 0

/** 并行加载统计与健康状态；健康不可达仍展示统计数据。 */
async function load(): Promise<void> {
  const current = ++token
  loading.value = true
  error.value = null

  try {
    const [stats, healthState] = await Promise.all([
      visionApi.overview(),
      visionApi.health().catch(() => null),
    ])
    if (current !== token) {
      return
    }
    overview.value = stats
    health.value = healthState
  } catch (cause) {
    if (current !== token) {
      return
    }
    overview.value = null
    error.value = errorToMessage(cause)
  } finally {
    if (current === token) {
      loading.value = false
    }
  }
}

onMounted(load)

const stats = computed<StatCard[]>(() => {
  const data = overview.value
  if (!data) {
    return []
  }
  return [
    {
      label: '识别任务',
      value: String(data.tasks.total ?? 0),
      hint: '已完成 / 进行中 / 失败的任务数。',
    },
    {
      label: '已处理帧',
      value: String(data.frames.processed_total ?? 0),
      hint: '所有任务 artifact 汇总的处理帧数。',
    },
    {
      label: '检测目标',
      value: String(data.detections.total ?? 0),
      hint: '全部采样帧中的检测目标总数。',
    },
    {
      label: '视觉事件',
      value: String(data.events.total ?? 0),
      hint: '由识别结果生成的 vision.detection 事件数。',
    },
  ]
})

const modelVersion = computed(
  () =>
    health.value?.model?.model_version ??
    overview.value?.model?.model_version ??
    '未上报',
)

const device = computed(
  () => health.value?.model?.device ?? overview.value?.model?.device ?? '未上报',
)

const license = computed(() => health.value?.model?.license ?? '未上报')

const reachableText = computed(() => {
  if (!health.value) {
    return '状态未上报'
  }
  return health.value.reachable ? '推理服务可达' : '推理服务不可达'
})

const serviceStatus = computed(
  () => String(health.value?.service?.status ?? '未上报'),
)

const recentTasks = computed(() => overview.value?.recent_tasks ?? [])
</script>

<template>
  <section class="vision-overview">
    <header class="vision-overview__header">
      <div>
        <h2 class="vision-overview__title">识别概览</h2>
        <p class="vision-overview__subtitle">
          全部指标来自真实 vision API；检测帧与目标数由后端 artifact 汇总，前端不重新计算。
        </p>
      </div>
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
    </header>

    <AsyncState :loading="loading" :error="error" @retry="load">
      <template v-if="overview">
        <div class="vision-overview__stats" data-test="overview-stats">
          <el-card
            v-for="card in stats"
            :key="card.label"
            class="vision-overview__stat"
            shadow="never"
            data-test="overview-stat"
          >
            <p class="vision-overview__stat-label">{{ card.label }}</p>
            <p class="vision-overview__stat-value">{{ card.value }}</p>
            <p class="vision-overview__stat-hint">{{ card.hint }}</p>
          </el-card>
        </div>

        <div class="vision-overview__panels">
          <el-card class="vision-overview__panel" shadow="never" data-test="vision-health">
            <template #header>
              <h3 class="vision-overview__panel-title">推理服务状态</h3>
            </template>
            <el-descriptions :column="1" size="small">
              <el-descriptions-item label="服务状态">
                {{ reachableText }}
              </el-descriptions-item>
              <el-descriptions-item label="服务 / 版本">
                {{ serviceStatus }} · {{ health?.service?.version ?? '—' }}
              </el-descriptions-item>
              <el-descriptions-item label="模型版本">
                {{ modelVersion }}
              </el-descriptions-item>
              <el-descriptions-item label="运行设备">
                {{ device }}
              </el-descriptions-item>
              <el-descriptions-item label="模型许可">
                {{ license }}
              </el-descriptions-item>
              <el-descriptions-item v-if="health?.error" label="错误">
                <span class="vision-overview__error">{{ health.error }}</span>
              </el-descriptions-item>
            </el-descriptions>
          </el-card>

          <el-card
            class="vision-overview__panel"
            shadow="never"
            data-test="overview-recent"
          >
            <template #header>
              <h3 class="vision-overview__panel-title">最近识别任务</h3>
            </template>

            <ul v-if="recentTasks.length > 0" class="vision-overview__recent">
              <li v-for="task in recentTasks" :key="task.task_uid">
                <router-link
                  class="vision-overview__recent-name"
                  :to="`/vision/records/${task.task_uid}`"
                >
                  {{ task.video_filename }}
                </router-link>
                <span class="vision-overview__recent-meta">
                  {{ task.status }} · {{ formatDateTime(task.created_at) }}
                </span>
              </li>
            </ul>
            <p v-else class="vision-overview__empty">暂无识别任务</p>
          </el-card>
        </div>
      </template>
    </AsyncState>
  </section>
</template>

<style scoped>
.vision-overview {
  width: 100%;
}

.vision-overview__header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
  padding: 18px 20px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.vision-overview__title {
  margin: 0;
  color: #111827;
  font-size: 18px;
  font-weight: 600;
}

.vision-overview__subtitle {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
}

.vision-overview__stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
}

.vision-overview__stat {
  border-radius: 12px;
}

.vision-overview__stat-label {
  margin: 0;
  color: #6b7280;
  font-size: 12px;
}

.vision-overview__stat-value {
  margin: 8px 0 0;
  color: #111827;
  font-size: 26px;
  font-weight: 600;
}

.vision-overview__stat-hint {
  margin: 6px 0 0;
  color: #94a3b8;
  font-size: 12px;
  line-height: 1.6;
}

.vision-overview__panels {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 16px;
  margin-top: 16px;
}

.vision-overview__panel {
  border-radius: 12px;
}

.vision-overview__panel-title {
  margin: 0;
  color: #111827;
  font-size: 15px;
  font-weight: 600;
}

.vision-overview__recent {
  margin: 0;
  padding: 0;
  list-style: none;
}

.vision-overview__recent li {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid #f1f5f9;
}

.vision-overview__recent-name {
  color: #2563eb;
  font-size: 13px;
  font-weight: 500;
  text-decoration: none;
}

.vision-overview__recent-meta {
  color: #94a3b8;
  font-size: 12px;
}

.vision-overview__empty {
  margin: 0;
  padding: 24px 0;
  color: #94a3b8;
  font-size: 13px;
  text-align: center;
}

.vision-overview__error {
  color: #dc2626;
}

@media (max-width: 960px) {
  .vision-overview__panels {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
