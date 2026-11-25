<script setup lang="ts">
import { computed } from 'vue'

import type { WorkflowNodeRun } from '@/api/runs'
import { formatDateTime } from '@/utils/datetime'
import { formatJson } from '@/utils/report'
import {
  formatDurationBetween,
  nodeStatusLabel,
  nodeStatusTagType,
  nodeTypeLabel,
} from '@/utils/status'

const props = defineProps<{
  node: WorkflowNodeRun
}>()

const emit = defineEmits<{
  openTrace: [spanId: string]
}>()

const approval = computed(() => props.node.approval)
const approversLabel = computed(() => {
  const approvers = approval.value?.approvers ?? []
  return approvers.length > 0 ? approvers.join('、') : '未配置'
})

function openSpan(): void {
  if (props.node.span_id) {
    emit('openTrace', props.node.span_id)
  }
}
</script>

<template>
  <el-card shadow="never" class="node-detail" data-test="run-node-detail">
    <template #header>
      <div class="node-detail__header">
        <div class="node-detail__title">
          <span class="node-detail__id">{{ node.node_id }}</span>
          <el-tag size="small" type="info" effect="plain">
            {{ nodeTypeLabel(node.node_type) }}
          </el-tag>
          <el-tag size="small" :type="nodeStatusTagType(node.status)" effect="light">
            {{ nodeStatusLabel(node.status) }}
          </el-tag>
          <span class="node-detail__muted">第 {{ node.attempt }} 次尝试</span>
        </div>
        <el-button
          v-if="node.span_id"
          link
          type="primary"
          data-test="open-node-trace"
          @click="openSpan"
        >
          查看 Span Trace
        </el-button>
        <span v-else class="node-detail__muted" data-test="node-span-missing">Span 未采集</span>
      </div>
    </template>

    <div class="node-detail__meta">
      <span>开始：{{ formatDateTime(node.started_at) }}</span>
      <span>结束：{{ formatDateTime(node.finished_at) }}</span>
      <span>
        耗时：{{
          formatDurationBetween(node.started_at, node.finished_at, {
            live: node.status === 'running',
          })
        }}
      </span>
      <span>心跳：{{ formatDateTime(node.heartbeat_at) }}</span>
      <span>span_id：{{ node.span_id ?? '未采集' }}</span>
    </div>

    <h4 class="node-detail__section-title">输入</h4>
    <pre class="node-detail__json" data-test="node-input">{{ formatJson(node.input) }}</pre>

    <h4 class="node-detail__section-title">输出</h4>
    <pre class="node-detail__json" data-test="node-output">{{ formatJson(node.output) }}</pre>

    <template v-if="node.error">
      <h4 class="node-detail__section-title">错误</h4>
      <pre class="node-detail__json node-detail__json--error" data-test="node-error">{{
        node.error
      }}</pre>
    </template>

    <template v-if="approval">
      <h4 class="node-detail__section-title">审批信息（只读）</h4>
      <dl class="node-detail__approval">
        <div>
          <dt>标题</dt>
          <dd>{{ approval.title ?? '未上报' }}</dd>
        </div>
        <div>
          <dt>审批人</dt>
          <dd>{{ approversLabel }}</dd>
        </div>
        <div>
          <dt>创建时间</dt>
          <dd>{{ formatDateTime(approval.created_at ?? null) }}</dd>
        </div>
        <div>
          <dt>过期时间</dt>
          <dd>{{ formatDateTime(approval.expires_at ?? null) }}</dd>
        </div>
        <div>
          <dt>决定</dt>
          <dd>{{ approval.decision ?? '待决定' }}</dd>
        </div>
        <div>
          <dt>处理人</dt>
          <dd>{{ approval.by ?? '—' }}</dd>
        </div>
        <div>
          <dt>处理时间</dt>
          <dd>{{ formatDateTime(approval.at ?? null) }}</dd>
        </div>
        <div>
          <dt>意见</dt>
          <dd>{{ approval.comment ?? '—' }}</dd>
        </div>
      </dl>
    </template>
  </el-card>
</template>

<style scoped>
.node-detail {
  margin-top: 12px;
  border-radius: 12px;
}

.node-detail__header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.node-detail__title {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.node-detail__id {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 14px;
  font-weight: 600;
  color: #111827;
}

.node-detail__muted {
  color: #94a3b8;
  font-size: 12px;
}

.node-detail__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 6px 18px;
  color: #6b7280;
  font-size: 12px;
}

.node-detail__section-title {
  margin: 14px 0 6px;
  font-size: 13px;
  font-weight: 600;
  color: #374151;
}

.node-detail__json {
  margin: 0;
  max-height: 240px;
  overflow: auto;
  padding: 10px 12px;
  border-radius: 8px;
  background: #f8fafc;
  border: 1px solid #e5e7eb;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  line-height: 1.7;
  color: #1f2937;
  white-space: pre-wrap;
  word-break: break-word;
}

.node-detail__json--error {
  background: #fef2f2;
  border-color: #fecaca;
  color: #b91c1c;
}

.node-detail__approval {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 8px 20px;
  margin: 0;
  font-size: 12px;
}

.node-detail__approval div {
  display: flex;
  gap: 6px;
}

.node-detail__approval dt {
  flex-shrink: 0;
  color: #94a3b8;
}

.node-detail__approval dd {
  margin: 0;
  color: #334155;
  word-break: break-word;
}
</style>
