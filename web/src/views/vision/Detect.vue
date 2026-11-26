<script setup lang="ts">
import { computed, ref } from 'vue'

import { ApiError } from '@/api/http'
import { visionApi } from '@/api/vision'
import AsyncState from '@/components/common/AsyncState.vue'
import { useAuthStore } from '@/stores/auth'
import { useVisionStore } from '@/stores/vision'

/** 支持的后缀；MIME 为空时（部分系统）仍按后缀放行。 */
const VIDEO_EXTENSIONS = ['.mp4', '.avi', '.mov', '.mkv', '.webm', '.m4v', '.flv', '.wmv']
const MAX_FILE_BYTES = 200 * 1024 * 1024

const STRIDE_OPTIONS = [
  { value: 'auto', label: '自动抽帧（约 900 帧）' },
  { value: '1', label: '每 1 帧' },
  { value: '5', label: '每 5 帧' },
  { value: '10', label: '每 10 帧' },
  { value: '25', label: '每 25 帧' },
]

function errorToMessage(value: unknown): string {
  if (value instanceof ApiError) {
    return value.message
  }
  if (value instanceof Error && value.message) {
    return value.message
  }
  return '上传失败，请稍后重试'
}

const auth = useAuthStore()
const store = useVisionStore()

const selectedFile = ref<File | null>(null)
const confThreshold = ref(0.25)
const stride = ref('auto')
const zonesText = ref('')
const submitting = ref(false)
const fileError = ref<string | null>(null)
const submitError = ref<string | null>(null)

/** 上传入口仅 admin/operator 可见；viewer 只能查看记录。 */
const canUpload = computed(() => auth.role === 'admin' || auth.role === 'operator')

const selectedName = computed(() => selectedFile.value?.name ?? '')

function isVideoFile(file: File): boolean {
  const name = file.name.toLowerCase()
  return (
    file.type.startsWith('video/') ||
    VIDEO_EXTENSIONS.some((extension) => name.endsWith(extension))
  )
}

function handleFileChange(event: Event): void {
  const input = event.target as HTMLInputElement | null
  selectedFile.value = input?.files?.[0] ?? null
  fileError.value = null
  submitError.value = null
}

/** 客户端先做后缀/大小校验，非法文件不发任何请求。 */
function validate(file: File | null): string | null {
  if (!file) {
    return '请先选择视频文件（支持 mp4/avi/mov/mkv/webm 等格式）'
  }
  if (!isVideoFile(file)) {
    return '仅支持 mp4/avi/mov/mkv/webm 等视频文件'
  }
  if (file.size > MAX_FILE_BYTES) {
    return '视频文件不能超过 200MB'
  }
  return null
}

function buildFormData(file: File): FormData {
  const form = new FormData()
  form.append('file', file)
  form.append('conf_threshold', String(confThreshold.value))
  if (stride.value === 'auto') {
    form.append('target_samples', '900')
  } else {
    form.append('frame_stride', stride.value)
  }
  const zones = zonesText.value.trim()
  if (zones) {
    form.append('zones', zones)
  }
  return form
}

async function handleUpload(): Promise<void> {
  if (submitting.value || !canUpload.value) {
    return
  }
  const invalid = validate(selectedFile.value)
  if (invalid) {
    fileError.value = invalid
    return
  }

  const file = selectedFile.value as File
  submitting.value = true
  fileError.value = null
  submitError.value = null

  try {
    const result = await visionApi.createTask(buildFormData(file))
    store.startPolling(result.task.task_uid)
  } catch (cause) {
    submitError.value = errorToMessage(cause)
  } finally {
    submitting.value = false
  }
}

const statusText = computed(() => {
  const task = store.task
  if (!task) {
    return ''
  }
  return `${task.status} · ${task.progress}%`
})
</script>

<template>
  <section class="vision-detect">
    <header class="vision-detect__header">
      <div>
        <h2 class="vision-detect__title">视频识别</h2>
        <p class="vision-detect__subtitle">
          上传 mp4 等视频（≤200MB），由 vision-service 抽帧推理；识别完成后可在记录页查看结果。
        </p>
      </div>
    </header>

    <AsyncState :loading="false" :error="null">
      <el-form class="vision-detect__form" label-width="96px" label-position="left">
        <el-form-item label="视频文件">
          <input
            class="vision-detect__file"
            type="file"
            accept="video/*,.mp4,.avi,.mov,.mkv,.webm"
            @change="handleFileChange"
          />
          <span v-if="selectedName" class="vision-detect__filename">
            {{ selectedName }}
          </span>
        </el-form-item>

        <el-form-item label="置信度">
          <el-slider
            v-model="confThreshold"
            :min="0.05"
            :max="0.95"
            :step="0.05"
            class="vision-detect__slider"
          />
          <span class="vision-detect__value">{{ confThreshold.toFixed(2) }}</span>
        </el-form-item>

        <el-form-item label="抽帧策略">
          <el-select v-model="stride" class="vision-detect__select">
            <el-option
              v-for="option in STRIDE_OPTIONS"
              :key="option.value"
              :label="option.label"
              :value="option.value"
            />
          </el-select>
        </el-form-item>

        <el-form-item label="区域（可选）">
          <el-input
            v-model="zonesText"
            type="textarea"
            :rows="3"
            placeholder='区域 JSON，例如 [{"zone_id":"gate","polygon":[[0,0],[1,0],[1,1],[0,1]]}]'
          />
        </el-form-item>

        <el-form-item v-if="canUpload">
          <el-button
            type="primary"
            data-test="upload"
            :loading="submitting"
            @click="handleUpload"
          >
            开始识别
          </el-button>
        </el-form-item>
        <p v-else class="vision-detect__hint">当前角色仅可查看识别记录，无法上传视频。</p>
      </el-form>
    </AsyncState>

    <el-alert
      v-if="fileError"
      class="vision-detect__alert"
      type="error"
      :title="fileError"
      :closable="false"
      show-icon
    />
    <el-alert
      v-if="submitError"
      class="vision-detect__alert"
      type="error"
      :title="submitError"
      :closable="false"
      show-icon
    />

    <el-card v-if="store.task" class="vision-detect__progress" shadow="never">
      <template #header>
        <h3 class="vision-detect__progress-title">
          当前任务 {{ store.task.video_filename }}
        </h3>
      </template>
      <el-progress :percentage="store.task.progress" />
      <p class="vision-detect__progress-meta">
        状态：{{ statusText }} · 模型 {{ store.task.model_version ?? '待分配' }}
      </p>
      <p v-if="store.polling" class="vision-detect__polling">正在轮询识别进度…</p>
      <p v-if="store.error" class="vision-detect__polling-error">{{ store.error }}</p>
      <router-link
        v-if="store.task"
        class="vision-detect__detail-link"
        :to="`/vision/records/${store.task.task_uid}`"
      >
        查看任务详情
      </router-link>
    </el-card>
  </section>
</template>

<style scoped>
.vision-detect {
  width: 100%;
}

.vision-detect__header {
  margin-bottom: 16px;
  padding: 18px 20px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.vision-detect__title {
  margin: 0;
  color: #111827;
  font-size: 18px;
  font-weight: 600;
}

.vision-detect__subtitle {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
}

.vision-detect__form {
  padding: 20px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.vision-detect__file {
  margin-right: 12px;
}

.vision-detect__filename {
  color: #475569;
  font-size: 13px;
}

.vision-detect__slider {
  width: 260px;
  margin-right: 12px;
}

.vision-detect__value {
  color: #64748b;
  font-size: 13px;
}

.vision-detect__select {
  width: 240px;
}

.vision-detect__hint {
  margin: 0;
  color: #94a3b8;
  font-size: 13px;
}

.vision-detect__alert {
  margin-top: 16px;
}

.vision-detect__progress {
  margin-top: 16px;
  border-radius: 12px;
}

.vision-detect__progress-title {
  margin: 0;
  color: #111827;
  font-size: 15px;
  font-weight: 600;
}

.vision-detect__progress-meta {
  margin: 10px 0 0;
  color: #64748b;
  font-size: 13px;
}

.vision-detect__polling {
  margin: 8px 0 0;
  color: #2563eb;
  font-size: 12px;
}

.vision-detect__polling-error {
  margin: 8px 0 0;
  color: #dc2626;
  font-size: 12px;
}

.vision-detect__detail-link {
  display: inline-block;
  margin-top: 10px;
  color: #2563eb;
  font-size: 13px;
  text-decoration: none;
}
</style>
