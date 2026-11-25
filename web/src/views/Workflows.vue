<script setup lang="ts">
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { useRouter } from 'vue-router'
import { type FormInstance, type FormRules } from 'element-plus'

import { ApiError } from '@/api/http'
import {
  createWorkflow,
  listWorkflows,
  type WorkflowListItem,
  type WorkflowStatus,
} from '@/api/workflows'
import AsyncState from '@/components/common/AsyncState.vue'
import { useAuthStore } from '@/stores/auth'
import { formatDateTime } from '@/utils/datetime'
import { runStatusLabel, runStatusTagType } from '@/utils/status'

type TagType = 'success' | 'info' | 'warning' | 'danger' | undefined

const PAGE_SIZE = 20

const router = useRouter()
const auth = useAuthStore()

const loading = ref(true)
const error = ref<string | null>(null)
const items = ref<WorkflowListItem[]>([])
const total = ref(0)
const page = ref(1)

const dialogVisible = ref(false)
const submitting = ref(false)
const formError = ref('')
const formRef = ref<FormInstance>()

const form = reactive({
  name: '',
  description: '',
})

const rules: FormRules = {
  name: [
    { required: true, message: '请输入工作流名称', trigger: 'blur' },
    { max: 128, message: '名称不能超过 128 个字符', trigger: 'blur' },
  ],
}

const canWrite = computed(() => auth.role === 'operator' || auth.role === 'admin')
const empty = computed(() => items.value.length === 0)

/** 只展示后端返回的错误信息，避免把 axios 细节暴露给用户。 */
function errorToMessage(value: unknown): string {
  if (value instanceof ApiError) {
    return value.message
  }
  if (value instanceof Error && value.message) {
    return value.message
  }
  return '请求失败，请稍后重试'
}

function workflowStatusLabel(status: WorkflowStatus): string {
  return status === 'published' ? '已发布' : '草稿'
}

function workflowStatusTagType(status: WorkflowStatus): TagType {
  return status === 'published' ? 'success' : 'info'
}

function versionLabel(version: number): string {
  return version > 0 ? `v${version}` : '未发布'
}

// 用递增令牌丢弃过期响应，避免快速翻页时旧请求覆盖新结果。
let loadToken = 0

async function load(targetPage: number = page.value): Promise<void> {
  const token = ++loadToken
  loading.value = true
  error.value = null

  try {
    const result = await listWorkflows({ page: targetPage, size: PAGE_SIZE })
    if (token !== loadToken) {
      return
    }
    items.value = result.items
    total.value = result.total
    page.value = targetPage
  } catch (cause) {
    if (token !== loadToken) {
      return
    }
    items.value = []
    total.value = 0
    error.value = errorToMessage(cause)
  } finally {
    if (token === loadToken) {
      loading.value = false
    }
  }
}

function handlePageChange(nextPage: number): void {
  page.value = nextPage
  void load(nextPage)
}

function openDetail(id: number): void {
  void router.push({ name: 'workflow-editor', params: { id } })
}

function handleRowClick(row: { id: number }): void {
  openDetail(row.id)
}

function openCreateDialog(): void {
  form.name = ''
  form.description = ''
  formError.value = ''
  dialogVisible.value = true
  void nextTick(() => formRef.value?.clearValidate())
}

async function handleCreate(): Promise<void> {
  if (submitting.value) {
    return
  }

  formError.value = ''
  form.name = form.name.trim()

  const valid = await formRef.value?.validate().catch(() => false)
  if (!valid) {
    return
  }

  submitting.value = true
  try {
    const created = await createWorkflow({
      name: form.name,
      description: form.description.trim() ? form.description.trim() : null,
      // 新工作流从空图草稿开始，节点与连线由编辑器补齐。
      dsl: { nodes: [], edges: [] },
    })
    dialogVisible.value = false
    await router.push({ name: 'workflow-editor', params: { id: created.id } })
  } catch (cause) {
    formError.value = errorToMessage(cause)
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  void load()
})
</script>

<template>
  <section class="workflows-page">
    <el-card shadow="never" class="workflows-page__card">
      <header class="workflows-page__toolbar">
        <div>
          <h2 class="workflows-page__title">工作流</h2>
          <p class="workflows-page__subtitle">
            查看工作流草稿、发布状态与最近运行；点击名称或“编辑”进入编辑器。
          </p>
        </div>
        <el-button
          v-if="canWrite"
          type="primary"
          data-test="create-workflow"
          @click="openCreateDialog"
        >
          新建工作流
        </el-button>
      </header>

      <AsyncState
        :loading="loading"
        :error="error"
        :empty="empty"
        @retry="load()"
      >
        <template #empty>
          <el-empty description="暂无工作流">
            <template v-if="canWrite">
              <p class="workflows-page__empty-hint">
                填写名称与描述即可创建第一个工作流，随后进入编辑器编排节点。
              </p>
              <el-button
                type="primary"
                data-test="create-workflow-empty"
                @click="openCreateDialog"
              >
                新建工作流
              </el-button>
            </template>
            <p v-else class="workflows-page__empty-hint">
              当前没有可查看的工作流；如需创建请联系管理员或操作员。
            </p>
          </el-empty>
        </template>

        <el-table
          :data="items"
          row-key="id"
          class="workflows-page__table"
          @row-click="handleRowClick"
        >
          <el-table-column prop="name" label="名称" min-width="200">
            <template #default="{ row }">
              <el-link
                type="primary"
                underline="never"
                class="workflows-page__name"
                @click.stop="openDetail(row.id)"
              >
                {{ row.name }}
              </el-link>
            </template>
          </el-table-column>

          <el-table-column label="状态" width="110">
            <template #default="{ row }">
              <el-tag :type="workflowStatusTagType(row.status)" effect="light">
                {{ workflowStatusLabel(row.status) }}
              </el-tag>
            </template>
          </el-table-column>

          <el-table-column label="当前版本" width="120">
            <template #default="{ row }">
              <span :class="{ 'workflows-page__muted': row.current_version <= 0 }">
                {{ versionLabel(row.current_version) }}
              </span>
            </template>
          </el-table-column>

          <el-table-column label="最近运行" min-width="220">
            <template #default="{ row }">
              <div v-if="row.last_run" class="workflows-page__run">
                <el-tag size="small" :type="runStatusTagType(row.last_run.status)" effect="light">
                  {{ runStatusLabel(row.last_run.status) }}
                </el-tag>
                <span class="workflows-page__muted">
                  {{ formatDateTime(row.last_run.created_at) }}
                </span>
              </div>
              <span v-else class="workflows-page__muted">暂无运行</span>
            </template>
          </el-table-column>

          <el-table-column label="更新时间" width="170">
            <template #default="{ row }">
              <span class="workflows-page__muted">{{ formatDateTime(row.updated_at) }}</span>
            </template>
          </el-table-column>

          <el-table-column label="操作" width="100" align="right">
            <template #default="{ row }">
              <el-button link type="primary" @click.stop="openDetail(row.id)">编辑</el-button>
            </template>
          </el-table-column>
        </el-table>

        <div v-if="total > 0" class="workflows-page__pagination">
          <el-pagination
            v-model:current-page="page"
            :page-size="PAGE_SIZE"
            :total="total"
            layout="total, prev, pager, next"
            background
            @current-change="handlePageChange"
          />
        </div>
      </AsyncState>
    </el-card>

    <el-dialog v-model="dialogVisible" title="新建工作流" width="480px">
      <el-alert
        v-if="formError"
        class="workflows-page__dialog-error"
        :title="formError"
        type="error"
        :closable="false"
        show-icon
      />

      <el-form
        ref="formRef"
        :model="form"
        :rules="rules"
        label-position="top"
        hide-required-asterisk
        @submit.prevent="handleCreate"
      >
        <el-form-item label="名称" prop="name">
          <el-input
            v-model="form.name"
            placeholder="请输入工作流名称"
            maxlength="128"
            show-word-limit
            :disabled="submitting"
            @keyup.enter="handleCreate"
          />
        </el-form-item>

        <el-form-item label="描述" prop="description">
          <el-input
            v-model="form.description"
            type="textarea"
            :rows="3"
            placeholder="可选：简要说明该工作流的用途"
            :disabled="submitting"
          />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button :disabled="submitting" @click="dialogVisible = false">取消</el-button>
        <el-button
          type="primary"
          data-test="submit-create"
          :loading="submitting"
          @click="handleCreate"
        >
          {{ submitting ? '创建中…' : '创建' }}
        </el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.workflows-page {
  width: 100%;
}

.workflows-page__card {
  border-radius: 12px;
}

.workflows-page__toolbar {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
  padding-bottom: 16px;
  border-bottom: 1px solid #eef1f6;
}

.workflows-page__title {
  margin: 0;
  font-size: 18px;
  font-weight: 600;
  color: #111827;
}

.workflows-page__subtitle {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
}

.workflows-page__table {
  margin-top: 8px;
}

.workflows-page :deep(.el-table__row) {
  cursor: pointer;
}

.workflows-page__name {
  font-weight: 500;
}

.workflows-page__muted {
  color: #94a3b8;
  font-size: 13px;
}

.workflows-page__run {
  display: flex;
  align-items: center;
  gap: 8px;
}

.workflows-page__pagination {
  display: flex;
  justify-content: flex-end;
  padding-top: 16px;
}

.workflows-page__empty-hint {
  margin: 0 0 12px;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.7;
}

.workflows-page__dialog-error {
  margin-bottom: 16px;
}
</style>
