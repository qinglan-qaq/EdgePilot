<script setup lang="ts">
import { computed } from 'vue'
import DOMPurify from 'dompurify'
import { marked } from 'marked'

const props = withDefaults(
  defineProps<{
    content?: string | null
  }>(),
  {
    content: '',
  },
)

const html = computed(() => {
  const text = (props.content ?? '').trim()
  if (!text) {
    return ''
  }
  const rendered = marked.parse(text, { async: false })
  if (typeof rendered !== 'string') {
    return ''
  }
  // 后端报告来自模型输出，必须 sanitize 后再 v-html。
  return DOMPurify.sanitize(rendered)
})

const hasContent = computed(() => html.value.length > 0)
</script>

<template>
  <div class="markdown-block">
    <div
      v-if="hasContent"
      class="markdown-body"
      data-test="markdown-body"
      v-html="html"
    />
    <p v-else class="markdown-block__empty" data-test="markdown-empty">无内容</p>
  </div>
</template>

<style scoped>
.markdown-block {
  width: 100%;
  overflow-x: auto;
}

.markdown-block__empty {
  margin: 0;
  color: #94a3b8;
  font-size: 13px;
}

.markdown-body {
  color: #1f2937;
  font-size: 14px;
  line-height: 1.8;
  word-break: break-word;
}

.markdown-body :deep(h1),
.markdown-body :deep(h2),
.markdown-body :deep(h3),
.markdown-body :deep(h4) {
  margin: 16px 0 8px;
  font-weight: 600;
  line-height: 1.4;
  color: #111827;
}

.markdown-body :deep(h1) {
  font-size: 20px;
}

.markdown-body :deep(h2) {
  font-size: 17px;
}

.markdown-body :deep(h3) {
  font-size: 15px;
}

.markdown-body :deep(p) {
  margin: 8px 0;
}

.markdown-body :deep(ul),
.markdown-body :deep(ol) {
  margin: 8px 0;
  padding-left: 22px;
}

.markdown-body :deep(li) {
  margin: 4px 0;
}

.markdown-body :deep(code) {
  padding: 1px 5px;
  border-radius: 4px;
  background: #f1f5f9;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  color: #be123c;
}

.markdown-body :deep(pre) {
  margin: 10px 0;
  padding: 12px 14px;
  overflow-x: auto;
  border-radius: 8px;
  background: #0f172a;
  color: #e2e8f0;
  font-size: 12px;
  line-height: 1.7;
}

.markdown-body :deep(pre code) {
  padding: 0;
  background: transparent;
  color: inherit;
}

.markdown-body :deep(blockquote) {
  margin: 10px 0;
  padding: 6px 14px;
  border-left: 3px solid #93c5fd;
  background: #eff6ff;
  color: #475569;
}

.markdown-body :deep(table) {
  margin: 10px 0;
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.markdown-body :deep(th),
.markdown-body :deep(td) {
  padding: 8px 10px;
  border: 1px solid #e2e8f0;
  text-align: left;
}

.markdown-body :deep(th) {
  background: #f8fafc;
  font-weight: 600;
}

.markdown-body :deep(a) {
  color: #2563eb;
  text-decoration: underline;
}

.markdown-body :deep(img) {
  max-width: 100%;
}
</style>
