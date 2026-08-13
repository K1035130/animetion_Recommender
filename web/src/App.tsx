import { useEffect, useState } from 'react'
import {
  api,
  type Experience,
  type Mode,
  type QuestionItem,
  type RankBy,
  type Recommendation,
} from './api'
import { type AnswerMap, clear, load, save, toAnswers } from './storage'

const EXPERIENCE_LABEL: Record<Experience, string> = {
  new: '新观众 · 只问最近 5 年',
  mid: '有点资历 · 最近 10 年',
  veteran: '老观众 · 不限年份',
}

const MODE_LABEL: Record<Mode, string> = {
  all: '不限年份',
  season: '当季混合',
  aired: '当季已开播',
  upcoming: '当季未开播',
  recent: '最近半年',
  classic: '经典回顾（2011 前）',
}

export default function App() {
  const [experience, setExperience] = useState<Experience>('mid')
  const [items, setItems] = useState<QuestionItem[]>([])
  const [answers, setAnswers] = useState<AnswerMap>(load)
  const [recs, setRecs] = useState<Recommendation[] | null>(null)
  const [mode, setMode] = useState<Mode>('all')
  const [rankBy, setRankBy] = useState<RankBy>('blend')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setError(null)
    api
      .questionnaire(30, experience)
      .then((r) => setItems(r.items))
      .catch((e) => setError(String(e.message ?? e)))
  }, [experience])

  useEffect(() => {
    save(answers)
  }, [answers])

  const answered = Object.values(answers).filter((a) => a.choice !== 'skip').length

  function setAnswer(id: number, choice: AnswerMap[number]['choice'], score?: number) {
    setAnswers((prev) => ({ ...prev, [id]: { choice, score } }))
  }

  async function getRecs() {
    setBusy(true)
    setError(null)
    try {
      const r = await api.recommend({
        answers: toAnswers(answers),
        mode,
        rank_by: rankBy,
        top_k: 15,
      })
      setRecs(r.items)
    } catch (e) {
      setError(String((e as Error).message ?? e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mx-auto max-w-3xl px-4 py-8">
      <header className="mb-6">
        <h1 className="text-2xl font-semibold">动画推荐</h1>
        <p className="mt-1 text-sm text-(--color-muted)">
          给看过的番打分，系统学你的口味 · 评分只存在这台浏览器里
        </p>
      </header>

      {error && (
        <div className="mb-4 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm">
          {error}
        </div>
      )}

      {/* 资历：只影响出题范围，不影响推荐范围 */}
      <section className="mb-6">
        <div className="mb-2 text-sm font-medium">你看番多久了？</div>
        <div className="flex flex-wrap gap-2">
          {(Object.keys(EXPERIENCE_LABEL) as Experience[]).map((e) => (
            <button
              key={e}
              onClick={() => setExperience(e)}
              className={`rounded-full border px-3 py-1.5 text-sm transition ${
                experience === e
                  ? 'border-(--color-brand) bg-(--color-brand) text-white'
                  : 'border-(--color-line) hover:border-(--color-brand)'
              }`}
            >
              {EXPERIENCE_LABEL[e]}
            </button>
          ))}
        </div>
      </section>

      <section className="mb-6">
        <div className="mb-3 flex items-baseline justify-between">
          <h2 className="text-lg font-medium">
            问卷 <span className="text-sm text-(--color-muted)">已答 {answered} 题</span>
          </h2>
          {answered > 0 && (
            <button
              onClick={() => {
                clear()
                setAnswers({})
                setRecs(null)
              }}
              className="text-xs text-(--color-muted) underline"
            >
              清空重答
            </button>
          )}
        </div>

        <div className="space-y-2">
          {items.map((it) => (
            <QuizRow
              key={it.subject_id}
              item={it}
              answer={answers[it.subject_id]}
              onAnswer={setAnswer}
            />
          ))}
          {items.length === 0 && !error && (
            <div className="text-sm text-(--color-muted)">加载中…</div>
          )}
        </div>
      </section>

      {/* 推荐范围：只作用在候选池过滤，不影响偏好学习 */}
      <section className="sticky bottom-0 -mx-4 border-t border-(--color-line) bg-(--color-page)/95 px-4 py-3 backdrop-blur">
        <div className="flex flex-wrap items-center gap-2">
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as Mode)}
            className="rounded-lg border border-(--color-line) bg-(--color-surface) px-2 py-1.5 text-sm"
          >
            {(Object.keys(MODE_LABEL) as Mode[]).map((m) => (
              <option key={m} value={m}>
                {MODE_LABEL[m]}
              </option>
            ))}
          </select>
          <select
            value={rankBy}
            onChange={(e) => setRankBy(e.target.value as RankBy)}
            className="rounded-lg border border-(--color-line) bg-(--color-surface) px-2 py-1.5 text-sm"
            title="match = 纯 tag 模型（第 5 周 baseline 对照用）"
          >
            <option value="blend">匹配度 + 评分</option>
            <option value="match">只看匹配度</option>
            <option value="quality">只看评分</option>
          </select>
          <button
            onClick={getRecs}
            disabled={busy || answered === 0}
            className="ml-auto rounded-lg bg-(--color-brand) px-4 py-1.5 text-sm font-medium text-white disabled:opacity-40"
          >
            {busy ? '计算中…' : answered === 0 ? '先答几题' : '看推荐'}
          </button>
        </div>
      </section>

      {recs && (
        <section className="mt-8">
          <h2 className="mb-3 text-lg font-medium">推荐结果</h2>
          {recs.length === 0 ? (
            <p className="text-sm text-(--color-muted)">
              没有结果 —— 换个年份范围，或者再多答几题。
            </p>
          ) : (
            <ol className="space-y-2">
              {/* ⚠️ 直接按返回顺序渲染。列表已按 rank_score 降序，不要重排 */}
              {recs.map((r, i) => (
                <RecRow key={r.subject_id} rec={r} rank={i + 1} />
              ))}
            </ol>
          )}
        </section>
      )}
    </div>
  )
}

function QuizRow({
  item,
  answer,
  onAnswer,
}: {
  item: QuestionItem
  answer?: AnswerMap[number]
  onAnswer: (id: number, choice: AnswerMap[number]['choice'], score?: number) => void
}) {
  const seen = answer?.choice === 'seen'
  return (
    <div className="rounded-xl border border-(--color-line) bg-(--color-surface) px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-medium">{item.name}</span>
        <span className="text-xs text-(--color-muted)">
          {item.year} · {item.done.toLocaleString()} 人看过
        </span>
      </div>

      <div className="mt-2 flex flex-wrap gap-1.5">
        {(
          [
            ['seen', '看过'],
            ['wish', '想尝试'],
            ['pass', '不感兴趣'],
            ['skip', '跳过'],
          ] as const
        ).map(([c, label]) => (
          <button
            key={c}
            onClick={() => onAnswer(item.subject_id, c, c === 'seen' ? (answer?.score ?? 8) : undefined)}
            className={`rounded-md border px-2 py-1 text-xs transition ${
              answer?.choice === c
                ? 'border-(--color-brand) bg-(--color-brand) text-white'
                : 'border-(--color-line) hover:border-(--color-brand)'
            }`}
          >
            {label}
          </button>
        ))}

        {/* 「看过」才需要打分。其余选项的分数由服务端 to_rating() 决定 */}
        {seen && (
          <label className="ml-1 flex items-center gap-2 text-xs">
            <input
              type="range"
              min={1}
              max={10}
              step={0.5}
              value={answer?.score ?? 8}
              onChange={(e) => onAnswer(item.subject_id, 'seen', Number(e.target.value))}
              className="w-28 accent-(--color-brand)"
            />
            <span className="w-8 tabular-nums font-medium">{answer?.score ?? 8}</span>
          </label>
        )}
      </div>
    </div>
  )
}

function RecRow({ rec, rank }: { rec: Recommendation; rank: number }) {
  return (
    <li className="flex gap-3 rounded-xl border border-(--color-line) bg-(--color-surface) px-3 py-2.5">
      <span className="w-6 shrink-0 pt-0.5 text-sm tabular-nums text-(--color-muted)">
        {rank}
      </span>
      <div className="min-w-0 flex-1">
        <div className="flex flex-wrap items-baseline gap-x-2">
          <a
            href={`https://bgm.tv/subject/${rec.subject_id}`}
            target="_blank"
            rel="noreferrer"
            className="font-medium hover:text-(--color-brand) hover:underline"
          >
            {rec.name}
          </a>
          <span className="text-xs text-(--color-muted)">{rec.year}</span>
        </div>
        <div className="mt-1 flex flex-wrap items-center gap-1.5">
          {rec.reasons.map((t) => (
            <span
              key={t}
              className="rounded bg-(--color-brand)/12 px-1.5 py-0.5 text-xs text-(--color-brand)"
            >
              {t}
            </span>
          ))}
        </div>
      </div>
      <div className="shrink-0 text-right text-xs tabular-nums text-(--color-muted)">
        <div>评分 {rec.bgm_score?.toFixed(1) ?? '—'}</div>
        <div>匹配 {(rec.match * 100).toFixed(0)}%</div>
      </div>
    </li>
  )
}
