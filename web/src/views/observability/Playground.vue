<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'

import {
  getPromptTemplate,
  listPromptTemplates,
  runPromptPlayground,
  type PromptPlaygroundResult,
  type PromptTemplateDetail,
  type PromptTemplateSummary,
  type PromptVersion,
} from '@/api/observability'
import { ApiError } from '@/api/http'
import AsyncState from '@/components/common/AsyncState.vue'
import JsonViewer from '@/components/observability/JsonViewer.vue'
import { useAuthStore } from '@/stores/auth'

const route = useRoute()
const router = useRouter()
const auth = useAuthStore()

const canExecute = computed(
  () => auth.role === 'admin' || auth.role === 'operator',
)

const templates = ref<PromptTemplateSummary[]>([])
const templatesLoading = ref(false)
const templatesError = ref<string | null>(null)

const selectedPk = ref<number | null>(null)
const detail = ref<PromptTemplateDetail | null>(null)
const detailLoading = ref(false)
const detailError = ref<string | null>(null)

const selectedVersionNo = ref<number | null>(null)
const variableValues = ref<Record<string, string>>({})
const variablesJson = ref('{}')
const executeReal = ref(false)
const running = ref(false)
const runError = ref<string | null>(null)
const result = ref<PromptPlaygroundResult | null>(null)

const versions = computed<PromptVersion[]>(() => detail.value?.versions ?? [])
const selectedVersion = computed<PromptVersion | null>(
  () => versions.value.find((item) => item.version === selectedVersionNo.value) ?? null,
)

/** schema.properties 的键；缺失或非对象时不生成任何变量键。 */
const variableKeys = computed<string[]>(() => {
  const schema = selectedVersion.value?.variables_schema
  const properties = schema?.properties
  if (!properties || typeof properties !== 'object' || Array.isArray(properties)) {
    return []
  }
  return Object.keys(properties as Record<string, unknown>)
})

const requiredKeys = computed<string[]>(() => {
  const schema = selectedVersion.value?.variables_schema
  const required = schema?.required
  return Array.isArray(required) ? required.map((item) => String(item)) : []
})

const resultStatusType = computed(() => {
  if (!result.value) {
    return 'info'
  }
  return result.value.status === 'ok' ? 'success' : 'danger'
})

const renderFailed = computed(
  () => result.value !== null && result.value.status !== 'ok' && !result.value.response,
)

function messageOf(cause: unknown): string {
  if (cause instanceof ApiError) {
    return cause.message
  }
  if (cause instanceof Error && cause.message) {
    return cause.message
  }
  return '请求失败，请稍后重试'
}

function queryTemplatePk(): number | null {
  const raw = route.query.template
  const text = Array.isArray(raw) ? raw[0] : raw
  const parsed = Number(text)
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null
}

/** 根据当前版本 schema 重置表单与 JSON 文本，互不覆盖用户已编辑的 JSON。 */
function applySchema(version: PromptVersion | null): void {
  const values: Record<string, string> = {}
  if (version) {
    const schema = version.variables_schema
    const properties = schema?.properties
    if (properties && typeof properties === 'object' && !Array.isArray(properties)) {
      for (const key of Object.keys(properties as Record<string, unknown>)) {
        values[key] = ''
      }
    }
  }
  variableValues.value = values
  variablesJson.value = JSON.stringify(values, null, 2)
}

function selectVersion(versionNo: number | null): void {
  selectedVersionNo.value = versionNo
  result.value = null
  runError.value = null
  applySchema(selectedVersion.value)
}

async function loadDetail(): Promise<void> {
  if (selectedPk.value === null) {
    detail.value = null
    return
  }
  detailLoading.value = true
  detailError.value = null
  result.value = null
  runError.value = null
  try {
    const loaded = await getPromptTemplate(selectedPk.value)
    detail.value = loaded
    const active = loaded.versions.find(
      (item) => item.version === loaded.active_version,
    )
    selectVersion((active ?? loaded.versions[0])?.version ?? null)
  } catch (cause) {
    detail.value = null
    detailError.value = messageOf(cause)
  } finally {
    detailLoading.value = false
  }
}

async function selectTemplate(templatePk: number): Promise<void> {
  selectedPk.value = templatePk
  await loadDetail()
}

async function loadTemplates(): Promise<void> {
  templatesLoading.value = true
  templatesError.value = null
  try {
    const page = await listPromptTemplates({ page: 1, size: 200 })
    templates.value = page.items
    if (page.items.length === 0) {
      selectedPk.value = null
      detail.value = null
      return
    }
    const requested = queryTemplatePk()
    const target =
      page.items.find((item) => item.id === requested) ??
      page.items.find((item) => item.id === selectedPk.value) ??
      page.items[0]
    await selectTemplate(target.id)
  } catch (cause) {
    templates.value = []
    templatesError.value = messageOf(cause)
  } finally {
    templatesLoading.value = false
  }
}

onMounted(() => {
  void loadTemplates()
})

watch(
  () => route.query.template,
  () => {
    const requested = queryTemplatePk()
    if (requested !== null && requested !== selectedPk.value) {
      void selectTemplate(requested)
    }
  },
)

/** 表单字段变化时同步到 JSON 文本；直接编辑 JSON 不反向覆盖表单。 */
watch(
  variableValues,
  (value) => {
    variablesJson.value = JSON.stringify(value, null, 2)
  },
  { deep: true },
)

function parseVariables(): Record<string, unknown> | null {
  const text = variablesJson.value.trim()
  if (!text) {
    return {}
  }
  try {
    const parsed: unknown = JSON.parse(text)
    if (parsed === null) {
      return {}
    }
    if (typeof parsed !== 'object' || Array.isArray(parsed)) {
      return null
    }
    return parsed as Record<string, unknown>
  } catch {
    return null
  }
}

async function run(execute: boolean): Promise<void> {
  if (selectedPk.value === null || selectedVersionNo.value === null) {
    return
  }
  if (execute && !canExecute.value) {
    ElMessage.warning('当前角色无权真实执行')
    return
  }
  const variables = parseVariables()
  if (variables === null) {
    ElMessage.error('变量 JSON 必须是合法的 JSON 对象')
    return
  }

  running.value = true
  runError.value = null
  result.value = null
  try {
    result.value = await runPromptPlayground(selectedPk.value, {
      version: selectedVersionNo.value,
      variables,
      execute,
    })
  } catch (cause) {
    runError.value = messageOf(cause)
  } finally {
    running.value = false
  }
}

function openTrace(traceId: string): void {
  void router.push({
    name: 'observability-trace-detail',
    params: { traceId },
  })
}
</script>

<template>
  <section class="playground">
    <header class="playground__toolbar">
      <div>
        <h2 class="playground__title">Prompt Playground</h2>
        <p class="playground__subtitle">
          预览只做渲染；真实执行会调用 LLM 并写入执行记录，渲染失败不会触达模型。
        </p>
      </div>
      <el-button size="small" @click="loadTemplates">刷新模板</el-button>
    </header>

    <AsyncState
      :loading="templatesLoading"
      :error="templatesError"
      :empty="templates.length === 0"
      @retry="loadTemplates"
    >
      <template #empty>
        <el-empty description="暂无 Prompt 模板，请先在提示词页创建" />
      </template>

      <el-card shadow="never" class="playground__card">
        <div class="playground__selectors">
          <el-select
            :model-value="selectedPk"
            placeholder="选择模板"
            size="small"
            @update:model-value="selectTemplate"
          >
            <el-option
              v-for="template in templates"
              :key="template.id"
              :value="template.id"
              :label="`${template.template_name}（${template.template_id}）`"
            />
          </el-select>

          <el-select
            v-model="selectedVersionNo"
            placeholder="选择版本"
            size="small"
            :disabled="versions.length === 0"
            @change="selectVersion"
          >
            <el-option
              v-for="version in versions"
              :key="version.id"
              :value="version.version"
              :label="`v${version.version}${
                detail?.active_version === version.version ? '（当前发布）' : ''
              }`"
            />
          </el-select>
        </div>

        <AsyncState
          :loading="detailLoading"
          :error="detailError"
          @retry="loadDetail"
        >
          <template v-if="detail && selectedVersion">
            <div class="playground__grid">
              <div>
                <div class="playground__section-header">
                  <h4>变量</h4>
                  <span class="playground__muted">
                    schema.properties 的键；可直接编辑下方 JSON
                  </span>
                </div>

                <p v-if="variableKeys.length === 0" class="playground__muted">
                  当前版本未声明 variables_schema.properties 变量键。
                </p>

                <el-form v-else label-width="120px" size="small">
                  <el-form-item
                    v-for="key in variableKeys"
                    :key="key"
                    :label="key + (requiredKeys.includes(key) ? ' *' : '')"
                  >
                    <el-input v-model="variableValues[key]" />
                  </el-form-item>
                </el-form>

                <h5 class="playground__label">variables JSON</h5>
                <el-input
                  v-model="variablesJson"
                  type="textarea"
                  :rows="10"
                  data-test="playground-variables"
                />
                <el-button
                  class="playground__regen"
                  size="small"
                  text
                  @click="applySchema(selectedVersion)"
                >
                  按 schema 重新生成变量
                </el-button>
              </div>

              <div>
                <div class="playground__section-header">
                  <h4>执行</h4>
                  <span class="playground__muted">
                    v{{ selectedVersion.version }} · 版本记录 ID
                    {{ selectedVersion.id }}
                  </span>
                </div>

                <div class="playground__actions">
                  <el-button
                    type="primary"
                    size="small"
                    :loading="running"
                    @click="run(false)"
                  >
                    预览渲染
                  </el-button>

                  <template v-if="canExecute">
                    <el-checkbox v-model="executeReal">
                      真实执行（调用 LLM 并写入执行记录）
                    </el-checkbox>
                    <el-button
                      type="warning"
                      size="small"
                      :loading="running"
                      :disabled="!executeReal"
                      @click="run(true)"
                    >
                      真实执行
                    </el-button>
                  </template>
                  <span v-else class="playground__muted">
                    当前角色仅可预览，真实执行需要 admin / operator。
                  </span>
                </div>

                <el-alert
                  v-if="runError"
                  class="playground__alert"
                  type="error"
                  :title="runError"
                  :closable="false"
                  show-icon
                />

                <template v-if="result">
                  <el-alert
                    v-if="renderFailed"
                    class="playground__alert"
                    type="warning"
                    :title="`渲染未成功（status=${result.status}），后端未调用 LLM，也没有写入执行记录。`"
                    :closable="false"
                    show-icon
                  />

                  <el-alert
                    v-if="result.response && result.response.error_code"
                    class="playground__alert"
                    type="error"
                    :title="`LLM 调用失败：${result.response.error_code}`"
                    :closable="false"
                    show-icon
                  />

                  <el-descriptions :column="2" size="small" border>
                    <el-descriptions-item label="status">
                      <el-tag :type="resultStatusType" size="small" effect="light">
                        {{ result.status }}
                      </el-tag>
                    </el-descriptions-item>
                    <el-descriptions-item label="version">
                      v{{ result.version }}
                    </el-descriptions-item>
                    <el-descriptions-item label="prompt_hash">
                      <span class="playground__mono">{{ result.prompt_hash || '未记录' }}</span>
                    </el-descriptions-item>
                    <el-descriptions-item label="trace_id">
                      <el-button
                        v-if="result.trace_id"
                        size="small"
                        text
                        type="primary"
                        @click="openTrace(result.trace_id)"
                      >
                        查看 Trace 详情
                      </el-button>
                      <span v-else class="playground__muted">未记录</span>
                    </el-descriptions-item>
                  </el-descriptions>

                  <h5 class="playground__label">渲染 messages</h5>
                  <JsonViewer :value="result.messages" filename="messages.json" />

                  <template v-if="result.response">
                    <h5 class="playground__label">模型响应</h5>
                    <el-descriptions :column="2" size="small" border>
                      <el-descriptions-item label="provider">
                        {{ result.response.provider ?? '未记录' }}
                      </el-descriptions-item>
                      <el-descriptions-item label="model">
                        {{ result.response.model_name ?? '未记录' }}
                      </el-descriptions-item>
                      <el-descriptions-item label="finish_reason">
                        {{ result.response.finish_reason ?? '未记录' }}
                      </el-descriptions-item>
                      <el-descriptions-item label="cost">
                        {{ result.response.cost ?? '费用不可用' }}
                      </el-descriptions-item>
                      <el-descriptions-item label="request_id">
                        {{ result.response.request_id ?? '未记录' }}
                      </el-descriptions-item>
                      <el-descriptions-item label="degraded">
                        {{ result.response.degraded === null ? '未记录' : result.response.degraded }}
                      </el-descriptions-item>
                    </el-descriptions>

                    <h5 class="playground__label">content</h5>
                    <pre class="playground__pre">{{ result.response.content ?? '未记录' }}</pre>

                    <h5 class="playground__label">usage</h5>
                    <JsonViewer :value="result.response.usage" filename="usage.json" />
                  </template>
                </template>
              </div>
            </div>
          </template>
        </AsyncState>
      </el-card>
    </AsyncState>
  </section>
</template>

<style scoped>
.playground {
  width: 100%;
}

.playground__toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  margin-bottom: 16px;
  padding: 18px 20px;
  border-radius: 12px;
  background: #ffffff;
  box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
}

.playground__title {
  margin: 0;
  color: #111827;
  font-size: 18px;
  font-weight: 600;
}

.playground__subtitle {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
}

.playground__card {
  border-radius: 12px;
}

.playground__selectors {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-bottom: 16px;
}

.playground__grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 20px;
}

.playground__section-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.playground__section-header h4 {
  margin: 0;
  color: #111827;
  font-size: 14px;
  font-weight: 600;
}

.playground__muted {
  color: #94a3b8;
  font-size: 12px;
}

.playground__label {
  margin: 16px 0 6px;
  color: #334155;
  font-size: 12px;
  font-weight: 600;
}

.playground__regen {
  margin-top: 6px;
}

.playground__actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-top: 12px;
}

.playground__alert {
  margin-top: 12px;
}

.playground__mono {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
}

.playground__pre {
  margin: 0;
  max-height: 240px;
  overflow: auto;
  padding: 10px 12px;
  border: 1px solid #e5e7eb;
  border-radius: 8px;
  background: #f8fafc;
  color: #334155;
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  line-height: 1.7;
  white-space: pre-wrap;
  word-break: break-word;
}

@media (max-width: 1100px) {
  .playground__grid {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
