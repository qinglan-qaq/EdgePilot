<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'

import { ApiError } from '@/api/http'
import {
  visionApi,
  type VisionHealth,
  type VisionModelRow,
  type VisionModelsResponse,
} from '@/api/vision'
import AsyncState from '@/components/common/AsyncState.vue'

function errorToMessage(value: unknown): string {
  if (value instanceof ApiError) {
    return value.message
  }
  if (value instanceof Error && value.message) {
    return value.message
  }
  return '请求失败，请稍后重试'
}

const rows = ref<VisionModelRow[]>([])
const health = ref<VisionHealth | null>(null)
const loading = ref(true)
const error = ref<string | null>(null)

let token = 0

/** 兼容后端数组形状与 {items}/{models} 形状；缺失时返回空列表。 */
function modelItems(
  response: VisionModelsResponse | VisionModelRow[],
): VisionModelRow[] {
  if (Array.isArray(response)) {
    return response
  }
  return response.items ?? response.models ?? []
}

/**
 * 加载 DB 模型登记与实时健康状态。
 *
 * 关键节点：健康检查失败不阻断模型表；模型接口失败才算整页错误。
 */
async function load(): Promise<void> {
  const current = ++token
  loading.value = true
  error.value = null

  try {
    const [modelsResponse, healthResponse] = await Promise.all([
      visionApi.models(),
      visionApi.health().catch(() => null),
    ])
    if (current !== token) {
      return
    }
    rows.value = modelItems(modelsResponse)
    health.value = healthResponse
  } catch (cause) {
    if (current !== token) {
      return
    }
    rows.value = []
    error.value = errorToMessage(cause)
  } finally {
    if (current === token) {
      loading.value = false
    }
  }
}

onMounted(load)

const reachable = computed(() => Boolean(health.value?.reachable))

const liveStatus = computed(() => {
  if (!health.value) {
    return '未上报'
  }
  return health.value.reachable ? '可达' : '不可达'
})

/** 许可只认后端上报；缺失时明确显示未上报，不替模型发明许可证。 */
const licenseText = computed(
  () => health.value?.model?.license ?? rows.value[0]?.license ?? '未上报',
)
</script>

<template>
  <section class="vision-models">
    <header class="vision-models__header">
      <div>
        <h2 class="vision-models__title">模型与许可</h2>
        <p class="vision-models__subtitle">
          DB 模型登记是事实来源；推理服务实时状态仅作参考，不覆盖本地登记。
        </p>
      </div>
      <el-button size="small" :loading="loading" @click="load">刷新</el-button>
    </header>

    <el-alert
      class="vision-models__license"
      data-test="license-alert"
      type="warning"
      :closable="false"
      show-icon
      title="YOLO11n 模型许可提醒"
    >
      <p class="vision-models__license-text">
        YOLO11n 使用 <strong>AGPL-3.0</strong> 许可。商业使用必须遵守 AGPL-3.0
        的开源义务（包括向网络用户提供对应源码），或购买 Ultralytics
        <strong>Enterprise License</strong> 获得商业授权；本系统默认不授予任何商业使用权利。
      </p>
      <p class="vision-models__license-text">
        选型与部署前请由法务确认具体授权路径，当前许可：{{ licenseText }}。
      </p>
    </el-alert>

    <el-alert
      v-if="health && !reachable"
      class="vision-models__service"
      data-test="service-hint"
      type="error"
      :closable="false"
      show-icon
      title="推理服务不可达"
    >
      <p class="vision-models__service-text">
        {{ health.error ?? 'vision-service 不可达' }}
      </p>
      <p class="vision-models__service-text">
        请先启动 vision-service：在 <code>vision-service</code> 目录执行
        <code>uvicorn app.main:app --host 0.0.0.0 --port 8001</code>，与
        <code>VISION_SERVICE_URL</code> 的默认端口保持一致。
      </p>
    </el-alert>

    <div class="vision-models__health" data-test="vision-models-health">
      <span>推理服务：{{ liveStatus }}</span>
      <span>模型：{{ health?.model?.model_version ?? '未上报' }}</span>
      <span>设备：{{ health?.model?.device ?? '未上报' }}</span>
      <span>许可：{{ licenseText }}</span>
    </div>

    <AsyncState
      class="vision-models__body"
      :loading="loading"
      :error="error"
      @retry="load"
    >
      <el-table :data="rows" row-key="model_key" size="small" data-test="models-table">
        <el-table-column prop="model_key" label="模型 key" min-width="130" />
        <el-table-column label="显示名" min-width="130">
          <template #default="{ row }">{{ row.display_name ?? row.model_name ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="版本" min-width="130">
          <template #default="{ row }">{{ row.model_version ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="Provider" min-width="120">
          <template #default="{ row }">{{ row.provider ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="设备" width="90">
          <template #default="{ row }">
            {{ row.live?.device ?? row.device ?? '未上报' }}
          </template>
        </el-table-column>
        <el-table-column label="输入尺寸" width="100">
          <template #default="{ row }">
            {{ Array.isArray(row.input_size) ? row.input_size.join('×') : (row.input_size ?? '—') }}
          </template>
        </el-table-column>
        <el-table-column label="类别数" width="90">
          <template #default="{ row }">
            {{ Array.isArray(row.classes) ? row.classes.length : '—' }}
          </template>
        </el-table-column>
        <el-table-column label="许可" min-width="120">
          <template #default="{ row }">{{ row.license ?? '—' }}</template>
        </el-table-column>
        <el-table-column label="状态" width="100">
          <template #default="{ row }">{{ row.status ?? '—' }}</template>
        </el-table-column>
      </el-table>
    </AsyncState>
  </section>
</template>

<style scoped>
.vision-models {
  width: 100%;
}

.vision-models__header {
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

.vision-models__title {
  margin: 0;
  color: #111827;
  font-size: 18px;
  font-weight: 600;
}

.vision-models__subtitle {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
}

.vision-models__license,
.vision-models__service {
  margin-bottom: 12px;
}

.vision-models__license-text,
.vision-models__service-text {
  margin: 4px 0 0;
  font-size: 13px;
  line-height: 1.7;
}

.vision-models__service-text code {
  padding: 1px 4px;
  border-radius: 4px;
  background: rgba(15, 23, 42, 0.06);
  font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
  font-size: 12px;
}

.vision-models__health {
  display: flex;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 12px;
  padding: 12px 16px;
  border-radius: 12px;
  background: #ffffff;
  color: #475569;
  font-size: 13px;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.vision-models__body {
  padding: 16px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}
</style>
