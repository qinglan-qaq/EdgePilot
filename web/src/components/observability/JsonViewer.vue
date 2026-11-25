<script setup lang="ts">
import { computed, ref } from 'vue'
import { ElMessage } from 'element-plus'

/**
 * 只读 JSON 查看器：美化、折叠、复制与超大内容截断下载。
 * 内容全部来自接口真实返回值；null / undefined 统一显示“未记录”。
 */
const props = withDefaults(
  defineProps<{
    value: unknown
    /** 超过该字节数时只渲染截断内容并允许下载完整文件。 */
    maxBytes?: number
    /** 下载文件名。 */
    filename?: string
  }>(),
  {
    maxBytes: 32 * 1024,
    filename: 'payload.json',
  },
)

const expanded = ref(true)

const missing = computed(() => props.value === null || props.value === undefined)

/** 字符串尽量解析为 JSON 再美化；解析失败保持原文，避免丢信息。 */
function serialize(value: unknown): string {
  if (typeof value === 'string') {
    const text = value.trim()
    if (!text) {
      return ''
    }
    try {
      return JSON.stringify(JSON.parse(text), null, 2)
    } catch {
      return value
    }
  }
  try {
    const serialized = JSON.stringify(value, null, 2)
    return serialized === undefined ? String(value) : serialized
  } catch {
    return String(value)
  }
}

const fullText = computed(() => serialize(props.value))

function byteLength(text: string): number {
  if (typeof TextEncoder !== 'undefined') {
    return new TextEncoder().encode(text).length
  }
  return text.length
}

const oversized = computed(
  () => !missing.value && byteLength(fullText.value) > props.maxBytes,
)

/** 截断只影响展示；复制与下载始终使用完整序列化内容。 */
const displayText = computed(() => {
  if (missing.value) {
    return '未记录'
  }
  if (!oversized.value) {
    return fullText.value
  }
  return `${fullText.value.slice(0, props.maxBytes)}\n…`
})

async function copy(): Promise<void> {
  if (missing.value) {
    return
  }
  try {
    if (!navigator.clipboard || typeof navigator.clipboard.writeText !== 'function') {
      throw new Error('clipboard unavailable')
    }
    await navigator.clipboard.writeText(fullText.value)
    ElMessage.success('已复制 JSON')
  } catch {
    ElMessage.warning('复制失败，请手动选择内容复制')
  }
}

/** Blob 下载完整原始 JSON；浏览器不支持时给出提示而不是静默失败。 */
function download(): void {
  if (missing.value || typeof URL.createObjectURL !== 'function') {
    ElMessage.warning('当前环境不支持下载')
    return
  }
  const blob = new Blob([fullText.value], { type: 'application/json;charset=utf-8' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = props.filename
  anchor.click()
  URL.revokeObjectURL(url)
}
</script>

<template>
  <div class="json-viewer" data-test="json-viewer">
    <div class="json-viewer__bar">
      <button
        type="button"
        class="json-viewer__button"
        data-test="json-toggle"
        @click="expanded = !expanded"
      >
        {{ expanded ? '收起' : '展开' }}
      </button>
      <button
        v-if="!missing"
        type="button"
        class="json-viewer__button"
        data-test="json-copy"
        @click="copy"
      >
        复制
      </button>
      <button
        v-if="oversized"
        type="button"
        class="json-viewer__button"
        data-test="json-download"
        @click="download"
      >
        下载原文件
      </button>
    </div>

    <p v-if="!expanded" class="json-viewer__collapsed" data-test="json-collapsed">
      已收起
    </p>
    <pre
      v-else
      class="json-viewer__pre"
      :class="{ 'json-viewer__pre--missing': missing }"
      data-test="json-content"
    >{{ displayText }}</pre>

    <p v-if="oversized" class="json-viewer__truncated" data-test="json-truncated">
      内容超过 {{ Math.round(maxBytes / 1024) }}KB，已截断展示；可下载原文件查看完整 JSON。
    </p>
  </div>
</template>

<style scoped>
.json-viewer {
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #f8fafc;
}

.json-viewer__bar {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding: 6px 8px;
  border-bottom: 1px solid #eef1f6;
}

.json-viewer__button {
  padding: 2px 8px;
  border: 1px solid #dbe3ee;
  border-radius: 6px;
  background: #ffffff;
  color: #475569;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.json-viewer__button:hover {
  border-color: #bfdbfe;
  color: #1d4ed8;
}

.json-viewer__pre {
  margin: 0;
  max-height: 320px;
  overflow: auto;
  padding: 10px 12px;
  color: #334155;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

.json-viewer__pre--missing,
.json-viewer__collapsed {
  color: #94a3b8;
}

.json-viewer__collapsed {
  margin: 0;
  padding: 10px 12px;
  font-size: 12px;
}

.json-viewer__truncated {
  margin: 0;
  padding: 6px 12px 10px;
  color: #b45309;
  font-size: 12px;
}
</style>
