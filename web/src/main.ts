import { createApp } from 'vue'
import { createPinia } from 'pinia'
import ElementPlus from 'element-plus'
import 'element-plus/dist/index.css'

import App from './App.vue'
import router from './router'
import { sanitizeRedirect } from './router/redirect'
import { useAuthStore } from './stores/auth'
import './styles/main.css'

const app = createApp(App)
const pinia = createPinia()

app.use(pinia)
app.use(router)
app.use(ElementPlus)

// 令牌刷新失败时统一退出，避免页面继续停留在需要登录的界面
window.addEventListener('auth:unauthorized', () => {
  const auth = useAuthStore(pinia)
  auth.reset()

  const current = router.currentRoute.value
  if (current.path === '/login') {
    return
  }

  const redirect = sanitizeRedirect(current.fullPath)
  void router.replace({
    name: 'login',
    query: redirect && redirect !== '/' ? { redirect } : {},
  })
})

app.mount('#app')
