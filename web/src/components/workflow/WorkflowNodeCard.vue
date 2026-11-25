<script setup lang="ts">
import { computed } from 'vue'
import { Handle, Position, type NodeProps } from '@vue-flow/core'

import {
  formatTimeout,
  nodeTypeMeta,
  type WorkflowNodeData,
} from './editor-model'

const props = defineProps<NodeProps<WorkflowNodeData>>()

/** 节点类型元数据；后端草稿中若出现未知类型则降级为灰色样式。 */
const meta = computed(() => nodeTypeMeta(props.data.nodeType))

/** 节点类型中文名。 */
const typeLabel = computed(() => meta.value?.label ?? props.data.nodeType)

/** 节点类型标识色。 */
const typeColor = computed(() => meta.value?.color ?? '#94a3b8')

/** 节点类型小标识。 */
const typeBadge = computed(() => meta.value?.badge ?? '?')

/** 是否 start / end / condition，用于控制连接桩。 */
const isStart = computed(() => props.data.nodeType === 'start')
const isEnd = computed(() => props.data.nodeType === 'end')
const isCondition = computed(() => props.data.nodeType === 'condition')

/** 节点超时；缺失时展示该类型的默认值。 */
const timeout = computed(() => {
  const value = props.data.fields.timeout_ms
  return typeof value === 'number' && Number.isFinite(value)
    ? value
    : (meta.value?.defaultTimeoutMs ?? 30_000)
})

/** 卡片中的关键引用 / 摘要。 */
const summary = computed(() => {
  const fields = props.data.fields
  if (props.data.nodeType === 'start') {
    return '流程入口'
  }
  if (props.data.nodeType === 'end') {
    return '流程终点'
  }
  if (props.data.nodeType === 'agent') {
    return typeof fields.agent === 'string' && fields.agent ? fields.agent : '未选择 Agent'
  }
  if (props.data.nodeType === 'tool') {
    return typeof fields.tool === 'string' && fields.tool ? fields.tool : '未选择工具'
  }
  if (props.data.nodeType === 'condition') {
    return typeof fields.expression === 'string' && fields.expression
      ? fields.expression
      : '未填写条件表达式'
  }
  if (props.data.nodeType === 'approval') {
    const title = typeof fields.title === 'string' && fields.title ? fields.title : '未填写审批标题'
    const approvers = Array.isArray(fields.approvers) ? fields.approvers.length : 0
    return `${title} · ${approvers} 个审批角色`
  }
  return props.data.nodeType
})
</script>

<template>
  <div
    class="wf-node"
    :class="[`wf-node--${data.nodeType}`]"
    :style="{ borderTopColor: typeColor }"
    :data-test="`workflow-node-${id}`"
    :data-node-id="id"
  >
    <Handle
      v-if="!isStart"
      type="target"
      :position="Position.Left"
      :connectable="connectable"
    />

    <header class="wf-node__header">
      <span class="wf-node__badge" :style="{ backgroundColor: typeColor }">{{ typeBadge }}</span>
      <span class="wf-node__type">{{ typeLabel }}</span>
      <span class="wf-node__id" :title="`可在表达式中使用 $${id}.output 引用`">{{ id }}</span>
    </header>

    <p class="wf-node__summary" :title="summary">{{ summary }}</p>

    <footer class="wf-node__footer">
      <span>超时 {{ formatTimeout(timeout) }}</span>
      <span v-if="!isStart && !isEnd" class="wf-node__hint">表达式引用 ${{ id }}.output</span>
    </footer>

    <template v-if="isCondition">
      <Handle
        id="true"
        type="source"
        :position="Position.Right"
        :connectable="connectable"
        :style="{ top: '34%' }"
      />
      <span class="wf-node__branch wf-node__branch--true">是</span>
      <Handle
        id="false"
        type="source"
        :position="Position.Right"
        :connectable="connectable"
        :style="{ top: '72%' }"
      />
      <span class="wf-node__branch wf-node__branch--false">否</span>
    </template>
    <Handle
      v-else-if="!isEnd"
      type="source"
      :position="Position.Right"
      :connectable="connectable"
    />
  </div>
</template>

<style scoped>
.wf-node {
  position: relative;
  width: 220px;
  padding: 10px 12px 9px;
  border: 1px solid #e5e7eb;
  border-top-width: 4px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 6px 18px rgba(17, 24, 39, 0.08);
  color: #111827;
}

.wf-node__header {
  display: flex;
  align-items: center;
  gap: 6px;
  min-width: 0;
}

.wf-node__badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 20px;
  height: 20px;
  border-radius: 6px;
  color: #ffffff;
  font-size: 11px;
  font-weight: 700;
  flex: none;
}

.wf-node__type {
  font-size: 12px;
  font-weight: 600;
  color: #374151;
  flex: none;
}

.wf-node__id {
  margin-left: auto;
  max-width: 96px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: 'JetBrains Mono', 'SFMono-Regular', Consolas, monospace;
  font-size: 11px;
  color: #6b7280;
}

.wf-node__summary {
  margin: 8px 0 6px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 12px;
  color: #4b5563;
}

.wf-node__footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  font-size: 11px;
  color: #9ca3af;
}

.wf-node__hint {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.wf-node__branch {
  position: absolute;
  right: 12px;
  transform: translateY(-50%);
  padding: 0 4px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.92);
  font-size: 10px;
  line-height: 16px;
  color: #92400e;
}

.wf-node__branch--true {
  top: 34%;
}

.wf-node__branch--false {
  top: 72%;
}

.wf-node :deep(.vue-flow__handle) {
  width: 10px;
  height: 10px;
  border: 2px solid #ffffff;
  background: #64748b;
}

.wf-node :deep(.vue-flow__handle-right) {
  right: -6px;
}

.wf-node :deep(.vue-flow__handle-left) {
  left: -6px;
}
</style>
