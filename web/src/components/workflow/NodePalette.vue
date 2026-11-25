<script setup lang="ts">
import {
  NODE_TYPE_META,
  NODE_TYPE_ORDER,
  WORKFLOW_NODE_DRAG_TYPE,
  type WorkflowNodeType,
} from './editor-model'

const props = withDefaults(
  defineProps<{
    /** 只读角色时禁止拖拽与点击添加。 */
    disabled?: boolean
    /** 画布中是否已经存在 start 节点。 */
    hasStart?: boolean
  }>(),
  {
    disabled: false,
    hasStart: false,
  },
)

const emit = defineEmits<{
  add: [type: WorkflowNodeType]
}>()

/** 节点面板中的简短说明。 */
const TYPE_HINTS: Record<WorkflowNodeType, string> = {
  start: '流程唯一入口',
  agent: '调用 Agent',
  tool: '调用工具',
  condition: '按表达式分支',
  approval: '等待人工审批',
  end: '流程终点',
}

/** 拖拽开始时写入节点类型，画布 drop 时读取。 */
function handleDragStart(event: DragEvent, type: WorkflowNodeType): void {
  if (props.disabled) {
    event.preventDefault()
    return
  }
  if (event.dataTransfer) {
    event.dataTransfer.setData(WORKFLOW_NODE_DRAG_TYPE, type)
    event.dataTransfer.setData('text/plain', type)
    event.dataTransfer.effectAllowed = 'move'
  }
}

/** 点击面板项也是真实添加路径之一；与拖拽共用画布的添加逻辑。 */
function handleClick(type: WorkflowNodeType): void {
  if (props.disabled) {
    return
  }
  emit('add', type)
}
</script>

<template>
  <el-card shadow="never" class="palette" :body-style="{ padding: '12px' }">
    <header class="palette__header">
      <h3>节点</h3>
      <span class="palette__count">{{ NODE_TYPE_ORDER.length }} 种</span>
    </header>

    <p class="palette__hint">
      {{ disabled ? '只读模式：可查看但无法添加或修改节点。' : '拖拽到画布，或点击添加到画布。' }}
    </p>

    <ul class="palette__list">
      <li
        v-for="type in NODE_TYPE_ORDER"
        :key="type"
        class="palette__item"
        :class="{ 'palette__item--muted': disabled || (type === 'start' && hasStart) }"
        :draggable="!disabled"
        :data-test="`palette-item-${type}`"
        :data-node-type="type"
        @dragstart="handleDragStart($event, type)"
        @click="handleClick(type)"
      >
        <span
          class="palette__badge"
          :style="{ backgroundColor: NODE_TYPE_META[type].color }"
        >
          {{ NODE_TYPE_META[type].badge }}
        </span>
        <span class="palette__text">
          <strong>{{ NODE_TYPE_META[type].label }}</strong>
          <small>{{ type === 'start' && hasStart ? '已存在 start 节点' : TYPE_HINTS[type] }}</small>
        </span>
      </li>
    </ul>

    <p v-if="hasStart" class="palette__note">工作流只能有一个 start 节点，重复添加会被拒绝。</p>
  </el-card>
</template>

<style scoped>
.palette {
  border-radius: 12px;
}

.palette__header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
}

.palette__header h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: #111827;
}

.palette__count {
  font-size: 12px;
  color: #9ca3af;
}

.palette__hint {
  margin: 6px 0 10px;
  font-size: 12px;
  line-height: 1.6;
  color: #6b7280;
}

.palette__list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.palette__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 10px;
  border: 1px solid #e5e7eb;
  border-radius: 10px;
  background: #fbfcfe;
  cursor: grab;
  user-select: none;
  transition:
    border-color 0.15s ease,
    box-shadow 0.15s ease,
    transform 0.15s ease;
}

.palette__item:hover {
  border-color: #bfdbfe;
  box-shadow: 0 4px 12px rgba(37, 99, 235, 0.08);
  transform: translateY(-1px);
}

.palette__item:active {
  cursor: grabbing;
}

.palette__item--muted {
  opacity: 0.55;
  cursor: not-allowed;
  transform: none;
  box-shadow: none;
}

.palette__badge {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 26px;
  height: 26px;
  border-radius: 8px;
  color: #ffffff;
  font-size: 12px;
  font-weight: 700;
  flex: none;
}

.palette__text {
  display: flex;
  flex-direction: column;
  min-width: 0;
}

.palette__text strong {
  font-size: 13px;
  font-weight: 600;
  color: #111827;
}

.palette__text small {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: #9ca3af;
}

.palette__note {
  margin: 10px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: #b45309;
}
</style>
