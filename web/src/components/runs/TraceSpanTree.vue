<script setup lang="ts">
import { computed } from 'vue'

import type { SpanNode } from '@/api/observability'
import { formatDurationMs } from '@/utils/status'

const props = withDefaults(
  defineProps<{
    nodes: SpanNode[]
    depth?: number
    selectedSpanId?: string | null
  }>(),
  {
    depth: 0,
    selectedSpanId: null,
  },
)

const emit = defineEmits<{
  select: [spanId: string]
}>()

interface FlatSpanRow {
  node: SpanNode
  depth: number
}

/**
 * 把 Span 树展开为带缩进层级的扁平列表。
 * 这样任意深度都能渲染，且不依赖组件自引用。
 */
const rows = computed<FlatSpanRow[]>(() => {
  const result: FlatSpanRow[] = []
  const walk = (list: SpanNode[], depth: number): void => {
    for (const node of list) {
      result.push({ node, depth })
      if (node.children && node.children.length > 0) {
        walk(node.children, depth + 1)
      }
    }
  }
  walk(props.nodes, 0)
  return result
})
</script>

<template>
  <ul class="span-tree">
    <li v-for="row in rows" :key="row.node.span_id" class="span-tree__item">
      <button
        type="button"
        class="span-tree__row"
        :class="{ 'span-tree__row--active': row.node.span_id === selectedSpanId }"
        :style="{ paddingLeft: `${10 + (depth + row.depth) * 18}px` }"
        :data-test="`span-node-${row.node.span_id}`"
        @click="emit('select', row.node.span_id)"
      >
        <span class="span-tree__depth" aria-hidden="true">{{ row.depth > 0 ? '└' : '•' }}</span>
        <span class="span-tree__name">{{ row.node.name }}</span>
        <el-tag size="small" type="info" effect="plain">{{ row.node.span_type }}</el-tag>
        <span class="span-tree__meta">{{ row.node.status }}</span>
        <span class="span-tree__meta">{{ formatDurationMs(row.node.duration_ms) }}</span>
        <span v-if="row.node.node_id" class="span-tree__meta">
          node: {{ row.node.node_id }}
        </span>
        <span v-if="row.node.tool_id" class="span-tree__meta">
          tool: {{ row.node.tool_id }}
        </span>
        <el-tag v-if="row.node.orphan" size="small" type="warning" effect="light">
          孤立
        </el-tag>
        <span v-if="row.node.retry_count" class="span-tree__meta">
          重试 {{ row.node.retry_count }} 次
        </span>
      </button>
    </li>
  </ul>
</template>

<style scoped>
.span-tree {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.span-tree__row {
  display: flex;
  width: 100%;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 7px 10px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #ffffff;
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.span-tree__row:hover {
  border-color: #bfdbfe;
}

.span-tree__row--active {
  border-color: #3b82f6;
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.12);
}

.span-tree__depth {
  color: #cbd5e1;
  font-size: 11px;
}

.span-tree__name {
  font-size: 13px;
  font-weight: 500;
  color: #111827;
}

.span-tree__meta {
  color: #94a3b8;
  font-size: 12px;
}
</style>
