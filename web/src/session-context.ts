/**
 * 会话 context 的类型与 hook。
 *
 * ⚠️ **与 SessionProvider 分成两个文件是有理由的，别合回去。**
 *    一个文件里既导出组件又导出非组件（hook / 常量），Vite 的
 *    React Fast Refresh 就失效 —— 改 session.tsx 会触发整页刷新而不是
 *    热更新，开发时正在填的问卷、正在打的字全没了。
 *    （oxlint 的 react(only-export-components) 规则查的就是这个。）
 */

import { createContext, useContext } from 'react'
import type { AnswerMap } from './storage'
import type { AuthUser, Choice } from './api'

export interface SessionValue {
  user: AuthUser | null
  loading: boolean
  answers: AnswerMap
  answered: number
  setAnswer: (id: number, choice: Choice, score?: number) => void
  clearAnswers: () => Promise<void>
  login: (username: string, password: string) => Promise<void>
  register: (username: string, password: string) => Promise<void>
  logout: () => Promise<void>
  /** /ask 成功后回传剩余配额，免得再查一次 /auth/me。 */
  setQuotaRemaining: (n: number) => void
  refreshUser: () => Promise<void>
}

export const SessionCtx = createContext<SessionValue | null>(null)

export function useSession(): SessionValue {
  const v = useContext(SessionCtx)
  if (!v) throw new Error('useSession 必须在 <SessionProvider> 内使用')
  return v
}
