import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  api,
  QuotaError,
  UnauthorizedError,
  type AskCandidate,
  type AskResponse,
  type AskRoute,
} from './api'
import { useSession } from './session-context'
import { USAGE_NOTICE } from './usageNotice'

const ROUTE_LABEL: Record<AskRoute, string> = {
  auto: '自动',
  ask: '剧情',
  voice: '声优',
  season: '新番',
  find: '找番',
}

// 展示用：实际 route 可能与用户选的按钮不同（回落/意图校验改判），
// 这里只是把后端返回的字符串映一个中文标签，认不出就原样显示。
const ROUTE_DISPLAY: Record<string, string> = {
  ask: '剧情问答',
  voice: '声优配役',
  season: '新番档期',
  find: '语义找番',
}

const SUGGESTIONS = [
  '《灰羽联盟》讲了什么故事？',
  '安兹·乌尔·恭是谁？',
  '花泽香菜配过哪些角色？',
  '十年前的这个季度在播什么？',
  '有没有主角很强但很低调的番？',
]

const TEXTAREA_MAX_PX = 200

interface Turn {
  id: number
  question: string
  routeRequested: AskRoute
  scopeAtAsk: number | null
  loading: boolean
  response: AskResponse | null
  error: string | null
  elapsedMs: number
}

let turnSeq = 0

export default function AskPanel({ onOpenAuth }: { onOpenAuth: () => void }) {
  const { user, loading: sessionLoading, refreshUser, setQuotaRemaining } = useSession()
  const [turns, setTurns] = useState<Turn[]>([])
  const [question, setQuestion] = useState('')
  const [routeChoice, setRouteChoice] = useState<AskRoute>('auto')
  const [spoiler, setSpoiler] = useState(false)
  const [scope, setScope] = useState<{ id: number; title: string } | null>(null)
  const [noticeOpen, setNoticeOpen] = useState(false)

  const scrollRef = useRef<HTMLDivElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)

  const busy = turns.some((t) => t.loading)

  useEffect(() => {
    const el = scrollRef.current
    if (el) el.scrollTo({ top: el.scrollHeight, behavior: 'smooth' })
  }, [turns])

  // 输入框跟着内容长高，到 TEXTAREA_MAX_PX 为止再内部滚动。
  useLayoutEffect(() => {
    const el = textareaRef.current
    if (!el) return
    el.style.height = 'auto'
    el.style.height = `${Math.min(el.scrollHeight, TEXTAREA_MAX_PX)}px`
  }, [question])

  function buildHistory(): [string, string][] {
    // ⚠️ 服务端不存会话状态（J 节），历史全靠客户端回传。
    //    服务端自己还会再截一次（MAX_HISTORY_TURNS=3），这里多传几轮也无妨。
    return turns
      .filter((t) => t.response?.answer)
      .slice(-4)
      .map((t) => [t.question, t.response!.answer as string])
  }

  async function submit(q: string, route: AskRoute, scopeId: number | null) {
    const text = q.trim()
    if (!text) return
    const id = ++turnSeq
    const history = buildHistory()
    const startedAt = performance.now()
    setTurns((prev) => [
      ...prev,
      { id, question: text, routeRequested: route, scopeAtAsk: scopeId, loading: true, response: null, error: null, elapsedMs: 0 },
    ])

    const ticker = window.setInterval(() => {
      setTurns((prev) =>
        prev.map((t) => (t.id === id ? { ...t, elapsedMs: performance.now() - startedAt } : t)),
      )
    }, 400)

    try {
      const res = await api.ask({
        question: text,
        route,
        spoiler,
        top_k: 8,
        scope: scopeId,
        history,
      })
      setTurns((prev) => prev.map((t) => (t.id === id ? { ...t, loading: false, response: res } : t)))
      // 每问一条就少一条，顶栏的「今日剩 N」要跟着动。
      // ⚠️ 走本地递减而不是重查 /auth/me：少一次往返，而真实值下次
      //    进页面/刷新时自然会对齐。
      if (user) setQuotaRemaining(Math.max(0, user.quota.remaining - 1))
    } catch (e) {
      // 401/429 是业务状态不是故障，各自给出下一步动作（登录 / 等配额），
      // 而不是甩一句「请求失败」。
      if (e instanceof UnauthorizedError) {
        void refreshUser()          // token 可能过期了，同步一下顶栏状态
        onOpenAuth()
      }
      if (e instanceof QuotaError) void refreshUser()
      setTurns((prev) =>
        prev.map((t) => (t.id === id ? { ...t, loading: false, error: String((e as Error).message ?? e) } : t)),
      )
    } finally {
      window.clearInterval(ticker)
    }
  }

  function send() {
    if (busy || !question.trim()) return
    const q = question
    setQuestion('')
    void submit(q, routeChoice, scope?.id ?? null)
  }

  function onKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    // ⚠️ 必须放行输入法组合中的 Enter —— 中文用户按回车是在选候选词，
    //    不判 isComposing 会把没打完的半截问题直接发出去。
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault()
      send()
    }
  }

  function pickCandidate(originalQuestion: string, c: AskCandidate) {
    setScope({ id: c.series_root, title: c.title })
    void submit(originalQuestion, routeChoice, c.series_root)
  }

  // 🚨 游客不能用问答（Kevin 2026-08-24 定）。这修订了设计文档那句
  //    「游客能用全部功能」—— 修订的理由是成本结构：推荐链路全程零模型，
  //    而问答每条至少两次模型调用，必须绑到账号上才能限流。
  //    ⚠️ 前端这一层只是体验，**真正的门在服务端**（_require_user）：
  //       前端拦不住直接打 API 的人，也不该假装拦得住。
  if (!sessionLoading && !user) return <GuestGate onOpenAuth={onOpenAuth} />

  const outOfQuota = user != null && user.quota.remaining <= 0

  return (
    <div className="flex h-full flex-col">
      <div ref={scrollRef} className="flex-1 overflow-y-auto">
        <div className="mx-auto max-w-3xl px-4 py-6">
          {turns.length === 0 ? (
            <EmptyState onPick={(s) => setQuestion(s)} />
          ) : (
            <div className="space-y-7">
              {turns.map((t) => (
                <TurnView key={t.id} turn={t} onPickCandidate={pickCandidate} />
              ))}
            </div>
          )}
        </div>
      </div>

      <div className="shrink-0 bg-(--color-page) px-4 pb-4">
        <div className="mx-auto max-w-3xl">
          {scope && (
            <div className="mb-2 flex items-center gap-2 rounded-lg border border-(--color-brand)/40 bg-(--color-brand)/10 px-3 py-1.5 text-xs">
              <span>作用域已锁定：《{scope.title}》</span>
              <button onClick={() => setScope(null)} className="ml-auto text-(--color-muted) underline">
                清除
              </button>
            </div>
          )}

          <div className="rounded-2xl border border-(--color-line) bg-(--color-surface) shadow-sm transition focus-within:border-(--color-brand)/60">
            <textarea
              ref={textareaRef}
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={onKeyDown}
              rows={1}
              maxLength={200}
              placeholder="问点什么…"
              className="block w-full resize-none bg-transparent px-4 pt-3.5 pb-2 text-sm leading-relaxed outline-none placeholder:text-(--color-muted)"
            />

            <div className="flex flex-wrap items-center gap-1.5 px-3 pb-2.5">
              {(Object.keys(ROUTE_LABEL) as AskRoute[]).map((r) => (
                <button
                  key={r}
                  type="button"
                  onClick={() => setRouteChoice(r)}
                  className={`rounded-full border px-2.5 py-1 text-xs transition ${
                    routeChoice === r
                      ? 'border-(--color-brand) bg-(--color-brand) text-(--color-on-brand)'
                      : 'border-(--color-line) text-(--color-muted) hover:border-(--color-brand) hover:text-(--color-brand)'
                  }`}
                >
                  {ROUTE_LABEL[r]}
                </button>
              ))}

              <label
                title="放开剧透门控（默认过滤已标记的剧透内容）"
                className={`ml-1 flex cursor-pointer items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs transition ${
                  spoiler
                    ? 'border-(--color-warn) text-(--color-warn)'
                    : 'border-(--color-line) text-(--color-muted) hover:border-(--color-warn)'
                }`}
              >
                <input
                  type="checkbox"
                  checked={spoiler}
                  onChange={(e) => setSpoiler(e.target.checked)}
                  className="h-3 w-3 accent-(--color-warn)"
                />
                允许剧透
              </label>

              <button
                type="button"
                onClick={() => setNoticeOpen(true)}
                title="使用须知"
                className="ml-auto flex h-7 w-7 shrink-0 items-center justify-center rounded-full border border-(--color-line) text-xs text-(--color-muted) transition hover:border-(--color-brand) hover:text-(--color-brand)"
              >
                ?
              </button>

              <button
                type="button"
                onClick={send}
                disabled={busy || outOfQuota || !question.trim()}
                aria-label="发送"
                title={outOfQuota ? '24 小时配额已用完' : undefined}
                className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-(--color-brand) text-(--color-on-brand) transition disabled:opacity-30"
              >
                {busy ? (
                  <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-current border-t-transparent" />
                ) : (
                  <span aria-hidden className="text-sm leading-none">↑</span>
                )}
              </button>
            </div>
          </div>

          <p className="mt-2 text-center text-[11px] text-(--color-muted)">
            {outOfQuota ? (
              <span className="text-(--color-warn)">
                24 小时内的 {user?.quota.limit} 条已用完
                {user?.quota.reset_at
                  ? `，${new Date(user.quota.reset_at).toLocaleString('zh-CN', {
                      month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit',
                    })} 之后恢复一条`
                  : '，请稍后再来'}
              </span>
            ) : (
              <>
                回答可能有错或包含剧透，请核对出处 · Enter 发送，Shift+Enter 换行
                {user && ` · 今日还能问 ${user.quota.remaining} 条`}
              </>
            )}
          </p>
        </div>
      </div>

      {noticeOpen && <NoticeDrawer onClose={() => setNoticeOpen(false)} />}
    </div>
  )
}

function GuestGate({ onOpenAuth }: { onOpenAuth: () => void }) {
  return (
    <div className="flex h-full flex-col items-center justify-center px-4 text-center">
      <span className="text-4xl">💬</span>
      <h1 className="mt-4 text-2xl font-semibold">问答需要登录</h1>
      <p className="mt-2 max-w-md text-sm leading-relaxed text-(--color-muted)">
        每条问答都要调用大模型，所以要绑定账号来限流：
        <strong className="text-(--color-ink)">每 24 小时 10 条</strong>。
      </p>
      <p className="mt-1 max-w-md text-sm leading-relaxed text-(--color-muted)">
        推荐、问卷、打分不受影响，不登录也能用。
      </p>
      <button
        onClick={onOpenAuth}
        className="mt-6 rounded-lg bg-(--color-brand) px-5 py-2 text-sm font-medium text-(--color-on-brand)"
      >
        登录 / 注册
      </button>
    </div>
  )
}

function EmptyState({ onPick }: { onPick: (s: string) => void }) {
  return (
    <div className="flex min-h-[45vh] flex-col items-center justify-center text-center">
      <h1 className="text-2xl font-semibold">有什么想问的？</h1>
      <p className="mt-2 text-sm text-(--color-muted)">
        剧情、角色、声优、档期，或者描述一下想看的番
      </p>
      <div className="mt-6 flex flex-wrap justify-center gap-2">
        {SUGGESTIONS.map((s) => (
          <button
            key={s}
            onClick={() => onPick(s)}
            className="rounded-full border border-(--color-line) bg-(--color-surface) px-3 py-1.5 text-xs text-(--color-muted) transition hover:border-(--color-brand) hover:text-(--color-brand)"
          >
            {s}
          </button>
        ))}
      </div>
    </div>
  )
}

function TurnView({
  turn,
  onPickCandidate,
}: {
  turn: Turn
  onPickCandidate: (originalQuestion: string, c: AskCandidate) => void
}) {
  return (
    <div className="space-y-3">
      <div className="flex justify-end">
        <div className="max-w-[80%] rounded-2xl bg-(--color-line)/50 px-4 py-2.5 text-sm leading-relaxed whitespace-pre-wrap">
          {turn.question}
          {turn.scopeAtAsk != null && (
            <div className="mt-1 text-[11px] text-(--color-muted)">已限定作用域</div>
          )}
        </div>
      </div>

      {/* ⚠️ 回答不套气泡：这条链路的回答动辄几百字带出处列表，
          包进气泡会挤成窄条，平铺可读性好得多（Claude 网页版同理）。 */}
      <div className="text-sm">
        {turn.loading && (
          <div className="flex items-center gap-2 text-(--color-muted)">
            <span className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-(--color-brand) border-t-transparent" />
            <span>
              思考中…{turn.elapsedMs > 1500 ? `${(turn.elapsedMs / 1000).toFixed(0)}s` : ''}
              {turn.elapsedMs > 6000 ? '（剧情问答通常要 10–45 秒）' : ''}
            </span>
          </div>
        )}
        {turn.error && <div className="text-(--color-danger)">请求失败：{turn.error}</div>}
        {turn.response && (
          <ResponseView res={turn.response} question={turn.question} onPickCandidate={onPickCandidate} />
        )}
      </div>
    </div>
  )
}

function ResponseView({
  res,
  question,
  onPickCandidate,
}: {
  res: AskResponse
  question: string
  onPickCandidate: (originalQuestion: string, c: AskCandidate) => void
}) {
  const displayRoute = ROUTE_DISPLAY[res.route] ?? res.route
  // season/find 成功时，answer 是同一份数据的文本复述——有结构化视图就不
  // 重复展示纯文本，避免同一条信息出现两遍。
  //
  // 🚨 **voice 是例外（2026-08-25 起）。** 它的 answer 不再是列表的复述，
  //    而是 LLM 针对问题写的回答（「最近配过什么」会挑年份最新的几部讲），
  //    藏起来等于把这次改动整个白做。判据用 meta.llm 在不在：LLM 挂掉时
  //    后端会回落成名单文本，那种情况下它**确实**是复述，仍然该藏。
  const voiceHasProse = Boolean(res.voice) && Boolean(res.meta?.llm)
  const hasStructured =
    Boolean(res.season || res.find) || (Boolean(res.voice) && !voiceHasProse)
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-(--color-muted)">
        <span
          className="rounded bg-(--color-brand)/12 px-1.5 py-0.5 text-(--color-brand)"
          title={res.route_reason}
        >
          走了：{displayRoute}
        </span>
        {res.route_reason && <span className="italic">{res.route_reason}</span>}
      </div>

      {res.answer && !hasStructured && (
        <p className="leading-7 whitespace-pre-wrap">{res.answer}</p>
      )}

      {res.state === 'ambiguous' && res.candidates.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {res.candidates.map((c) => (
            <button
              key={c.series_root}
              onClick={() => onPickCandidate(question, c)}
              className="rounded-full border border-(--color-line) px-2.5 py-1 text-xs transition hover:border-(--color-brand) hover:text-(--color-brand)"
            >
              《{c.title}》{c.year ? `（${c.year}）` : ''}
            </button>
          ))}
        </div>
      )}

      {res.route === 'ask' && res.state === 'ok' && res.chunks.length > 0 && (
        <details className="rounded-lg border border-(--color-line) px-2.5 py-1.5">
          <summary className="cursor-pointer text-xs text-(--color-muted)">
            查看出处（{res.chunks.length} 条）
          </summary>
          <ul className="mt-2 space-y-1.5">
            {res.chunks.map((ch) => (
              <li key={ch.chunk_id} className="rounded border border-(--color-line) bg-(--color-page) px-2 py-1.5 text-xs">
                <div className="mb-1 flex flex-wrap items-center gap-1 text-(--color-muted)">
                  {ch.section && <span className="font-medium text-(--color-ink)">【{ch.section}】</span>}
                  <span className="rounded bg-(--color-line)/40 px-1">{ch.kind}</span>
                  <span>{ch.source === 'moegirl' ? '萌娘百科' : 'Bangumi'}</span>
                  {ch.pinned && <span className="rounded bg-(--color-brand)/12 px-1 text-(--color-brand)">精确命中</span>}
                  {ch.spoiler_level > 0 && <span className="rounded bg-red-500/10 px-1 text-(--color-danger)">含剧透</span>}
                </div>
                <p className="whitespace-pre-wrap text-(--color-muted)">{ch.text}</p>
              </li>
            ))}
          </ul>
        </details>
      )}

      {res.voice && (
        <div className="rounded-lg border border-(--color-line) px-2.5 py-2 text-xs">
          <div className="mb-1.5 font-medium">
            {res.voice.name_cn ?? res.voice.name} · 共 {res.voice.n_roles} 个配役
          </div>
          <ul className="space-y-1">
            {res.voice.items.map((r) => (
              <li key={`${r.character_id}-${r.series_root}`} className="flex justify-between gap-2 text-(--color-muted)">
                <span>
                  {r.character_name ?? '（未知角色名）'} · 《{r.title}》
                  {r.air_year ? `（${r.air_year}）` : ''}
                </span>
                {r.role_type === 1 && <span className="shrink-0 text-(--color-brand)">主角</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {res.season && (
        <div className="rounded-lg border border-(--color-line) px-2.5 py-2 text-xs">
          <div className="mb-1.5 font-medium">
            {res.season.year} 年 {res.season.month} 月番 · 共 {res.season.total} 部
          </div>
          <ul className="space-y-1">
            {res.season.items.map((it) => (
              <li key={it.subject_id} className="flex justify-between gap-2 text-(--color-muted)">
                <span>{it.name_cn ?? it.name}</span>
                <span className="shrink-0">{it.air_date}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {res.find && (
        <div className="rounded-lg border border-(--color-line) px-2.5 py-2 text-xs">
          <ul className="space-y-1">
            {res.find.items.map((h, i) => (
              <li key={h.subject_id} className="flex justify-between gap-2 text-(--color-muted)">
                <span>
                  {i + 1}. {h.name}
                  {h.air_year ? `（${h.air_year}）` : ''}
                </span>
                <a
                  href={`https://bgm.tv/subject/${h.subject_id}`}
                  target="_blank"
                  rel="noreferrer"
                  className="shrink-0 text-(--color-brand) hover:underline"
                >
                  详情
                </a>
              </li>
            ))}
          </ul>
        </div>
      )}

      {res.series_root && (
        <a
          href={`https://bgm.tv/subject/${res.series_root}`}
          target="_blank"
          rel="noreferrer"
          className="inline-block text-[11px] text-(--color-brand) hover:underline"
        >
          {res.title ?? '查看条目'} → Bangumi
        </a>
      )}
    </div>
  )
}

function NoticeDrawer({ onClose }: { onClose: () => void }) {
  return (
    <div
      className="fixed inset-0 z-50 flex items-end justify-center bg-black/40 sm:items-center"
      onClick={onClose}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="max-h-[85vh] w-full max-w-lg overflow-y-auto rounded-t-2xl border border-(--color-line) bg-(--color-surface) p-5 sm:rounded-2xl"
      >
        <div className="mb-3 flex items-center justify-between">
          <h2 className="text-base font-medium">使用须知</h2>
          <button onClick={onClose} className="text-sm text-(--color-muted) hover:text-(--color-ink)">
            关闭
          </button>
        </div>
        <pre className="whitespace-pre-wrap font-sans text-xs leading-relaxed text-(--color-muted)">
          {USAGE_NOTICE}
        </pre>
      </div>
    </div>
  )
}
