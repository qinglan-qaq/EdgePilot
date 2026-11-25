<script setup lang="ts">
import type { ValidationIssue, ValidationReport } from '@/api/workflows'

defineProps<{
  /** 最近一次校验报告；null 表示尚未校验。 */
  report: ValidationReport | null
  /** 校验接口本身的错误（非 DSL 校验失败）。 */
  error: string | null
  /** 是否正在请求校验接口。 */
  validating: boolean
}>()

const emit = defineEmits<{
  validate: []
  selectNode: [nodeId: string]
}>()

/** 点击问题时定位到对应节点；图级问题没有 node_id，不可定位。 */
function handleIssueClick(issue: ValidationIssue): void {
  if (issue.node_id) {
    emit('selectNode', issue.node_id)
  }
}
</script>

<template>
  <el-card shadow="never" class="validation" :body-style="{ padding: '12px' }">
    <header class="validation__header">
      <h3>校验结果</h3>
      <el-button
        size="small"
        type="primary"
        plain
        :loading="validating"
        data-test="validate-workflow-panel"
        @click="emit('validate')"
      >
        校验
      </el-button>
    </header>

    <div v-if="validating" class="validation__loading" data-test="validation-loading">
      正在校验当前草稿…
    </div>

    <el-alert
      v-else-if="error"
      :title="error"
      type="error"
      :closable="false"
      show-icon
      data-test="validation-error"
    />

    <template v-else-if="report">
      <div class="validation__summary" data-test="validation-summary">
        <el-tag
          :type="report.valid ? 'success' : 'danger'"
          effect="dark"
          data-test="validation-status"
        >
          {{ report.valid ? '校验通过' : '校验未通过' }}
        </el-tag>
        <span>节点 {{ report.node_count }}</span>
        <span>连线 {{ report.edge_count }}</span>
        <span>入口 {{ report.entry_node ?? '无' }}</span>
        <span>结束 {{ report.end_nodes.length > 0 ? report.end_nodes.join(', ') : '无' }}</span>
      </div>

      <p class="validation__order" data-test="validation-order">
        拓扑顺序：
        {{ report.topological_order ? report.topological_order.join(' → ') : '不可用（存在环或结构错误）' }}
      </p>

      <div v-if="report.errors.length > 0" class="validation__issues" data-test="validation-errors">
        <p class="validation__issues-title">错误</p>
        <ul>
          <li v-for="issue in report.errors" :key="`${issue.code}-${issue.node_id}-${issue.message}`">
            <button
              type="button"
              class="validation__issue validation__issue--error"
              :class="{ 'validation__issue--clickable': issue.node_id }"
              @click="handleIssueClick(issue)"
            >
              <strong>{{ issue.code }}</strong>
              <span>{{ issue.message }}</span>
              <em v-if="issue.node_id">[{{ issue.node_id }}]</em>
            </button>
          </li>
        </ul>
      </div>

      <div v-if="report.warnings.length > 0" class="validation__issues" data-test="validation-warnings">
        <p class="validation__issues-title">警告</p>
        <ul>
          <li v-for="issue in report.warnings" :key="`${issue.code}-${issue.node_id}-${issue.message}`">
            <button
              type="button"
              class="validation__issue validation__issue--warning"
              :class="{ 'validation__issue--clickable': issue.node_id }"
              @click="handleIssueClick(issue)"
            >
              <strong>{{ issue.code }}</strong>
              <span>{{ issue.message }}</span>
              <em v-if="issue.node_id">[{{ issue.node_id }}]</em>
            </button>
          </li>
        </ul>
      </div>

      <p v-if="report.valid && report.warnings.length === 0" class="validation__hint">
        当前草稿通过结构、引用与可达性校验。
      </p>
    </template>

    <p v-else class="validation__hint">
      点击“校验”检查当前画布草稿；非法结果同样会展示为真实报告。
    </p>
  </el-card>
</template>

<style scoped>
.validation {
  border-radius: 12px;
}

.validation__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}

.validation__header h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: #111827;
}

.validation__loading {
  padding: 12px 0;
  font-size: 12px;
  color: #6b7280;
}

.validation__summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  font-size: 12px;
  color: #4b5563;
}

.validation__order {
  margin: 10px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: #6b7280;
  word-break: break-word;
}

.validation__issues {
  margin-top: 10px;
}

.validation__issues-title {
  margin: 0 0 4px;
  font-size: 12px;
  font-weight: 600;
  color: #374151;
}

.validation__issues ul {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.validation__issue {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  width: 100%;
  padding: 6px 8px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #fbfcfe;
  text-align: left;
  font-size: 11px;
  line-height: 1.6;
}

.validation__issue strong {
  font-weight: 600;
}

.validation__issue em {
  font-style: normal;
  color: #6b7280;
}

.validation__issue--error {
  border-color: #fecaca;
  background: #fef2f2;
  color: #991b1b;
}

.validation__issue--warning {
  border-color: #fde68a;
  background: #fffbeb;
  color: #92400e;
}

.validation__issue--clickable {
  cursor: pointer;
}

.validation__issue--clickable:hover {
  border-color: #93c5fd;
}

.validation__hint {
  margin: 8px 0 0;
  font-size: 11px;
  line-height: 1.6;
  color: #6b7280;
}
</style>
