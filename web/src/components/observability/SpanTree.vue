<script setup lang="ts">
import { computed, ref } from 'vue'

import type { SpanNode } from '@/api/observability'
import {
  formatDurationMs,
  isFailedSpanStatus,
  runStatusLabel,
  runStatusTagType,
  spanTypeLabel,
} from '@/utils/status'

const props = withDefaults(
  defineProps<{
    nodes: SpanNode[]
    selectedSpanId?: string | null
    /** Trace 总耗时，用于计算 Span 的相对占比；缺失时不显示百分比。 */
    traceDurationMs?: number | null
  }>(),
  {
    selectedSpanId: null,
    traceDurationMs: null,
  },
)

const emit = defineEmits<{
  select: [spanId: string]
}>()

interface TreeInstance {
  setCurrentKey: (key?: string | number | null) => void
}

const treeRef = ref<TreeInstance | null>(null)

/** 根节点始终展开；失败节点及其全部祖先也默认展开，便于直接定位异常链路。 */
const expandedKeys = computed<string[]>(() => {
  const keys = new Set<string>()
  for (const root of props.nodes) {
    keys.add(root.span_id)
  }
  const walk = (list: SpanNode[], ancestors: string[]): void => {
    for (const node of list) {
      const path = [...ancestors, node.span_id]
      const failed = isFailedSpanStatus(node.status) || node.error != null
      if (failed) {
        for (const id of path) {
          keys.add(id)
        }
      }
      if (node.children && node.children.length > 0) {
        walk(node.children, path)
      }
    }
  }
  walk(props.nodes, [])
  return [...keys]
})

const treeProps = { children: 'children', label: 'name' }

/** 当前耗时占 Trace 总耗时的百分比；任一缺失都显示 —，不估算。 */
function relativePercent(node: SpanNode): string {
  const total = props.traceDurationMs
  const duration = node.duration_ms
  if (
    total === null ||
    total === undefined ||
    !Number.isFinite(total) ||
    total <= 0 ||
    duration === null ||
    duration === undefined ||
    !Number.isFinite(duration)
  ) {
    return '—'
  }
  return `${Math.min(100, Math.max(0, (duration / total) * 100)).toFixed(0)}%`
}

function handleNodeClick(data: SpanNode): void {
  treeRef.value?.setCurrentKey(data.span_id)
  emit('select', data.span_id)
}
</script>

<template>
  <div class="span-tree" data-test="span-tree">
    <el-tree
      ref="treeRef"
      :data="nodes"
      node-key="span_id"
      :props="treeProps"
      :default-expanded-keys="expandedKeys"
      :current-node-key="selectedSpanId ?? undefined"
      :expand-on-click-node="false"
      highlight-current
      @node-click="handleNodeClick"
    >
      <template #default="{ data }">
        <div
          class="span-tree__row"
          :class="{ 'span-tree__row--failed': isFailedSpanStatus(data.status) }"
          :data-test="`span-tree-node-${data.span_id}`"
        >
          <el-tag size="small" type="info" effect="plain" class="span-tree__type">
            {{ spanTypeLabel(data.span_type) }}
          </el-tag>
          <span class="span-tree__name" :title="data.name">{{ data.name }}</span>
          <el-tag size="small" :type="runStatusTagType(data.status)" effect="light">
            {{ runStatusLabel(data.status) }}
          </el-tag>
          <span class="span-tree__duration">
            {{ formatDurationMs(data.duration_ms) }}
            <span class="span-tree__percent">({{ relativePercent(data) }})</span>
          </span>
          <el-tag
            v-if="data.attempt && data.attempt > 1"
            size="small"
            type="warning"
            effect="plain"
          >
            第 {{ data.attempt }} 次
          </el-tag>
          <el-tag v-if="data.orphan" size="small" type="warning" effect="light">
            孤立
          </el-tag>
        </div>
      </template>
    </el-tree>
  </div>
</template>

<style scoped>
.span-tree {
  min-width: 0;
}

.span-tree__row {
  display: flex;
  min-width: 0;
  width: 100%;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 4px 6px 4px 0;
}

.span-tree__row--failed .span-tree__name {
  color: #b91c1c;
}

.span-tree__type {
  flex-shrink: 0;
}

.span-tree__name {
  min-width: 0;
  flex: 1 1 60px;
  overflow: hidden;
  color: #111827;
  font-size: 13px;
  font-weight: 500;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.span-tree__duration {
  color: #64748b;
  font-size: 12px;
  white-space: nowrap;
}

.span-tree__percent {
  color: #94a3b8;
}
</style>
