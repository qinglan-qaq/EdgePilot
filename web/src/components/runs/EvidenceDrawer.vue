<script setup lang="ts">
import { computed, ref, watch } from 'vue'

import { getEvent, type EventDetail } from '@/api/events'
import { ApiError } from '@/api/http'
import { formatDateTime } from '@/utils/datetime'
import { formatJson, type ReportEvidence } from '@/utils/report'

const props = defineProps<{
  modelValue: boolean
  items: ReportEvidence[]
}>()

const emit = defineEmits<{
  'update:modelValue': [value: boolean]
}>()

const visible = computed({
  get: () => props.modelValue,
  set: (value: boolean) => emit('update:modelValue', value),
})

const activeIndex = ref<number | null>(null)
const loading = ref(false)
const error = ref<string | null>(null)
const eventDetail = ref<EventDetail | null>(null)

const activeItem = computed<ReportEvidence | null>(() => {
  if (activeIndex.value === null) {
    return null
  }
  return props.items[activeIndex.value] ?? null
})

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

function resetDetail(): void {
  activeIndex.value = null
  loading.value = false
  error.value = null
  eventDetail.value = null
}

/**
 * 打开抽屉时不自动请求；只有点击具体证据的“查看”才加载。
 * chunk_id 后端没有详情接口，只展示引用与片段。
 */
async function view(index: number): Promise<void> {
  const item = props.items[index]
  if (!item) {
    return
  }
  // 同一证据已加载时重复点击不重复请求。
  if (activeIndex.value === index && eventDetail.value && !error.value) {
    return
  }

  activeIndex.value = index
  loading.value = false
  error.value = null
  eventDetail.value = null

  if (item.type !== 'event_id') {
    return
  }

  if (!/^\d+$/.test(item.ref) || !Number.isSafeInteger(Number(item.ref))) {
    error.value = '无效事件 ID，无法查看详情。'
    return
  }

  loading.value = true
  try {
    eventDetail.value = await getEvent(Number(item.ref))
  } catch (cause) {
    error.value = errorToMessage(cause)
  } finally {
    loading.value = false
  }
}

// 打开与关闭都清空上一次的详情，避免证据列表变化后展示过期内容。
watch(
  () => props.modelValue,
  () => resetDetail(),
)

watch(
  () => props.items,
  (items) => {
    if (activeIndex.value !== null && !items[activeIndex.value]) {
      resetDetail()
    }
  },
)
</script>

<template>
  <el-drawer v-model="visible" title="运行证据" size="52%" data-test="evidence-drawer">
    <div class="evidence">
      <p class="evidence__hint">
        证据来自运行输出，仅做展示；点击“查看”才会请求详情。
      </p>

      <p v-if="items.length === 0" class="evidence__empty">本次运行没有可展示的证据。</p>

      <ul v-else class="evidence__list">
        <li
          v-for="(item, index) in items"
          :key="`${item.type}:${item.ref}`"
          class="evidence__item"
          :data-test="`evidence-item-${index}`"
        >
          <div class="evidence__row">
            <el-tag size="small" type="info" effect="plain">#{{ index + 1 }}</el-tag>
            <el-tag size="small" effect="light">{{ item.type }}</el-tag>
            <button
              type="button"
              class="evidence__ref evidence__ref--action"
              :data-test="`evidence-ref-${index}`"
              :aria-label="`查看证据 ${index + 1}：${item.type} ${item.ref}`"
              @click="view(index)"
            >
              {{ item.ref }}
            </button>
            <el-button
              size="small"
              type="primary"
              plain
              :data-test="`evidence-view-${index}`"
              @click="view(index)"
            >
              查看
            </el-button>
          </div>
          <p class="evidence__snippet">{{ item.snippet ?? '无片段' }}</p>
        </li>
      </ul>

      <section v-if="activeItem" class="evidence__detail" data-test="evidence-detail">
        <h3 class="evidence__title">证据详情：{{ activeItem.type }} {{ activeItem.ref }}</h3>

        <div v-if="loading" class="evidence__muted">加载中…</div>

        <el-alert
          v-else-if="error"
          :title="error"
          type="error"
          :closable="false"
          show-icon
          data-test="evidence-error"
        />

        <template v-else-if="eventDetail">
          <div data-test="evidence-event-detail">
            <dl class="evidence__fields">
              <div>
                <dt>事件 ID</dt>
                <dd>{{ eventDetail.id }}</dd>
              </div>
              <div>
                <dt>事件类型</dt>
                <dd>{{ eventDetail.event_type }}</dd>
              </div>
              <div>
                <dt>严重级别</dt>
                <dd>{{ eventDetail.severity ?? '未上报' }}</dd>
              </div>
              <div>
                <dt>发生时间</dt>
                <dd>{{ formatDateTime(eventDetail.occurred_at) }}</dd>
              </div>
              <div>
                <dt>节点</dt>
                <dd>{{ eventDetail.node_id ?? '未上报' }}</dd>
              </div>
              <div>
                <dt>设备</dt>
                <dd>{{ eventDetail.device_id ?? '未上报' }}</dd>
              </div>
              <div>
                <dt>置信度</dt>
                <dd>{{ eventDetail.confidence ?? '未上报' }}</dd>
              </div>
              <div>
                <dt>模型版本</dt>
                <dd>{{ eventDetail.model_version ?? '未上报' }}</dd>
              </div>
              <div>
                <dt>状态</dt>
                <dd>{{ eventDetail.status }}</dd>
              </div>
            </dl>

            <p class="evidence__snapshot" data-test="evidence-snapshot">
              快照：{{ eventDetail.has_snapshot ? '有（未内嵌预览）' : '无' }}
            </p>

            <h4 class="evidence__sub-title">bbox</h4>
            <pre class="evidence__json">{{ formatJson(eventDetail.bbox) }}</pre>

            <h4 class="evidence__sub-title">payload</h4>
            <pre class="evidence__json">{{ formatJson(eventDetail.payload) }}</pre>
          </div>
        </template>

        <template v-else-if="activeItem.type === 'chunk_id'">
          <p class="evidence__note" data-test="evidence-chunk-note">
            分块详情接口尚未提供，仅展示引用与片段。
          </p>
          <p class="evidence__line">
            ref：<code class="evidence__ref">{{ activeItem.ref }}</code>
          </p>
          <p class="evidence__line">
            片段：{{ activeItem.snippet ?? '无片段' }}
          </p>
        </template>

        <template v-else-if="activeItem.type === 'metric'">
          <p class="evidence__note" data-test="evidence-metric-note">
            指标证据仅展示引用与片段，不在此处推算数值。
          </p>
          <p class="evidence__line">
            ref：<code class="evidence__ref">{{ activeItem.ref }}</code>
          </p>
          <p class="evidence__line">
            片段：{{ activeItem.snippet ?? '无片段' }}
          </p>
        </template>

        <p v-else class="evidence__note" data-test="evidence-unsupported">
          暂不支持查看该类型证据（{{ activeItem.type }}），仅展示引用与片段。
        </p>
      </section>
    </div>
  </el-drawer>
</template>

<style scoped>
.evidence {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.evidence__hint {
  margin: 0;
  color: #6b7280;
  font-size: 12px;
  line-height: 1.7;
}

.evidence__empty,
.evidence__muted {
  margin: 0;
  color: #94a3b8;
  font-size: 13px;
}

.evidence__list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.evidence__item {
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #ffffff;
}

.evidence__row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.evidence__ref {
  padding: 1px 5px;
  border: none;
  border-radius: 4px;
  background: #f1f5f9;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  color: #334155;
}

.evidence__ref--action {
  cursor: pointer;
  text-decoration: underline dotted;
}

.evidence__ref--action:hover {
  color: #2563eb;
  background: #eff6ff;
}

.evidence__row .el-button {
  margin-left: auto;
}

.evidence__snippet {
  margin: 6px 0 0;
  color: #64748b;
  font-size: 12px;
  line-height: 1.7;
  word-break: break-word;
}

.evidence__detail {
  padding-top: 12px;
  border-top: 1px solid #eef1f6;
}

.evidence__title {
  margin: 0 0 8px;
  font-size: 14px;
  font-weight: 600;
  color: #111827;
}

.evidence__fields {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 8px 20px;
  margin: 0 0 10px;
  font-size: 12px;
}

.evidence__fields div {
  display: flex;
  gap: 6px;
}

.evidence__fields dt {
  flex-shrink: 0;
  color: #94a3b8;
}

.evidence__fields dd {
  margin: 0;
  color: #334155;
}

.evidence__snapshot {
  margin: 0 0 10px;
  font-size: 12px;
  color: #475569;
}

.evidence__sub-title {
  margin: 12px 0 6px;
  font-size: 12px;
  font-weight: 600;
  color: #374151;
}

.evidence__json {
  margin: 0;
  max-height: 200px;
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

.evidence__note {
  margin: 0 0 8px;
  padding: 8px 12px;
  border-radius: 8px;
  background: #fffbeb;
  color: #92400e;
  font-size: 12px;
  line-height: 1.7;
}

.evidence__line {
  margin: 4px 0;
  color: #475569;
  font-size: 12px;
  line-height: 1.7;
  word-break: break-word;
}
</style>
