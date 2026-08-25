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
  summary: string | null
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

export interface QuotaStatus {
  used: number
  limit: number
  remaining: number
  // 仅在用满时有值：最早那条何时滚出 24 小时窗口。
  reset_at: string | null
}

export interface AuthUser {
  user_id: number
  email: string
  created_at: string
  rating_count: number
  quota: QuotaStatus
}

/** 未登录时访问需要登录的接口。调用方据此弹登录框，而不是当成普通报错。 */
export class UnauthorizedError extends Error {}

/** 问答配额用尽（后端 429）。 */
export class QuotaError extends Error {}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(BASE + path, {
    ...init,
    // ⚠️ 会话是 httpOnly cookie，本地开发时 5173 → 8000 属跨源，
    //    不带 credentials 浏览器不会发这个 cookie —— 表现为「登录成功了
    //    但下一个请求又是未登录」。线上同源其实可以省，但两边写法保持一致
    //    才不会出现「本地好好的，上线就挂」的反面（这里是反过来的版本）。
    credentials: 'include',
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
    // 401/429 是**业务状态**不是故障，调用方要分别处理（弹登录框 / 提示配额），
    // 所以给它们专门的类型 —— 否则只能去 match 报错文案，那是会漂的。
    if (r.status === 401) throw new UnauthorizedError(detail)
    if (r.status === 429) throw new QuotaError(detail)
    throw new Error(detail)
  }
  return r.json()
}

// ── 流程 C · 单一入口问答（POST /api/ask）─────────────────────────
// ⚠️ 对着 server/schemas.py 手写，改后端 schema 后要回来同步（同上）。

export type AskState = 'ok' | 'ambiguous' | 'no_corpus' | 'unknown'
export type AskRoute = 'auto' | 'ask' | 'voice' | 'season' | 'find'

export interface AskChunk {
  chunk_id: number
  section: string | null
  text: string
  kind: 'prose' | 'songs' | 'profile'
  source: 'moegirl' | 'bangumi_char'
  spoiler_level: number
  score: number | null
  pinned: boolean
}

export interface AskCandidate {
  series_root: number
  title: string
  year: number | null
}

export interface VoiceRoleItem {
  character_id: number
  character_name: string | null
  series_root: number
  title: string
  air_year: number | null
  role_type: number | null
  fav_done: number | null
}

export interface VoiceResponse {
  person_id: number
  name: string
  name_cn: string | null
  n_roles: number
  items: VoiceRoleItem[]
}

export interface SeasonItem {
  subject_id: number
  name: string
  name_cn: string | null
  air_date: string
  form: string | null
  done: number
  bgm_score: number | null
}

export interface SeasonResponse {
  year: number
  month: number
  window_start: string
  window_end: string
  total: number
  items: SeasonItem[]
}

export interface FindHit {
  subject_id: number
  name: string
  air_year: number | null
  match: number
}

export interface FindResponse {
  query: string
  items: FindHit[]
}

export interface RelatedWork {
  series_root: number
  subject_id: number
  name: string
  name_cn: string | null
  air_year: number | null
  fav_done: number
  via_role: string
  via_name: string
}

export interface RelatedResponse {
  series_root: number
  title: string | null
  items: RelatedWork[]
}

// ⚠️ 服务端不存会话状态，历史由调用方传入（第 2 节架构铁律，见 J 节）。
export interface AskRequest {
  question: string
  route?: AskRoute
  spoiler?: boolean
  top_k?: number
  scope?: number | null
  history?: [string, string][]
}

export interface AskResponse {
  route: string
  route_reason: string
  state: AskState
  answer: string | null
  series_root: number | null
  title: string | null
  chunks: AskChunk[]
  candidates: AskCandidate[]
  meta: Record<string, unknown>
  voice?: VoiceResponse | null
  season?: SeasonResponse | null
  find?: FindResponse | null
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

  // ⚠️ /ask 端到端实测 10–45 秒（LLM 生成占 78.4%），调用方要给 loading 态。
  //    voice/season 分支零模型，0.3–0.4 秒，但每次请求现在都多一次
  //    classify_intent 校验（0.3–4.9s）——见 CLAUDE.md「意图校验」一节。
  ask: (body: AskRequest) =>
    req<AskResponse>('/ask', { method: 'POST', body: JSON.stringify(body) }),

  related: (seriesRoot: number, role = '', limit = 8) =>
    req<RelatedResponse>(
      `/related?series_root=${seriesRoot}&role=${encodeURIComponent(role)}&limit=${limit}`,
    ),

  // ── 账号系统 ────────────────────────────────────────────────
  // ⚠️ 会话是 httpOnly cookie，前端**读不到也存不了 token**（有意的：
  //    JS 读不到就意味着 XSS 偷不走）。登录态一律靠 me() 现查。
  me: () => req<AuthUser | null>('/auth/me'),

  register: (email: string, password: string, guestRatings: Answer[] = []) =>
    req<AuthUser>('/auth/register', {
      method: 'POST',
      body: JSON.stringify({ email, password, guest_ratings: guestRatings }),
    }),

  login: (email: string, password: string, guestRatings: Answer[] = []) =>
    req<AuthUser>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password, guest_ratings: guestRatings }),
    }),

  logout: () => req<{ ok: boolean }>('/auth/logout', { method: 'POST' }),

  getRatings: () => req<{ items: Answer[] }>('/ratings'),

  // 防抖后批量调用（方案 B）。⚠️ choice='skip' 会删除服务端已有行。
  putRatings: (items: Answer[], source: 'questionnaire' | 'manual' = 'manual') =>
    req<{ written: number; deleted: number; total: number }>('/ratings', {
      method: 'PUT',
      body: JSON.stringify({ items, source }),
    }),

  clearRatings: () => req<{ deleted: number }>('/ratings', { method: 'DELETE' }),
}
