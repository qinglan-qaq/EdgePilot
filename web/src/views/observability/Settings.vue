<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

import AsyncState from '@/components/common/AsyncState.vue'
import MetricCard from '@/components/observability/MetricCard.vue'
import { useObservabilityStore } from '@/stores/observability'

const store = useObservabilityStore()

interface CaptureModeInfo {
  label: string
  description: string
}

/** 四种采集模式的真实含义（与后端 redaction / recorder 语义一致）。 */
const CAPTURE_MODE_INFO: Record<string, CaptureModeInfo> = {
  OFF: {
    label: 'OFF',
    description: '只存标识（trace/span/template/version/status/model），不保存任何内容。',
  },
  METADATA: {
    label: 'METADATA',
    description: '只保存 prompt_hash 等元数据，不保存变量与渲染后的 messages。',
  },
  REDACTED: {
    label: 'REDACTED',
    description: '内容脱敏后保存：敏感键替换为 ***，并清理 Bearer / sk- / 手机号 / 身份证号。',
  },
  FULL: {
    label: 'FULL',
    description: '内容原样保存（含变量、渲染 messages 与响应），仅建议受控环境使用。',
  },
}

const RETENTION_LABELS: Record<string, string> = {
  trace_events: 'Trace 事件',
  traces: 'Trace',
  spans: 'Span',
  llm_calls: 'LLM 调用',
  tool_calls: '工具调用',
  retrieval_records: '检索记录',
  prompt_executions: 'Prompt 执行',
  audit_logs: '审计日志',
}

const SCRUBBER_LABELS: Record<string, string> = {
  bearer: 'Bearer Token',
  sk_key: 'sk- 密钥',
  phone: '手机号',
  id_card: '身份证号',
}

const settings = computed(() => store.settings)

const currentModeDescription = computed(() => {
  const mode = settings.value?.capture_mode
  return mode ? CAPTURE_MODE_INFO[mode]?.description ?? '未记录的采集模式' : ''
})

const retentionRows = computed(() =>
  Object.entries(settings.value?.retention_days ?? {}).map(([key, days]) => ({
    key,
    label: RETENTION_LABELS[key] ?? key,
    days,
  })),
)

const modeRows = computed(() =>
  (settings.value?.capture_modes ?? []).map((mode) => ({
    mode,
    description: CAPTURE_MODE_INFO[mode]?.description ?? '未记录该模式说明',
    active: settings.value?.capture_mode === mode,
  })),
)

/** provider + model 才是唯一键；不同 provider 可能有同名 model。 */
function unpricedRowKey(row: { provider: string | null; model_name: string | null }): string {
  return `${row.provider ?? 'null'}:${row.model_name ?? 'null'}`
}

function load(): void {
  void store.loadSettings()
}

onMounted(load)

// ---- 修改采集模式：只生成变更步骤，绝不伪造保存 -------------------------

const editVisible = ref(false)
const targetMode = ref('')
const changePlan = ref<string[]>([])

function openEdit(): void {
  targetMode.value = settings.value?.capture_mode ?? ''
  changePlan.value = []
  editVisible.value = true
}

async function planChange(): Promise<void> {
  const target = targetMode.value
  if (!target) {
    ElMessage.warning('请选择目标采集模式')
    return
  }

  const current = settings.value?.capture_mode ?? '未知'
  try {
    await ElMessageBox.confirm(
      `当前版本采集模式由环境变量 OBS_CAPTURE_MODE 控制，确认后需更新环境变量并重启 api/worker 才生效。\n\n当前模式：${current}\n目标模式：${target}`,
      '变更采集模式',
      {
        type: 'warning',
        confirmButtonText: '查看变更步骤',
        cancelButtonText: '取消',
      },
    )
  } catch {
    return
  }

  // 只展示操作步骤；不调用任何写接口，也不修改本地展示值。
  changePlan.value = [
    `1. 更新部署环境变量：OBS_CAPTURE_MODE=${target}`,
    '2. 重启 api 进程，使新采集模式生效',
    '3. 重启 worker 进程（Celery worker 同样读取该环境变量）',
    '4. 返回本页点击“刷新”，确认 capture_mode 已变为目标模式',
  ]
  ElMessage.info('已生成变更步骤，本页面不会保存任何配置')
}
</script>

<template>
  <section class="settings-page">
    <header class="settings-page__toolbar">
      <div>
        <h2 class="settings-page__title">可观测设置</h2>
        <p class="settings-page__subtitle">
          本页只读展示当前进程的真实配置；采集模式由环境变量 OBS_CAPTURE_MODE 控制，
          本期不提供持久化写入接口。
        </p>
      </div>
      <div class="settings-page__actions">
        <el-button size="small" :loading="store.settingsLoading" @click="load">
          刷新
        </el-button>
        <el-button size="small" type="primary" @click="openEdit">修改采集模式</el-button>
      </div>
    </header>

    <AsyncState :loading="store.settingsLoading" :error="store.settingsError" @retry="load">
      <template v-if="settings">
        <div class="settings-page__cards">
          <MetricCard
            metric="capture_mode"
            label="当前采集模式"
            :value="settings.capture_mode"
            hint="来自服务端进程配置 obs_capture_mode / 环境变量 OBS_CAPTURE_MODE。"
            :detail="currentModeDescription"
          />
          <MetricCard
            metric="unpriced_models"
            label="缺价模型组合"
            :value="String(settings.unpriced_models.length)"
            hint="租户范围内 cost IS NULL 的 provider/model 组合数（最多展示 20 条）。"
          />
          <MetricCard
            metric="scrubber_count"
            label="脱敏规则"
            :value="String(settings.redaction.scrubbers.length)"
            hint="内容脱敏阶段启用的正则清理规则数量。"
            :detail="settings.redaction.token_count_exempt ? 'Token 计数键不脱敏' : null"
          />
        </div>

        <el-card shadow="never" class="settings-page__card">
          <template #header>
            <div class="settings-page__card-header">
              <h3>采集模式</h3>
              <span class="settings-page__muted">
                当前：{{ settings.capture_mode }}
              </span>
            </div>
          </template>

          <el-table :data="modeRows" size="small" row-key="mode" data-test="capture-modes">
            <el-table-column label="模式" width="140">
              <template #default="{ row }">
                <el-tag :type="row.active ? 'success' : 'info'" size="small" effect="light">
                  {{ row.mode }}
                </el-tag>
                <span v-if="row.active" class="settings-page__muted settings-page__current">
                  当前
                </span>
              </template>
            </el-table-column>
            <el-table-column prop="description" label="说明" min-width="360" />
          </el-table>
        </el-card>

        <el-card shadow="never" class="settings-page__card">
          <template #header>
            <div class="settings-page__card-header">
              <h3>保留期</h3>
              <span class="settings-page__muted">固定策略，本期不支持运行时修改</span>
            </div>
          </template>

          <el-table :data="retentionRows" size="small" row-key="key" data-test="retention-days">
            <el-table-column prop="label" label="数据类型" min-width="160" />
            <el-table-column prop="key" label="表 / 记录" min-width="180" />
            <el-table-column label="保留天数" width="120">
              <template #default="{ row }">{{ row.days }} 天</template>
            </el-table-column>
          </el-table>
        </el-card>

        <el-card shadow="never" class="settings-page__card">
          <template #header>
            <div class="settings-page__card-header">
              <h3>脱敏规则</h3>
            </div>
          </template>

          <el-descriptions :column="1" size="small" border>
            <el-descriptions-item label="敏感键正则">
              <code class="settings-page__code">
                {{ settings.redaction.sensitive_key_pattern }}
              </code>
            </el-descriptions-item>
            <el-descriptions-item label="内容清理器">
              <el-tag
                v-for="scrubber in settings.redaction.scrubbers"
                :key="scrubber"
                size="small"
                effect="plain"
                class="settings-page__tag"
              >
                {{ SCRUBBER_LABELS[scrubber] ?? scrubber }}
              </el-tag>
            </el-descriptions-item>
            <el-descriptions-item label="Token 计数键豁免">
              {{ settings.redaction.token_count_exempt ? '是（*_tokens 计数不视为敏感键）' : '否' }}
            </el-descriptions-item>
          </el-descriptions>
        </el-card>

        <el-card shadow="never" class="settings-page__card">
          <template #header>
            <div class="settings-page__card-header">
              <h3>未配价模型</h3>
              <span class="settings-page__muted">
                租户范围内 cost IS NULL 的调用（按次数降序，最多 20 条）
              </span>
            </div>
          </template>

          <el-empty
            v-if="settings.unpriced_models.length === 0"
            description="没有缺价模型：当前租户所有 LLM 调用都有成本记录"
          />
          <el-table
            v-else
            :data="settings.unpriced_models"
            size="small"
            :row-key="unpricedRowKey"
            data-test="unpriced-models"
          >
            <el-table-column prop="provider" label="provider" min-width="140" />
            <el-table-column prop="model_name" label="model" min-width="180" />
            <el-table-column prop="calls" label="缺价调用数" width="120" />
          </el-table>
        </el-card>
      </template>
    </AsyncState>

    <el-dialog v-model="editVisible" title="修改采集模式" width="560px">
      <p class="settings-page__dialog-hint">
        采集模式由环境变量控制，本页面不会保存任何配置。请选择目标模式后查看变更步骤。
      </p>
      <el-form label-width="110px">
        <el-form-item label="目标模式">
          <el-select v-model="targetMode" placeholder="请选择">
            <el-option
              v-for="mode in settings?.capture_modes ?? []"
              :key="mode"
              :value="mode"
              :label="mode"
            />
          </el-select>
        </el-form-item>
      </el-form>

      <el-alert
        v-if="changePlan.length > 0"
        type="info"
        :closable="false"
        title="未保存任何更改；请按以下步骤在部署环境操作："
        show-icon
      >
        <ol class="settings-page__plan">
          <li v-for="step in changePlan" :key="step">{{ step }}</li>
        </ol>
      </el-alert>

      <template #footer>
        <el-button @click="editVisible = false">关闭</el-button>
        <el-button type="primary" @click="planChange">生成变更步骤</el-button>
      </template>
    </el-dialog>
  </section>
</template>

<style scoped>
.settings-page {
  width: 100%;
}

.settings-page__toolbar {
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

.settings-page__title {
  margin: 0;
  color: #111827;
  font-size: 18px;
  font-weight: 600;
}

.settings-page__subtitle {
  margin: 6px 0 0;
  color: #6b7280;
  font-size: 13px;
  line-height: 1.6;
  max-width: 760px;
}

.settings-page__actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.settings-page__cards {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 12px;
}

.settings-page__card {
  margin-top: 16px;
  border-radius: 12px;
}

.settings-page__card-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.settings-page__card-header h3 {
  margin: 0;
  color: #111827;
  font-size: 15px;
  font-weight: 600;
}

.settings-page__muted {
  color: #94a3b8;
  font-size: 12px;
}

.settings-page__current {
  margin-left: 6px;
}

.settings-page__code {
  font-family: 'JetBrains Mono', Consolas, monospace;
  font-size: 12px;
  word-break: break-all;
}

.settings-page__tag {
  margin-right: 6px;
}

.settings-page__dialog-hint {
  margin: 0 0 16px;
  color: #475569;
  font-size: 13px;
  line-height: 1.6;
}

.settings-page__plan {
  margin: 8px 0 0;
  padding-left: 20px;
  color: #475569;
  font-size: 13px;
  line-height: 1.8;
}

@media (max-width: 960px) {
  .settings-page__toolbar {
    flex-direction: column;
  }
}
</style>
