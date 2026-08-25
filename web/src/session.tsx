/**
 * 会话与评分的单一入口。
 *
 * 🚨 **上层组件不该知道用户登没登录。** QuestionnaireCards / RateSearch /
 *    RecommendResults 拿到的永远是 `{answers, setAnswer}`，数据存哪由本模块
 *    决定 —— 这是服务端那条「评分随请求传入，推荐链路不知道评分来自
 *    localStorage 还是 user_rating」铁律在前端的对应物。
 *    ⚠️ 一旦让某个组件自己判断 `if (user) ... else ...`，这条铁律就破了，
 *       而且会破在多处、各自漂移。
 *
 * ## 同步策略：防抖批量（方案 B，Kevin 2026-08-24 定）
 *
 *     A 每次点选项立刻 PUT     最不易丢，但问卷答 30 题 = 30 次请求
 *     B 防抖 1.2 秒批量 PUT    ← 采用
 *     C 只在点「开始推荐」时 PUT  请求最少，中途关页面全丢
 *
 * ⚠️ **防抖必须配 flush**：只防抖不 flush 的话，用户点完最后一题
 *    立刻关标签页，那一批就永远丢了 —— 而「答完就关」恰恰是最常见的
 *    使用方式。`visibilitychange` 比 `beforeunload` 可靠（移动端浏览器
 *    经常不触发后者）。
 *
 * ⚠️ **只同步"脏"的那些**，不是每次全量 PUT。全量的话每次都要传几百条，
 *    而且会把服务端的 updated_at 全部刷新一遍，那一列就再也说明不了
 *    「这条评分是什么时候改的」。
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, UnauthorizedError, type Answer, type AuthUser, type Choice } from './api'
import { SessionCtx, type SessionValue } from './session-context'
import { type AnswerMap, clear as clearLocal, load, save, toAnswers } from './storage'

const SYNC_DEBOUNCE_MS = 1200

export function SessionProvider({ children }: { children: React.ReactNode }) {
  const [user, setUser] = useState<AuthUser | null>(null)
  const [loading, setLoading] = useState(true)
  const [answers, setAnswers] = useState<AnswerMap>(load)

  // 待同步的 subject_id。⚠️ 用 ref 不用 state：它变了不该触发重渲染，
  // 而且防抖回调要读到最新值（state 会被闭包捕获成旧值）。
  const dirtyRef = useRef<Set<number>>(new Set())
  const timerRef = useRef<number | null>(null)
  const answersRef = useRef(answers)
  answersRef.current = answers
  const userRef = useRef(user)
  userRef.current = user

  const flush = useCallback(async () => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current)
      timerRef.current = null
    }
    if (!userRef.current || dirtyRef.current.size === 0) return
    const ids = [...dirtyRef.current]
    dirtyRef.current.clear()
    const map = answersRef.current
    const items: Answer[] = ids.map((id) => ({
      subject_id: id,
      choice: map[id]?.choice ?? 'skip',   // 已被删掉的当 skip 传，服务端会删行
      ...(map[id]?.choice === 'seen' ? { score: map[id]?.score } : {}),
    }))
    try {
      await api.putRatings(items, 'questionnaire')
    } catch (e) {
      // ⚠️ 同步失败就把 id 放回去，下次再试 —— 直接吞掉的话用户的评分
      //    会静默丢失，而他完全看不出来（本地显示得好好的）。
      ids.forEach((id) => dirtyRef.current.add(id))
      if (e instanceof UnauthorizedError) setUser(null)
    }
  }, [])

  // 冷启动：查一次登录态；已登录就用服务端的评分覆盖本地视图。
  useEffect(() => {
    let alive = true
    api
      .me()
      .then(async (u) => {
        if (!alive) return
        setUser(u)
        if (u) {
          const { items } = await api.getRatings()
          if (!alive) return
          const m: AnswerMap = {}
          for (const a of items) m[a.subject_id] = { choice: a.choice, score: a.score ?? undefined }
          setAnswers(m)
        }
      })
      .catch(() => {
        /* 查不到登录态就当游客，不打断页面 */
      })
      .finally(() => alive && setLoading(false))
    return () => {
      alive = false
    }
  }, [])

  // 页面被隐藏/关闭前把待同步的推出去（见模块注释「必须配 flush」）。
  useEffect(() => {
    const onHide = () => {
      if (document.visibilityState === 'hidden') void flush()
    }
    document.addEventListener('visibilitychange', onHide)
    window.addEventListener('pagehide', onHide)
    return () => {
      document.removeEventListener('visibilitychange', onHide)
      window.removeEventListener('pagehide', onHide)
    }
  }, [flush])

  const setAnswer = useCallback(
    (id: number, choice: Choice, score?: number) => {
      setAnswers((prev) => {
        const next = { ...prev, [id]: { choice, score } }
        // ⚠️ 无论登没登录都写一份本地：登录用户的本地副本在网络断了时
        //    仍然能用，而且下次登录会作为 guest_ratings 补空缺。
        save(next)
        answersRef.current = next
        return next
      })
      if (userRef.current) {
        dirtyRef.current.add(id)
        if (timerRef.current !== null) window.clearTimeout(timerRef.current)
        timerRef.current = window.setTimeout(() => void flush(), SYNC_DEBOUNCE_MS)
      }
    },
    [flush],
  )

  const clearAnswers = useCallback(async () => {
    dirtyRef.current.clear()
    if (timerRef.current !== null) window.clearTimeout(timerRef.current)
    clearLocal()
    setAnswers({})
    answersRef.current = {}
    if (userRef.current) {
      try {
        await api.clearRatings()
      } catch {
        /* 清云端失败不阻断本地清空，下次同步会纠正 */
      }
    }
  }, [])

  const adopt = useCallback((u: AuthUser, merged: Answer[]) => {
    setUser(u)
    const m: AnswerMap = {}
    for (const a of merged) m[a.subject_id] = { choice: a.choice, score: a.score ?? undefined }
    setAnswers(m)
    answersRef.current = m
    save(m)
    dirtyRef.current.clear()
  }, [])

  const login = useCallback(
    async (email: string, password: string) => {
      // 把本地评分一起带上：服务端按「云端为准、本地只补空缺」合并。
      const u = await api.login(email, password, toAnswers(answersRef.current))
      const { items } = await api.getRatings()
      adopt(u, items)
    },
    [adopt],
  )

  const register = useCallback(
    async (email: string, password: string) => {
      const u = await api.register(email, password, toAnswers(answersRef.current))
      const { items } = await api.getRatings()
      adopt(u, items)
    },
    [adopt],
  )

  const logout = useCallback(async () => {
    await flush()               // ⚠️ 先把未同步的推上去再登出，否则那批就丢了
    await api.logout()
    setUser(null)
    // ⚠️ **登出不清本地评分**：那是这台浏览器上的游客数据，用户只是退出账号
    //    不是要删数据。清掉的话「登出 → 发现还想用 → 重新登录」中间的
    //    那段游客体验就是空白的。
  }, [flush])

  const setQuotaRemaining = useCallback((n: number) => {
    setUser((u) =>
      u ? { ...u, quota: { ...u.quota, remaining: n, used: u.quota.limit - n } } : u,
    )
  }, [])

  const refreshUser = useCallback(async () => {
    try {
      setUser(await api.me())
    } catch {
      /* 忽略 */
    }
  }, [])

  const answered = useMemo(
    () => Object.values(answers).filter((a) => a.choice !== 'skip').length,
    [answers],
  )

  const value = useMemo<SessionValue>(
    () => ({
      user, loading, answers, answered, setAnswer, clearAnswers,
      login, register, logout, setQuotaRemaining, refreshUser,
    }),
    [user, loading, answers, answered, setAnswer, clearAnswers,
     login, register, logout, setQuotaRemaining, refreshUser],
  )

  return <SessionCtx.Provider value={value}>{children}</SessionCtx.Provider>
}
