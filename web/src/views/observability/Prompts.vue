<script setup lang="ts">
import { computed, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'

import {
  addPromptVersion,
  createPromptTemplate,
  publishPromptVersion,
  rollbackPromptVersion,
  type AddPromptVersionPayload,
  type PromptTemplateSummary,
  type PromptVersion,
} from '@/api/observability'
import { ApiError } from '@/api/http'
import AsyncState from '@/components/common/AsyncState.vue'
import JsonViewer from '@/components/observability/JsonViewer.vue'
import PromptDiff from '@/components/observability/PromptDiff.vue'
import { useAuthStore } from '@/stores/auth'
import { useObservabilityStore } from '@/stores/observability'
import { formatDateTime } from '@/utils/datetime'

const router = useRouter()
const auth = useAuthStore()
const store = useObservabilityStore()

const isAdmin = computed(() => auth.role === 'admin')

const page = ref(1)
const pageSize = 20
const selectedPk = ref<number | null>(null)
const selectedVersionNo = ref<number | null>(null)
const compareLeft = ref<number | null>(null)
const compareRight = ref<number | null>(null)
const executionsPage = ref(1)
const executionsSize = 10

const templates = computed(() => store.promptTemplates)
const detail = computed(() => store.promptTemplate)
const versions = computed<PromptVersion[]>(() => detail.value?.versions ?? [])
const selectedVersion = computed<PromptVersion | null>(
  () => versions.value.find((item) => item.version === selectedVersionNo.value) ?? null,
)
const leftVersion = computed<PromptVersion | null>(
  () => versions.value.find((item) => item.version === compareLeft.value) ?? null,
)
const rightVersion = computed<PromptVersion | null>(
  () => versions.value.find((item) => item.version === compareRight.value) ?? null,
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

function versionOptionLabel(version: PromptVersion): string {
  const active = detail.value?.active_version === version.version ? '（当前发布）' : ''
  return `v${version.version}${active}`
}

/** 从后端返回的版本列表初始化选中项与对比项（列表按版本号倒序）。 */
function resetVersionSelection(): void {
  const list = versions.value
  if (list.length === 0) {
    selectedVersionNo.value = null
    compareLeft.value = null
    compareRight.value = null
    return
  }
  const active = list.find((item) => item.version === detail.value?.active_version)
  selectedVersionNo.value = (active ?? list[0]).version
  compareRight.value = list[0].version
  compareLeft.value = list.length > 1 ? list[1].version : null
}

async function loadExecutions(): Promise<void> {
  if (selectedPk.value === null) {
    return
  }
  await store.loadPromptExecutions(selectedPk.value, {
    page: executionsPage.value,
    size: executionsSize,
  })
}

async function loadDetail(): Promise<void> {
  if (selectedPk.value === null) {
    return
  }
  await Promise.all([store.loadPromptTemplate(selectedPk.value), loadExecutions()])
  if (!store.promptTemplateError) {
    resetVersionSelection()
  }
}

async function selectTemplate(templatePk: number): Promise<void> {
  selectedPk.value = templatePk
  selectedVersionNo.value = null
  compareLeft.value = null
  compareRight.value = null
  executionsPage.value = 1
  await loadDetail()
}

async function loadTemplates(): Promise<void> {
  await store.loadPromptTemplates({ page: page.value, size: pageSize })
  if (store.promptTemplatesError) {
    return
  }
  const current = templates.value.find((item) => item.id === selectedPk.value)
  if (current) {
    return
  }
  if (templates.value.length > 0) {
    await selectTemplate(templates.value[0].id)
  } else {
    selectedPk.value = null
    selectedVersionNo.value = null
    compareLeft.value = null
    compareRight.value = null
  }
}

onMounted(() => {
  void loadTemplates()
})

function handleRowClick(row: PromptTemplateSummary): void {
  if (row.id !== selectedPk.value) {
    void selectTemplate(row.id)
  }
}

function handlePageChange(nextPage: number): void {
  page.value = nextPage
  void loadTemplates()
}

function handleExecutionsPageChange(nextPage: number): void {
  executionsPage.value = nextPage
  void loadExecutions()
}

function openPlayground(): void {
  if (selectedPk.value === null) {
    return
  }
  void router.push({
    name: 'observability-prompt-playground',
    query: { template: String(selectedPk.value) },
  })
}

function openTrace(traceId: string): void {
  void router.push({
    name: 'observability-trace-detail',
    params: { traceId },
  })
}

// ---- 创建模板 -----------------------------------------------------------

const createVisible = ref(false)
const createSubmitting = ref(false)
const createForm = reactive({
  template_id: '',
  template_name: '',
  description: '',
  category: '',
  owner: '',
})

function openCreate(): void {
  createForm.template_id = ''
  createForm.template_name = ''
  createForm.description = ''
  createForm.category = ''
  createForm.owner = ''
  createVisible.value = true
}

async function submitCreate(): Promise<void> {
  const templateId = createForm.template_id.trim()
  const templateName = createForm.template_name.trim()
  if (!templateId || !templateName) {
    ElMessage.warning('请填写模板标识与名称')
    return
  }
  createSubmitting.value = true
  try {
    const created = await createPromptTemplate({
      template_id: templateId,
      template_name: templateName,
      description: createForm.description.trim() || null,
      category: createForm.category.trim() || null,
      owner: createForm.owner.trim() || null,
    })
    ElMessage.success('模板已创建')
    createVisible.value = false
    page.value = 1
    await loadTemplates()
    await selectTemplate(created.id)
  } catch (cause) {
    ElMessage.error(messageOf(cause))
  } finally {
    createSubmitting.value = false
  }
}

// ---- 新增版本 -----------------------------------------------------------

const versionVisible = ref(false)
const versionSubmitting = ref(false)
const versionForm = reactive({
  system_prompt: '',
  user_prompt_template: '',
  variables_schema: '',
  changelog: '',
})

function openVersionDialog(): void {
  versionForm.system_prompt = selectedVersion.value?.system_prompt ?? ''
  versionForm.user_prompt_template = selectedVersion.value?.user_prompt_template ?? ''
  versionForm.variables_schema = selectedVersion.value?.variables_schema
    ? JSON.stringify(selectedVersion.value.variables_schema, null, 2)
    : ''
  versionForm.changelog = ''
  versionVisible.value = true
}

/** JSON 文本为空表示 null；非法 JSON 或非对象立即阻止提交。 */
function parseVariablesSchema(): Record<string, unknown> | null | 'invalid' {
  const text = versionForm.variables_schema.trim()
  if (!text) {
    return null
  }
  try {
    const parsed: unknown = JSON.parse(text)
    if (parsed === null) {
      return null
    }
    if (typeof parsed !== 'object' || Array.isArray(parsed)) {
      return 'invalid'
    }
    return parsed as Record<string, unknown>
  } catch {
    return 'invalid'
  }
}

async function submitVersion(): Promise<void> {
  if (selectedPk.value === null) {
    return
  }
  const schema = parseVariablesSchema()
  if (schema === 'invalid') {
    ElMessage.error('variables_schema 必须是合法的 JSON 对象')
    return
  }
  const payload: AddPromptVersionPayload = {
    system_prompt: versionForm.system_prompt || null,
    user_prompt_template: versionForm.user_prompt_template || null,
    variables_schema: schema,
    changelog: versionForm.changelog.trim() || null,
  }
  versionSubmitting.value = true
  try {
    await addPromptVersion(selectedPk.value, payload)
    ElMessage.success('已追加新版本')
    versionVisible.value = false
    await loadDetail()
    await loadTemplates()
  } catch (cause) {
    ElMessage.error(messageOf(cause))
  } finally {
    versionSubmitting.value = false
  }
}

// ---- 发布 / 回滚 --------------------------------------------------------

/**
 * 发布或回滚到指定版本。
 *
 * 关键节点：
 * 1. 历史版本不可变，发布/回滚只切换 template.active_version；
 * 2. 二次确认文案明确“不修改任何历史版本内容”；
 * 3. 成功后同时刷新模板分页与详情，保证版本列表与 active 标记一致。
 *
 * @param action publish 或 rollback。
 * @param version 目标版本号。
 */
async function confirmSwitch(action: 'publish' | 'rollback', version: number): Promise<void> {
  if (selectedPk.value === null) {
    return
  }
  const label = action === 'publish' ? '发布' : '回滚'
  try {
    await ElMessageBox.confirm(
      `确认${label} v${version}？该操作只会切换当前发布版本（active_version），不会修改任何历史版本内容。`,
      `${label}版本`,
      {
        type: 'warning',
        confirmButtonText: `确认${label}`,
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  try {
    const templatePk = selectedPk.value
    if (action === 'publish') {
      await publishPromptVersion(templatePk, version)
    } else {
      await rollbackPromptVersion(templatePk, version)
    }
    ElMessage.success(`已${label} v${version}`)
    await loadDetail()
    await loadTemplates()
  } catch (cause) {
    ElMessage.error(messageOf(cause))
  }
}
</script>

<template>
  <section class="prompts">
    <header class="prompts__toolbar">
      <div>
        <h2 class="prompts__title">提示词</h2>
        <p class="prompts__subtitle">
          模板版本不可变；发布与回滚只切换当前发布版本。全部数据来自真实 Prompt API。
        </p>
      </div>
      <el-button
        v-if="isAdmin"
        type="primary"
        size="small"
        data-test="prompts-create"
        @click="openCreate"
      >
        新建模板
      </el-button>
    </header>

    <div class="prompts__layout">
      <el-card shadow="never" class="prompts__list-card">
        <template #header>
          <div class="prompts__list-header">
            <span>模板列表</span>
            <el-button size="small" text @click="loadTemplates">刷新</el-button>
          </div>
        </template>

        <AsyncState
          :loading="store.promptTemplatesLoading"
          :error="store.promptTemplatesError"
          :empty="templates.length === 0"
          @retry="loadTemplates"
        >
          <template #empty>
            <el-empty description="暂无 Prompt 模板" />
          </template>

          <el-table
            :data="templates"
            size="small"
            highlight-current-row
            row-key="id"
            data-test="prompt-templates"
            @row-click="handleRowClick"
          >
            <el-table-column label="模板" min-width="180">
              <template #default="{ row }">
                <div class="prompts__template-name">{{ row.template_name }}</div>
                <div class="prompts__muted">{{ row.template_id }}</div>
              </template>
            </el-table-column>
            <el-table-column label="版本" width="90">
              <template #default="{ row }">
                {{ row.active_version === null ? '未发布' : `v${row.active_version}` }}
              </template>
            </el-table-column>
          </el-table>

          <el-pagination
            class="prompts__pagination"
            layout="prev, pager, next"
            :current-page="page"
            :page-size="pageSize"
            :total="store.promptTemplatesTotal"
            @current-change="handlePageChange"
          />
        </AsyncState>
      </el-card>

      <div class="prompts__detail">
        <el-empty v-if="selectedPk === null" description="请选择左侧模板查看详情" />

        <AsyncState
          v-else
          :loading="store.promptTemplateLoading"
          :error="store.promptTemplateError"
          @retry="loadDetail"
        >
          <template v-if="detail">
            <el-card shadow="never" class="prompts__detail-head">
              <div class="prompts__detail-title-row">
                <div>
                  <h3 class="prompts__detail-title">{{ detail.template_name }}</h3>
                  <p class="prompts__muted">
                    {{ detail.template_id }}
                    <span v-if="detail.category"> · {{ detail.category }}</span>
                  </p>
                </div>
                <div class="prompts__detail-actions">
                  <el-tag v-if="detail.active_version !== null" type="success" effect="light">
                    当前发布 v{{ detail.active_version }}
                  </el-tag>
                  <el-tag v-else type="info" effect="light">未发布</el-tag>
                  <el-button size="small" @click="openPlayground">打开 Playground</el-button>
                  <el-button size="small" @click="loadDetail">刷新</el-button>
                  <el-button
                    v-if="isAdmin"
                    type="primary"
                    size="small"
                    @click="openVersionDialog"
                  >
                    新增版本
                  </el-button>
                </div>
              </div>
              <p v-if="detail.description" class="prompts__description">
                {{ detail.description }}
              </p>
            </el-card>

            <el-card shadow="never" class="prompts__section">
              <template #header>
                <div class="prompts__section-header">
                  <h4>版本时间线</h4>
                  <span class="prompts__muted">倒序展示，版本行只追加不修改</span>
                </div>
              </template>

              <el-empty v-if="versions.length === 0" description="尚未创建任何版本" />
              <el-timeline v-else>
                <el-timeline-item
                  v-for="version in versions"
                  :key="version.id"
                  :timestamp="`v${version.version} · ${formatDateTime(version.created_at)}`"
                  placement="top"
                  :type="version.version === detail.active_version ? 'success' : 'primary'"
                >
                  <div class="prompts__timeline-row">
                    <div>
                      <el-tag
                        v-if="version.version === detail.active_version"
                        type="success"
                        size="small"
                        effect="light"
                      >
                        当前发布
                      </el-tag>
                      <span class="prompts__changelog">
                        {{ version.changelog ?? '未记录变更说明' }}
                      </span>
                    </div>
                    <div class="prompts__timeline-actions">
                      <el-button
                        size="small"
                        text
                        @click="selectedVersionNo = version.version"
                      >
                        查看
                      </el-button>
                      <template v-if="isAdmin">
                        <el-button
                          v-if="version.version !== detail.active_version"
                          size="small"
                          type="primary"
                          link
                          @click="confirmSwitch('publish', version.version)"
                        >
                          发布
                        </el-button>
                        <el-button
                          v-if="version.version !== detail.active_version"
                          size="small"
                          type="warning"
                          link
                          @click="confirmSwitch('rollback', version.version)"
                        >
                          回滚
                        </el-button>
                      </template>
                    </div>
                  </div>
                </el-timeline-item>
              </el-timeline>
            </el-card>

            <el-card v-if="selectedVersion" shadow="never" class="prompts__section">
              <template #header>
                <div class="prompts__section-header">
                  <h4>版本内容 v{{ selectedVersion.version }}</h4>
                  <span class="prompts__muted">
                    创建者 {{ selectedVersion.created_by ?? '未记录' }} ·
                    {{ formatDateTime(selectedVersion.created_at) }}
                  </span>
                </div>
              </template>

              <h5 class="prompts__label">System Prompt</h5>
              <pre class="prompts__pre" data-test="prompt-system">{{
                selectedVersion.system_prompt ?? '未记录'
              }}</pre>

              <h5 class="prompts__label">User Prompt Template</h5>
              <pre class="prompts__pre" data-test="prompt-user">{{
                selectedVersion.user_prompt_template ?? '未记录'
              }}</pre>

              <h5 class="prompts__label">Variables Schema</h5>
              <JsonViewer :value="selectedVersion.variables_schema" filename="variables_schema.json" />

              <h5 class="prompts__label">变更说明</h5>
              <p class="prompts__changelog-block">
                {{ selectedVersion.changelog ?? '未记录' }}
              </p>
            </el-card>

            <el-card shadow="never" class="prompts__section">
              <template #header>
                <div class="prompts__section-header">
                  <h4>版本对比</h4>
                  <span class="prompts__muted">按行 LCS 比较，绿色为新增、红色为删除</span>
                </div>
              </template>

              <div class="prompts__compare-selects">
                <el-select v-model="compareLeft" size="small" placeholder="选择版本 A">
                  <el-option
                    v-for="version in versions"
                    :key="`left-${version.id}`"
                    :value="version.version"
                    :label="versionOptionLabel(version)"
                  />
                </el-select>
                <span class="prompts__muted">对比</span>
                <el-select v-model="compareRight" size="small" placeholder="选择版本 B">
                  <el-option
                    v-for="version in versions"
                    :key="`right-${version.id}`"
                    :value="version.version"
                    :label="versionOptionLabel(version)"
                  />
                </el-select>
              </div>

              <template v-if="leftVersion && rightVersion">
                <h5 class="prompts__label">System Prompt</h5>
                <PromptDiff
                  :left="leftVersion.system_prompt"
                  :right="rightVersion.system_prompt"
                  :left-label="`v${leftVersion.version}`"
                  :right-label="`v${rightVersion.version}`"
                />

                <h5 class="prompts__label">User Prompt Template</h5>
                <PromptDiff
                  :left="leftVersion.user_prompt_template"
                  :right="rightVersion.user_prompt_template"
                  :left-label="`v${leftVersion.version}`"
                  :right-label="`v${rightVersion.version}`"
                />
              </template>
              <p v-else class="prompts__muted">至少需要两个版本才能对比。</p>
            </el-card>

            <el-card shadow="never" class="prompts__section">
              <template #header>
                <div class="prompts__section-header">
                  <h4>使用历史</h4>
                  <span class="prompts__muted">按执行时间倒序，仅展示本模板记录</span>
                </div>
              </template>

              <AsyncState
                :loading="store.promptExecutionsLoading"
                :error="store.promptExecutionsError"
                :empty="store.promptExecutions.length === 0"
                @retry="loadExecutions"
              >
                <template #empty>
                  <el-empty description="暂无执行记录" />
                </template>

                <el-table :data="store.promptExecutions" size="small" data-test="prompt-executions">
                  <el-table-column label="版本" width="80">
                    <template #default="{ row }">
                      {{ row.template_version === null ? '未记录' : `v${row.template_version}` }}
                    </template>
                  </el-table-column>
                  <el-table-column label="状态" width="130">
                    <template #default="{ row }">
                      <el-tag
                        :type="row.render_status === 'ok' ? 'success' : 'danger'"
                        size="small"
                        effect="light"
                      >
                        {{ row.render_status }}
                      </el-tag>
                    </template>
                  </el-table-column>
                  <el-table-column label="模型" min-width="140">
                    <template #default="{ row }">{{ row.model_name ?? '未记录' }}</template>
                  </el-table-column>
                  <el-table-column label="时间" width="160">
                    <template #default="{ row }">{{ formatDateTime(row.created_at) }}</template>
                  </el-table-column>
                  <el-table-column label="Trace" min-width="180">
                    <template #default="{ row }">
                      <el-button
                        v-if="row.trace_id"
                        size="small"
                        text
                        type="primary"
                        @click="openTrace(row.trace_id)"
                      >
                        {{ row.trace_id }}
                      </el-button>
                      <span v-else class="prompts__muted">未记录</span>
                    </template>
                  </el-table-column>
                </el-table>

                <el-pagination
                  class="prompts__pagination"
                  layout="prev, pager, next"
                  :current-page="executionsPage"
                  :page-size="executionsSize"
                  :total="store.promptExecutionsTotal"
                  @current-change="handleExecutionsPageChange"
                />
              </AsyncState>
            </el-card>
          </template>
        </AsyncState>
      </div>
    </div>

    <el-dialog v-model="createVisible" title="新建 Prompt 模板" width="520px">
      <el-form label-width="110px">
        <el-form-item label="模板标识" required>
          <el-input v-model="createForm.template_id" placeholder="例如 vision_analysis" />
        </el-form-item>
        <el-form-item label="模板名称" required>
          <el-input v-model="createForm.template_name" />
        </el-form-item>
        <el-form-item label="描述">
          <el-input v-model="createForm.description" type="textarea" :rows="2" />
        </el-form-item>
        <el-form-item label="分类">
          <el-input v-model="createForm.category" />
        </el-form-item>
        <el-form-item label="负责人">
          <el-input v-model="createForm.owner" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="createVisible = false">取消</el-button>
        <el-button type="primary" :loading="createSubmitting" @click="submitCreate">
          创建
        </el-button>
      </template>
    </el-dialog>

    <el-dialog v-model="versionVisible" title="新增版本" width="640px">
      <el-form label-width="130px">
        <el-form-item label="System Prompt">
          <el-input v-model="versionForm.system_prompt" type="textarea" :rows="4" />
        </el-form-item>
        <el-form-item label="User Prompt 模板">
          <el-input v-model="versionForm.user_prompt_template" type="textarea" :rows="5" />
        </el-form-item>
        <el-form-item label="Variables Schema">
          <el-input
            v-model="versionForm.variables_schema"
            type="textarea"
            :rows="5"
            placeholder='JSON 对象，例如 {"type":"object","properties":{}}'
          />
        </el-form-item>
        <el-form-item label="变更说明">
          <el-input v-model="versionForm.changelog" type="textarea" :rows="2" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="versionVisible = false">取消</el-button>
        <el-button type="primary" :loading="versionSubmitting" @click="submitVersion">
          提交
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.prompts {
  width: 100%;
}

.prompts__toolbar {
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

.prompts__title {
  margin: 0;
  color: #111827;
  font-size: 18px;
  font-weight: 600;
}

.prompts__subtitle {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
}

.prompts__layout {
  display: grid;
  grid-template-columns: 300px minmax(0, 1fr);
  gap: 16px;
  align-items: start;
}

.prompts__list-card,
.prompts__detail-head,
.prompts__section {
  border-radius: 12px;
}

.prompts__list-header,
.prompts__section-header,
.prompts__list-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.prompts__list-card :deep(.el-table__row) {
  cursor: pointer;
}

.prompts__template-name {
  color: #0f172a;
  font-size: 13px;
  font-weight: 500;
}

.prompts__muted {
  color: #94a3b8;
  font-size: 12px;
}

.prompts__pagination {
  margin-top: 12px;
  justify-content: flex-end;
}

.prompts__detail {
  display: flex;
  flex-direction: column;
  gap: 16px;
  min-width: 0;
}

.prompts__detail-title-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
}

.prompts__detail-title {
  margin: 0;
  color: #111827;
  font-size: 16px;
  font-weight: 600;
}

.prompts__detail-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.prompts__description {
  margin: 10px 0 0;
  color: #475569;
  font-size: 13px;
  line-height: 1.6;
}

.prompts__section-header h4 {
  margin: 0;
  color: #111827;
  font-size: 14px;
  font-weight: 600;
}

.prompts__timeline-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}

.prompts__timeline-actions {
  display: flex;
  align-items: center;
  gap: 4px;
}

.prompts__changelog {
  margin-left: 8px;
  color: #475569;
  font-size: 13px;
}

.prompts__label {
  margin: 16px 0 6px;
  color: #334155;
  font-size: 12px;
  font-weight: 600;
}

.prompts__label:first-child {
  margin-top: 0;
}

.prompts__pre {
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

.prompts__changelog-block {
  margin: 0;
  color: #475569;
  font-size: 13px;
  line-height: 1.6;
}

.prompts__compare-selects {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

@media (max-width: 1100px) {
  .prompts__layout {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>
