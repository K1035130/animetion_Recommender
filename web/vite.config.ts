import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // 把 /api/* 代理到本地 FastAPI。
    // ⚠️ 这让**开发时也是同源**的 —— 前端一律用相对路径 fetch('/api/...')，
    //    线上由 vercel.json 的 rewrite 分流，两边行为一致。
    //    因此前端永远不需要知道 API 的域名，也不该有 VITE_API_BASE 这种变量：
    //    一旦硬编码了域名，本地和线上就会走两条不同的路径，CORS 问题也会跟着回来。
    proxy: {
      '/api': { target: 'http://127.0.0.1:8000', changeOrigin: true },
    },
  },
})
