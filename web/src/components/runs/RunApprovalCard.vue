<script setup lang="ts">
import { computed, ref } from 'vue'

import type { UserRole } from '@/api/http'
import type { WorkflowNodeRun } from '@/api/runs'
import { formatDateTime } from '@/utils/datetime'

const props = defineProps<{
  node: WorkflowNodeRun
  role: UserRole | null
  submitting?: boolean
}>()

const emit = defineEmits<{
  approve: [comment: string]
  reject: [comment: string]
}>()

const comment = ref('')

const approval = computed(() => props.node.approval)
const title = computed(() => approval.value?.title?.trim() || props.node.node_id)
const approvers = computed(() => approval.value?.approvers ?? [])
/** 管理员不是隐式审批人：角色必须显式出现在 approvers 中。 */
const canDecide = computed(
  () => props.role !== null && approvers.value.includes(props.role),
)
const approversLabel = computed(() =>
  approvers.value.length > 0 ? approvers.value.join('、') : '未配置审批人',
)

function submit(approve: boolean): void {
  if (props.submitting) {
    return
  }
  if (approve) {
    emit('approve', comment.value)
  } else {
    emit('reject', comment.value)
  }
}
</script>

<template>
  <el-card shadow="never" class="approval-card" data-test="approval-card">
    <template #header>
      <div class="approval-card__header">
        <span class="approval-card__title">{{ title }}</span>
        <el-tag type="warning" effect="light" size="small">待审批</el-tag>
      </div>
    </template>

    <dl class="approval-card__meta">
      <div>
        <dt>审批人</dt>
        <dd>{{ approversLabel }}</dd>
      </div>
      <div>
        <dt>创建时间</dt>
        <dd>{{ formatDateTime(approval?.created_at ?? null) }}</dd>
      </div>
      <div>
        <dt>过期时间</dt>
        <dd>{{ formatDateTime(approval?.expires_at ?? null) }}</dd>
      </div>
    </dl>

    <el-input
      v-model="comment"
      type="textarea"
      :rows="3"
      maxlength="2000"
      show-word-limit
      data-test="approval-comment"
      placeholder="可选：填写审批意见"
      :disabled="submitting"
    />

    <div v-if="canDecide" class="approval-card__actions">
      <el-button
        type="danger"
        plain
        data-test="reject-run"
        :loading="submitting"
        @click="submit(false)"
      >
        拒绝
      </el-button>
      <el-button
        type="success"
        data-test="approve-run"
        :loading="submitting"
        @click="submit(true)"
      >
        批准
      </el-button>
    </div>

    <el-alert
      v-else
      class="approval-card__forbidden"
      type="warning"
      :closable="false"
      show-icon
      data-test="approval-forbidden"
      title="当前角色不在审批人列表，仅可查看审批信息。"
    />
  </el-card>
</template>

<style scoped>
.approval-card {
  border-radius: 12px;
}

.approval-card__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.approval-card__title {
  font-size: 15px;
  font-weight: 600;
  color: #111827;
}

.approval-card__meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px 28px;
  margin: 0 0 12px;
}

.approval-card__meta div {
  display: flex;
  gap: 6px;
  font-size: 13px;
}

.approval-card__meta dt {
  color: #94a3b8;
}

.approval-card__meta dd {
  margin: 0;
  color: #334155;
}

.approval-card__actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  margin-top: 12px;
}

.approval-card__forbidden {
  margin-top: 12px;
}
</style>
