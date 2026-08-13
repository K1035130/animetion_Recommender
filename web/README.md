# web — 前端 v0

Vite + React + TypeScript + Tailwind 4。**刻意做得很薄**，见 [../CLAUDE.md](../CLAUDE.md)：
第 3–5 周的产出（embedding、聚类选题、评测）会改变前端要展示的东西，
现在打磨的 UI 大概率要重做，所以功能齐了再一次性升级。

```bash
npm install
npm run dev      # http://localhost:5173
```

⚠️ 用 `localhost` 而不是 `127.0.0.1` —— Vite 绑的是 localhost，Windows 上解析到 IPv6。

⚠️ **需要后端同时跑着**：`uv run uvicorn server.main:app --reload`（8000 端口）。
`vite.config.ts` 把 `/api/*` 代理过去，所以开发时也是同源的。

## 三条不要破坏的约定

1. **一律用相对路径 `fetch('/api/...')`，不要引入 API 域名变量。**
   开发靠 Vite 代理、线上靠 vercel.json 的 rewrite，两边都同源。
   硬编码域名会让本地与线上走两条不同路径，CORS 问题也会跟着回来。

2. **传作答选项（`choice`），不传算好的分数。**
   分数与置信度的映射只在服务端 `questionnaire.to_rating()` 一处维护。
   在 TypeScript 里复制一份 = 埋一个静默的推荐质量漂移。

3. **推荐列表按返回顺序渲染，不要按 `match` 或 `quality` 重排。**
   后端已按 `rank_score` 降序，而 blend 模式下 `match` 会大小交错 —— 那是预期的。

## 文件

```
src/api.ts        接口类型与调用封装（手写，对着 server/schemas.py）
src/storage.ts    localStorage 读写；格式与 /api/recommend 请求体一致
src/App.tsx       全部界面
```

## 还没接的接口

`/api/search`（用户主动搜作品打分）和 `/api/anime/{id}`（详情页）后端已经能用，界面没做。
