import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5275,
    proxy: {
      '/api': {
        target: 'http://localhost:8275',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
      // 后端返回同源的 /media URL，开发服务器需要将图片请求转发给 FastAPI。
      '/media': {
        target: 'http://localhost:8275',
        changeOrigin: true,
      },
    },
  },
  test: {
    environment: 'jsdom',
    exclude: ['e2e/**', 'node_modules/**'],
  },
})
