<script setup lang="ts">
withDefaults(
  defineProps<{
    loading?: boolean
    error?: string | null
    empty?: boolean
  }>(),
  {
    loading: false,
    error: null,
    empty: false,
  },
)

const emit = defineEmits<{
  retry: []
}>()
</script>

<template>
  <div class="async-state">
    <div v-if="loading" class="async-state__loading" data-test="async-loading" role="status">
      <span class="async-state__spinner" aria-hidden="true"></span>
      <span>加载中…</span>
    </div>

    <div v-else-if="error" class="async-state__error" data-test="async-error">
      <el-alert :title="error" type="error" :closable="false" show-icon />
      <el-button class="async-state__retry" type="primary" plain @click="emit('retry')">
        重试
      </el-button>
    </div>

    <div v-else-if="empty" class="async-state__empty" data-test="async-empty">
      <slot name="empty">
        <el-empty description="暂无数据" />
      </slot>
    </div>

    <div v-else class="async-state__content" data-test="async-content">
      <slot />
    </div>
  </div>
</template>

<style scoped>
.async-state {
  width: 100%;
}

.async-state__loading {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 12px;
  padding: 48px 24px;
  color: #6b7280;
  font-size: 14px;
}

.async-state__spinner {
  width: 20px;
  height: 20px;
  border: 2px solid #dbeafe;
  border-top-color: #2563eb;
  border-radius: 50%;
  animation: async-state-spin 0.8s linear infinite;
}

@keyframes async-state-spin {
  to {
    transform: rotate(360deg);
  }
}

.async-state__error {
  padding: 24px;
}

.async-state__retry {
  margin-top: 16px;
}

.async-state__empty {
  padding: 24px;
}
</style>
