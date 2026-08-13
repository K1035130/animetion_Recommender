/**
 * 后端接口的类型与调用封装。
 *
 * ⚠️ 一律用相对路径 `/api/...`，不要引入 API 域名变量。
 *    开发时 vite 代理转发到 127.0.0.1:8000，线上由 vercel.json 的 rewrite 分流，
 *    两边都是同源。硬编码域名会让本地与线上走两条不同的路径，CORS 也会跟着回来。
 *
 * ⚠️ 类型是手写的，对着 server/schemas.py。改后端 schema 后要回来同步 ——
 *    以后可以从 /api/openapi.json 生成，现在接口就五个，不值得引工具链。
 */

const BASE = '/api'

export type Choice = 'seen' | 'wish' | 'pass' | 'skip'
export type Mode = 'all' | 'season' | 'aired' | 'upcoming' | 'recent' | 'classic'
export type RankBy = 'match' | 'quality' | 'blend'
export type Experience = 'new' | 'mid' | 'veteran'

export interface QuestionItem {
  subject_id: number
  name: string
  year: number | null
  done: number
  form: string | null
  replaced_from: number | null
}

/**
 * ⚠️ 列表恒按 `rank_score` 降序，**直接按顺序渲染，不要用 match 或 quality 重排**。
 *    blend/quality 模式下 match 会出现大小交错，那是预期的：
 *    rank_score = α·归一化匹配 + (1-α)·归一化评分。
 */
export interface Recommendation {
  subject_id: number
  name: string
  year: number | null
  match: number // 与偏好向量的余弦，[-1,1]
  quality: number // 贝叶斯加权评分，[0,10]
  rank_score: number // 排序依据，量纲随 rank_by 变，不要展示给用户
  bgm_score: number | null
  done: number
  reasons: string[] // 对匹配度贡献最大的 tag
}

export interface SearchHit {
  subject_id: number
  name: string
  name_cn: string | null
  year: number | null
  form: string | null
  done: number
  bgm_score: number | null
  via: 'tsv' | 'trgm'
}

/**
 * 一条作答。
 * ⚠️ 传的是**选项**，不是算好的分数 —— 分数与置信度的映射只在服务端
 *    questionnaire.to_rating() 一处维护。在这里复制一份等于埋一个静默漂移。
 */
export interface Answer {
  subject_id: number
  choice: Choice
  score?: number // 仅 choice==='seen' 时必填
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(BASE + path, {
    ...init,
    headers: { 'Content-Type': 'application/json', ...init?.headers },
  })
  if (!r.ok) {
    // 后端的业务校验错误在 detail 里（如「choice='seen' 必须带分数」），
    // 直接透出来，比统一报「请求失败」好排查
    let detail = `${r.status} ${r.statusText}`
    try {
      const body = await r.json()
      if (body?.detail) detail = typeof body.detail === 'string' ? body.detail : JSON.stringify(body.detail)
    } catch {
      /* 响应不是 JSON，用状态码兜底 */
    }
    throw new Error(detail)
  }
  return r.json()
}

export const api = {
  health: () =>
    req<{ status: string; catalog_size: number; with_tag_vec: number }>('/health'),

  questionnaire: (n: number, experience: Experience) =>
    req<{ items: QuestionItem[]; experience: Experience; total: number }>(
      `/questionnaire?n=${n}&experience=${experience}`,
    ),

  recommend: (body: {
    answers: Answer[]
    mode?: Mode
    rank_by?: RankBy
    top_k?: number
  }) =>
    req<{ items: Recommendation[]; used_ratings: number; rank_by: RankBy }>(
      '/recommend',
      { method: 'POST', body: JSON.stringify(body) },
    ),

  search: (q: string) =>
    req<SearchHit[]>(`/search?q=${encodeURIComponent(q)}&limit=10`),
}
