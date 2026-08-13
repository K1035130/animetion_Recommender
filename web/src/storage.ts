/**
 * 游客评分：只存 localStorage，服务端零写入（CLAUDE.md 第 2 节双轨会话）。
 *
 * ⚠️ 存的是**作答选项**（choice + 可选分数），不是算好的权重。
 *    第 6 周做「游客转正」时，把这里的记录批量 POST 进 user_rating 即可 ——
 *    因为格式与 /api/recommend 的请求体完全一致，不需要任何转换。
 */

import type { Answer, Choice } from './api'

const KEY = 'anime-rec:answers:v1'

export type AnswerMap = Record<number, { choice: Choice; score?: number }>

export function load(): AnswerMap {
  try {
    const raw = localStorage.getItem(KEY)
    return raw ? JSON.parse(raw) : {}
  } catch {
    // 存坏了就当没有 —— 宁可重答一遍问卷，也别让首页白屏
    return {}
  }
}

export function save(m: AnswerMap) {
  localStorage.setItem(KEY, JSON.stringify(m))
}

export function clear() {
  localStorage.removeItem(KEY)
}

/** AnswerMap → 请求体。'skip' 不产生记录，用缺失表示。 */
export function toAnswers(m: AnswerMap): Answer[] {
  return Object.entries(m)
    .filter(([, a]) => a.choice !== 'skip')
    .map(([id, a]) => ({
      subject_id: Number(id),
      choice: a.choice,
      ...(a.choice === 'seen' ? { score: a.score } : {}),
    }))
}
