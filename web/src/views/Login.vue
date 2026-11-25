<script setup lang="ts">
import { reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { type FormInstance, type FormRules } from 'element-plus'

import { ApiError } from '@/api/http'
import { sanitizeRedirect } from '@/router/redirect'
import { useAuthStore } from '@/stores/auth'

const auth = useAuthStore()
const route = useRoute()
const router = useRouter()

const formRef = ref<FormInstance>()
const submitting = ref(false)
const errorMessage = ref('')

const form = reactive({
  email: '',
  password: '',
})

const rules: FormRules = {
  email: [
    { required: true, message: '请输入邮箱', trigger: 'blur' },
    { type: 'email', message: '请输入有效的邮箱地址', trigger: ['blur', 'change'] },
  ],
  password: [{ required: true, message: '请输入密码', trigger: 'blur' }],
}

function errorToMessage(error: unknown): string {
  if (error instanceof ApiError) {
    return error.message
  }
  if (error instanceof Error && error.message) {
    return error.message
  }
  return '登录失败，请稍后重试'
}

async function handleSubmit(): Promise<void> {
  if (submitting.value) {
    return
  }

  submitting.value = true
  errorMessage.value = ''

  try {
    form.email = form.email.trim()
    const valid = await formRef.value?.validate().catch(() => false)
    if (!valid) {
      return
    }

    await auth.login(form.email, form.password)
    const redirect = sanitizeRedirect(route.query.redirect) ?? '/'
    await router.replace(redirect === '/login' ? '/' : redirect)
  } catch (error) {
    errorMessage.value = errorToMessage(error)
  } finally {
    submitting.value = false
  }
}
</script>

<template>
  <div class="login-page">
    <section class="login-panel">
      <div class="login-brand">
        <span class="login-brand__mark">EP</span>
        <div>
          <p class="login-brand__name">EdgePilot</p>
          <p class="login-brand__desc">边缘智能运维平台</p>
        </div>
      </div>

      <el-card class="login-card" shadow="never">
        <h1 class="login-card__title">登录</h1>
        <p class="login-card__subtitle">使用你的 EdgePilot 账号进入控制台</p>

        <el-alert
          v-if="errorMessage"
          class="login-card__alert"
          :title="errorMessage"
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
          @submit.prevent="handleSubmit"
        >
          <el-form-item label="邮箱" prop="email">
            <el-input
              v-model="form.email"
              placeholder="请输入邮箱"
              autocomplete="username"
              :disabled="submitting"
              @keyup.enter="handleSubmit"
            />
          </el-form-item>

          <el-form-item label="密码" prop="password">
            <el-input
              v-model="form.password"
              type="password"
              placeholder="请输入密码"
              autocomplete="current-password"
              show-password
              :disabled="submitting"
              @keyup.enter="handleSubmit"
            />
          </el-form-item>

          <el-button
            class="login-card__submit"
            type="primary"
            native-type="submit"
            :loading="submitting"
          >
            {{ submitting ? '登录中…' : '登录' }}
          </el-button>
        </el-form>
      </el-card>

      <p class="login-footer">EdgePilot · 边缘视觉与智能体协同</p>
    </section>
  </div>
</template>

<style scoped>
.login-page {
  display: flex;
  min-height: 100%;
  align-items: center;
  justify-content: center;
  padding: 48px 24px;
  background:
    radial-gradient(circle at 18% 20%, rgba(37, 99, 235, 0.18), transparent 42%),
    radial-gradient(circle at 82% 76%, rgba(14, 165, 233, 0.14), transparent 40%),
    linear-gradient(145deg, #0b1220 0%, #111c30 52%, #0f2135 100%);
}

.login-panel {
  width: 100%;
  max-width: 420px;
}

.login-brand {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 24px;
  color: #e5edff;
}

.login-brand__mark {
  display: inline-flex;
  width: 44px;
  height: 44px;
  align-items: center;
  justify-content: center;
  border-radius: 12px;
  background: linear-gradient(135deg, #3b82f6, #06b6d4);
  font-weight: 700;
  letter-spacing: 0.08em;
  color: #ffffff;
  font-size: 15px;
}

.login-brand__name {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.02em;
}

.login-brand__desc {
  margin: 2px 0 0;
  font-size: 12px;
  color: rgba(226, 232, 240, 0.72);
}

.login-card {
  border: 1px solid rgba(255, 255, 255, 0.1);
  border-radius: 16px;
  background: rgba(255, 255, 255, 0.98);
}

.login-card__title {
  margin: 0;
  font-size: 24px;
  font-weight: 700;
  color: #0f172a;
}

.login-card__subtitle {
  margin: 6px 0 24px;
  color: #6b7280;
  font-size: 13px;
}

.login-card__alert {
  margin-bottom: 18px;
}

.login-card__submit {
  width: 100%;
  margin-top: 4px;
  height: 40px;
}

.login-footer {
  margin: 20px 0 0;
  text-align: center;
  color: rgba(226, 232, 240, 0.62);
  font-size: 12px;
}
</style>
