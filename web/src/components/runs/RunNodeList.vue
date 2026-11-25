<script setup lang="ts">
import type { WorkflowNodeRun } from '@/api/runs'
import { formatDateTime } from '@/utils/datetime'
import {
  formatDurationBetween,
  nodeStatusLabel,
  nodeStatusTagType,
  nodeTypeLabel,
} from '@/utils/status'

withDefaults(
  defineProps<{
    nodes: WorkflowNodeRun[]
    selectedNodeId?: string | null
  }>(),
  {
    selectedNodeId: null,
  },
)

const emit = defineEmits<{
  select: [nodeId: string]
}>()

/** 节点错误摘要只用于列表；完整错误在节点详情中展示。 */
function errorSummary(error: string | null): string {
  if (!error) {
    return ''
  }
  return error.length > 120 ? `${error.slice(0, 120)}…` : error
}
</script>

<template>
  <ul v-if="nodes.length > 0" class="node-list" data-test="run-node-list">
    <li
      v-for="node in nodes"
      :key="node.id"
      class="node-list__item"
      :class="{ 'node-list__item--active': node.node_id === selectedNodeId }"
      :data-test="`run-node-${node.node_id}`"
      @click="emit('select', node.node_id)"
    >
      <div class="node-list__row">
        <span class="node-list__id">{{ node.node_id }}</span>
        <el-tag size="small" type="info" effect="plain">
          {{ nodeTypeLabel(node.node_type) }}
        </el-tag>
        <el-tag size="small" :type="nodeStatusTagType(node.status)" effect="light">
          {{ nodeStatusLabel(node.status) }}
        </el-tag>
        <span class="node-list__attempt">第 {{ node.attempt }} 次</span>
        <span class="node-list__duration">
          {{
            formatDurationBetween(node.started_at, node.finished_at, {
              live: node.status === 'running',
            })
          }}
        </span>
      </div>

      <div class="node-list__meta">
        <span>开始：{{ formatDateTime(node.started_at) }}</span>
        <span>结束：{{ formatDateTime(node.finished_at) }}</span>
        <span v-if="node.heartbeat_at && node.status === 'running'">
          心跳：{{ formatDateTime(node.heartbeat_at) }}
        </span>
      </div>

      <p v-if="node.error" class="node-list__error" :data-test="`node-error-${node.node_id}`">
        {{ errorSummary(node.error) }}
      </p>
    </li>
  </ul>
  <el-empty v-else description="暂无节点运行记录" data-test="run-node-empty" />
</template>

<style scoped>
.node-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.node-list__item {
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #ffffff;
  cursor: pointer;
  transition: border-color 0.15s ease, box-shadow 0.15s ease;
}

.node-list__item:hover {
  border-color: #bfdbfe;
}

.node-list__item--active {
  border-color: #3b82f6;
  box-shadow: 0 0 0 2px rgba(59, 130, 246, 0.12);
}

.node-list__row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.node-list__id {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 13px;
  font-weight: 600;
  color: #111827;
}

.node-list__attempt,
.node-list__duration,
.node-list__meta {
  color: #94a3b8;
  font-size: 12px;
}

.node-list__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 16px;
  margin-top: 6px;
}

.node-list__error {
  margin: 6px 0 0;
  color: #dc2626;
  font-size: 12px;
  line-height: 1.6;
}
</style>
