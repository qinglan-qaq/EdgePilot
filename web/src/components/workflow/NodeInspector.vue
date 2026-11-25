<script setup lang="ts">
import { computed, reactive, watch } from 'vue'

import type { AgentPickerItem, ToolPickerItem } from '@/api/workflows'

import {
  APPROVER_ROLES,
  coerceInputValue,
  defaultInputValue,
  displayInputValue,
  extractInputSchema,
  MAX_TIMEOUT_MS,
  MIN_TIMEOUT_MS,
  nodeTypeMeta,
  stableStringify,
  type ApproverRole,
  type FlowNode,
  type JsonSchemaProperty,
} from './editor-model'

const props = defineProps<{
  /** 当前选中的画布节点；null 表示未选中。 */
  node: FlowNode | null
  /** 可选 Agent 列表；加载失败时为空数组。 */
  agents: AgentPickerItem[]
  /** 可选工具列表；加载失败时为空数组。 */
  tools: ToolPickerItem[]
  /** 只读角色时禁用全部表单。 */
  disabled: boolean
  /** Agent / Tool 选择器加载失败的说明。 */
  pickerWarning: string | null
  /** 当前节点的上游节点 id，用于表达式提示。 */
  upstreamIds: string[]
}>()

const emit = defineEmits<{
  patch: [nodeId: string, patch: Record<string, unknown | undefined>]
}>()

/** 读取 JSON 对象。 */
function asRecord(value: unknown): Record<string, unknown> | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return null
  }
  return value as Record<string, unknown>
}

/** 读取有限数值。 */
function numericValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

/** 判断字符串是否为合法的审批角色。 */
function isApproverRole(value: unknown): value is ApproverRole {
  return typeof value === 'string' && (APPROVER_ROLES as readonly string[]).includes(value)
}

const nodeId = computed(() => props.node?.id ?? '')
const nodeType = computed(() => props.node?.data.nodeType ?? '')
const meta = computed(() => nodeTypeMeta(nodeType.value))
const fields = computed<Record<string, unknown>>(() => props.node?.data.fields ?? {})

const isAgent = computed(() => nodeType.value === 'agent')
const isTool = computed(() => nodeType.value === 'tool')
const isCondition = computed(() => nodeType.value === 'condition')
const isApproval = computed(() => nodeType.value === 'approval')

const timeoutValue = computed(
  () => numericValue(fields.value.timeout_ms) ?? meta.value?.defaultTimeoutMs ?? 30_000,
)
const maxRetriesValue = computed(() => numericValue(fields.value.max_retries) ?? 2)
const agentName = computed(() => {
  const value = fields.value.agent
  return typeof value === 'string' ? value : ''
})
const toolId = computed(() => {
  const value = fields.value.tool
  return typeof value === 'string' ? value : ''
})
const conditionExpression = computed(() => {
  const value = fields.value.expression
  return typeof value === 'string' ? value : ''
})
const approvalTitle = computed(() => {
  const value = fields.value.title
  return typeof value === 'string' ? value : ''
})
const approvers = computed<ApproverRole[]>(() => {
  const value = fields.value.approvers
  return Array.isArray(value) ? value.filter(isApproverRole) : []
})

const selectedAgent = computed(
  () => props.agents.find((agent) => agent.name === agentName.value) ?? null,
)
const selectedTool = computed(
  () => props.tools.find((tool) => tool.tool_id === toolId.value) ?? null,
)

const inputMap = computed<Record<string, unknown>>(() => {
  const value = asRecord(fields.value.input_map)
  return value ?? {}
})

/** 从 Agent config.input_schema 或 Tool input_schema 提取参数定义。 */
const schema = computed(() => {
  if (isAgent.value) {
    const config = asRecord(selectedAgent.value?.config)
    return extractInputSchema(config?.['input_schema'])
  }
  if (isTool.value) {
    return extractInputSchema(selectedTool.value?.input_schema)
  }
  return null
})

const schemaEntries = computed<
  Array<{ key: string; prop: JsonSchemaProperty; required: boolean }>
>(() => {
  const view = schema.value
  if (!view) {
    return []
  }
  return Object.entries(view.properties).map(([key, prop]) => ({
    key,
    prop,
    required: view.required.includes(key),
  }))
})
const schemaKeys = computed(() => new Set(schemaEntries.value.map((entry) => entry.key)))
const extraEntries = computed(() =>
  Object.entries(inputMap.value)
    .filter(([key]) => !schemaKeys.value.has(key))
    .map(([key, value]) => ({ key, value })),
)
const hasMappingTarget = computed(() => (isAgent.value ? Boolean(agentName.value) : Boolean(toolId.value)))

/** 输入映射的本地文本草稿；提交时再按 schema 类型转换。 */
const mapDrafts = reactive<Record<string, string>>({})

/** 同步 input_map 到本地草稿，避免数字/布尔值在输入过程中被反复格式化。 */
function syncMapDrafts(): void {
  const keys = new Set([
    ...schemaEntries.value.map((entry) => entry.key),
    ...Object.keys(inputMap.value),
  ])
  for (const key of keys) {
    mapDrafts[key] = displayInputValue(inputMap.value[key])
  }
  for (const key of Object.keys(mapDrafts)) {
    if (!keys.has(key)) {
      delete mapDrafts[key]
    }
  }
}

watch(
  () => [
    props.node?.id ?? '',
    stableStringify(inputMap.value),
    schemaEntries.value.map((entry) => entry.key).join('|'),
  ],
  () => syncMapDrafts(),
  { immediate: true },
)

/** 提交一个字段修改到父组件。 */
function patchFields(patch: Record<string, unknown | undefined>): void {
  if (props.disabled || !props.node) {
    return
  }
  emit('patch', props.node.id, patch)
}

function onTimeoutChange(value: number | null | undefined): void {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return
  }
  patchFields({
    timeout_ms: Math.min(MAX_TIMEOUT_MS, Math.max(MIN_TIMEOUT_MS, Math.trunc(value))),
  })
}

function onMaxRetriesChange(value: number | null | undefined): void {
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    return
  }
  patchFields({ max_retries: Math.max(0, Math.trunc(value)) })
}

function onAgentChange(value: unknown): void {
  patchFields({ agent: typeof value === 'string' ? value : '' })
}

function onToolChange(value: unknown): void {
  patchFields({ tool: typeof value === 'string' ? value : '' })
}

function onExpressionInput(value: string): void {
  patchFields({ expression: value })
}

function onTitleInput(value: string): void {
  patchFields({ title: value })
}

function onApproversChange(value: Array<string | number | boolean>): void {
  patchFields({ approvers: value.filter(isApproverRole) })
}

/** 勾选/取消勾选某个 schema 字段。 */
function toggleMapKey(key: string, checked: boolean): void {
  if (props.disabled) {
    return
  }
  const next = { ...inputMap.value }
  if (checked) {
    const prop = schema.value?.properties[key]
    next[key] = defaultInputValue(prop?.type)
  } else {
    delete next[key]
  }
  mapDrafts[key] = checked ? displayInputValue(next[key]) : ''
  patchFields({ input_map: Object.keys(next).length > 0 ? next : undefined })
}

/** 提交某个映射值；保留 $ 表达式，其余按 schema 类型转换。 */
function commitMapValue(key: string): void {
  if (props.disabled) {
    return
  }
  const prop = schema.value?.properties[key]
  const next = {
    ...inputMap.value,
    [key]: coerceInputValue(mapDrafts[key] ?? '', prop?.type),
  }
  patchFields({ input_map: next })
}

/** 删除一个 schema 之外的已有映射键。 */
function removeMapKey(key: string): void {
  const next = { ...inputMap.value }
  delete next[key]
  mapDrafts[key] = ''
  patchFields({ input_map: Object.keys(next).length > 0 ? next : undefined })
}

/** Agent 下拉文案；未启用仍展示但标注状态。 */
function agentOptionLabel(agent: AgentPickerItem): string {
  return agent.status === 'active' ? agent.name : `${agent.name}（${agent.status}）`
}

/** 工具下拉文案；禁用工具仍展示但标注。 */
function toolOptionLabel(tool: ToolPickerItem): string {
  return tool.enabled ? tool.name : `${tool.name}（已禁用）`
}

/** 角色中文名。 */
function roleLabel(role: ApproverRole): string {
  if (role === 'viewer') {
    return '查看者 viewer'
  }
  if (role === 'operator') {
    return '操作员 operator'
  }
  return '管理员 admin'
}

const upstreamLabel = computed(() =>
  props.upstreamIds.length > 0 ? props.upstreamIds.join('、') : '无（只能引用 $input）',
)
</script>

<template>
  <el-card shadow="never" class="inspector" :body-style="{ padding: '12px' }">
    <header class="inspector__header">
      <h3>节点检查器</h3>
      <el-tag v-if="node && meta" size="small" effect="plain">{{ meta.label }}</el-tag>
    </header>

    <el-empty
      v-if="!node"
      description="在画布中选择一个节点后编辑属性"
      :image-size="64"
      class="inspector__empty"
    />

    <div v-else class="inspector__body">
      <el-alert
        v-if="pickerWarning && (isAgent || isTool)"
        :title="pickerWarning"
        type="warning"
        :closable="false"
        show-icon
        class="inspector__alert"
      />
      <el-alert
        v-if="!meta"
        :title="`未知节点类型：${nodeType}`"
        description="保存后仍会被后端校验拒绝，请修复 DSL。"
        type="warning"
        :closable="false"
        show-icon
        class="inspector__alert"
      />

      <section class="inspector__section">
        <header class="inspector__section-header">
          <span>基础</span>
          <small>id 用于表达式引用</small>
        </header>

        <div class="inspector__field">
          <label>节点 id</label>
          <el-input :model-value="nodeId" disabled />
          <p class="inspector__hint">
            表达式通过 <code>${{ nodeId }}.output</code> 引用该节点；本编辑器不提供重命名，
            以免已有表达式失效。
          </p>
        </div>

        <div class="inspector__field">
          <label>类型</label>
          <el-input :model-value="nodeType" disabled />
        </div>

        <div class="inspector__field">
          <label>超时 timeout_ms</label>
          <el-input-number
            :model-value="timeoutValue"
            :min="MIN_TIMEOUT_MS"
            :max="MAX_TIMEOUT_MS"
            :step="1000"
            :disabled="disabled"
            controls-position="right"
            class="inspector__number"
            @update:model-value="onTimeoutChange"
          />
          <p class="inspector__hint">允许范围 1..86400000 毫秒。</p>
        </div>
      </section>

      <section v-if="isAgent" class="inspector__section">
        <header class="inspector__section-header">
          <span>Agent</span>
          <small>值为 Agent 的 name</small>
        </header>

        <div class="inspector__field">
          <label>Agent</label>
          <el-select
            :model-value="agentName"
            :disabled="disabled"
            filterable
            clearable
            placeholder="请选择 Agent"
            @update:model-value="onAgentChange"
          >
            <el-option
              v-for="agent in agents"
              :key="agent.name"
              :label="agentOptionLabel(agent)"
              :value="agent.name"
            />
          </el-select>
          <p v-if="agents.length === 0" class="inspector__hint inspector__hint--warning">
            Agent 选择器不可用，可直接保留已有 name 或稍后重试。
          </p>
          <p v-else-if="selectedAgent && selectedAgent.status !== 'active'" class="inspector__hint inspector__hint--warning">
            该 Agent 当前状态为 {{ selectedAgent.status }}，发布校验会失败。
          </p>
        </div>

        <div class="inspector__field">
          <label>最大重试次数 max_retries</label>
          <el-input-number
            :model-value="maxRetriesValue"
            :min="0"
            :step="1"
            :precision="0"
            :disabled="disabled"
            controls-position="right"
            class="inspector__number"
            @update:model-value="onMaxRetriesChange"
          />
        </div>
      </section>

      <section v-if="isTool" class="inspector__section">
        <header class="inspector__section-header">
          <span>工具</span>
          <small>值为 tool_id</small>
        </header>

        <div class="inspector__field">
          <label>工具</label>
          <el-select
            :model-value="toolId"
            :disabled="disabled"
            filterable
            clearable
            placeholder="请选择工具"
            @update:model-value="onToolChange"
          >
            <el-option
              v-for="tool in tools"
              :key="tool.tool_id"
              :label="toolOptionLabel(tool)"
              :value="tool.tool_id"
            />
          </el-select>
          <p v-if="tools.length === 0" class="inspector__hint inspector__hint--warning">
            工具选择器不可用，可直接保留已有 tool_id 或稍后重试。
          </p>
          <p v-else-if="selectedTool && !selectedTool.enabled" class="inspector__hint inspector__hint--warning">
            该工具已禁用，发布校验会失败。
          </p>
        </div>

        <div class="inspector__field">
          <label>最大重试次数 max_retries</label>
          <el-input-number
            :model-value="maxRetriesValue"
            :min="0"
            :step="1"
            :precision="0"
            :disabled="disabled"
            controls-position="right"
            class="inspector__number"
            @update:model-value="onMaxRetriesChange"
          />
        </div>
      </section>

      <section v-if="isAgent || isTool" class="inspector__section">
        <header class="inspector__section-header">
          <span>参数映射 input_map</span>
          <small>字面量或 $ 表达式</small>
        </header>

        <p class="inspector__hint">
          值可以是标量字面量，也可以是以 <code>$</code> 开头的表达式，例如
          <code>$input.camera</code> 或 <code>$agent_1.output.score</code>。
        </p>

        <p v-if="!hasMappingTarget" class="inspector__hint inspector__hint--warning">
          请先选择 {{ isAgent ? 'Agent' : '工具' }}，再配置参数映射。
        </p>

        <template v-else>
          <div v-if="schemaEntries.length > 0" class="inspector__map">
            <div v-for="entry in schemaEntries" :key="entry.key" class="inspector__map-row">
              <el-checkbox
                :model-value="Object.prototype.hasOwnProperty.call(inputMap, entry.key)"
                :disabled="disabled"
                @update:model-value="toggleMapKey(entry.key, Boolean($event))"
              >
                <code>{{ entry.key }}</code>
                <span class="inspector__map-type">{{ entry.prop.type ?? 'any' }}</span>
                <span v-if="entry.required" class="inspector__map-required">必填</span>
              </el-checkbox>
              <el-input
                v-if="Object.prototype.hasOwnProperty.call(inputMap, entry.key)"
                v-model="mapDrafts[entry.key]"
                :disabled="disabled"
                size="small"
                placeholder="字面量或 $ 表达式"
                @change="commitMapValue(entry.key)"
              />
              <p v-if="entry.prop.description" class="inspector__hint">
                {{ entry.prop.description }}
              </p>
            </div>
          </div>
          <p v-else class="inspector__hint inspector__hint--warning">
            未声明 input_schema，引擎将直接使用运行输入。
          </p>

          <div v-if="extraEntries.length > 0" class="inspector__map inspector__map--extra">
            <p class="inspector__hint">schema 之外的已有键：</p>
            <div v-for="entry in extraEntries" :key="entry.key" class="inspector__map-row">
              <code class="inspector__map-extra-key">{{ entry.key }}</code>
              <el-input
                v-model="mapDrafts[entry.key]"
                :disabled="disabled"
                size="small"
                placeholder="标量或 $ 表达式"
                @change="commitMapValue(entry.key)"
              />
              <el-button
                link
                type="danger"
                :disabled="disabled"
                data-test="remove-input-key"
                @click="removeMapKey(entry.key)"
              >
                删除
              </el-button>
            </div>
          </div>
        </template>
      </section>

      <section v-if="isCondition" class="inspector__section">
        <header class="inspector__section-header">
          <span>条件 expression</span>
          <small>仅允许受支持的表达式语法</small>
        </header>

        <div class="inspector__field">
          <el-input
            :model-value="conditionExpression"
            type="textarea"
            :rows="3"
            :disabled="disabled"
            placeholder="例如：$input.score >= 0.8 and $agent_1.output.result == true"
            @update:model-value="onExpressionInput"
          />
          <p class="inspector__hint">
            允许字段访问、比较、and/or/not、括号与标量字面量；只能引用
            <code>$input</code> 或上游节点的 <code>$&lt;node_id&gt;.output</code>。
          </p>
          <p class="inspector__hint">当前上游节点：{{ upstreamLabel }}</p>
        </div>
      </section>

      <section v-if="isApproval" class="inspector__section">
        <header class="inspector__section-header">
          <span>审批</span>
          <small>管理员不会自动成为审批人</small>
        </header>

        <div class="inspector__field">
          <label>标题 title</label>
          <el-input
            :model-value="approvalTitle"
            :disabled="disabled"
            placeholder="例如：请确认高温告警处置"
            @update:model-value="onTitleInput"
          />
        </div>

        <div class="inspector__field">
          <label>审批角色 approvers</label>
          <el-checkbox-group
            :model-value="approvers"
            :disabled="disabled"
            @update:model-value="onApproversChange"
          >
            <el-checkbox v-for="role in APPROVER_ROLES" :key="role" :value="role">
              {{ roleLabel(role) }}
            </el-checkbox>
          </el-checkbox-group>
          <p class="inspector__hint">
            必须至少选择一个审批角色；管理员不会隐式获得审批权，需要显式勾选 admin。
          </p>
        </div>
      </section>

      <section v-if="nodeType === 'start' || nodeType === 'end'" class="inspector__section">
        <p class="inspector__hint">
          {{ nodeType === 'start' ? 'start' : 'end' }} 节点只包含 id、type 与 timeout_ms。
        </p>
      </section>
    </div>
  </el-card>
</template>

<style scoped>
.inspector {
  border-radius: 12px;
}

.inspector__header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.inspector__header h3 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
  color: #111827;
}

.inspector__empty {
  padding: 12px 0;
}

.inspector__body {
  display: flex;
  flex-direction: column;
  gap: 14px;
}

.inspector__alert {
  margin-bottom: 0;
}

.inspector__section {
  padding-bottom: 12px;
  border-bottom: 1px solid #eef1f6;
}

.inspector__section:last-child {
  padding-bottom: 0;
  border-bottom: none;
}

.inspector__section-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.inspector__section-header span {
  font-size: 13px;
  font-weight: 600;
  color: #111827;
}

.inspector__section-header small {
  font-size: 11px;
  color: #9ca3af;
}

.inspector__field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 10px;
}

.inspector__field:last-child {
  margin-bottom: 0;
}

.inspector__field > label {
  font-size: 12px;
  font-weight: 500;
  color: #4b5563;
}

.inspector__number {
  width: 100%;
}

.inspector__hint {
  margin: 0;
  font-size: 11px;
  line-height: 1.6;
  color: #6b7280;
}

.inspector__hint--warning {
  color: #b45309;
}

.inspector__hint code {
  padding: 1px 4px;
  border-radius: 4px;
  background: #f3f4f6;
  color: #374151;
}

.inspector__map {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.inspector__map--extra {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed #e5e7eb;
}

.inspector__map-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.inspector__map-row :deep(.el-checkbox) {
  height: auto;
  margin-right: 0;
}

.inspector__map-type {
  margin-left: 6px;
  font-size: 10px;
  color: #9ca3af;
}

.inspector__map-required {
  margin-left: 6px;
  font-size: 10px;
  color: #dc2626;
}

.inspector__map-extra-key {
  font-size: 11px;
  color: #374151;
}
</style>
