import { useState } from 'react'
import { api, type Mode, type RankBy, type Recommendation } from './api'
import type { AnswerMap } from './storage'
import { toAnswers } from './storage'

const MODE_LABEL: Record<Mode, string> = {
  all: '不限年份',
  season: '当季混合',
  aired: '当季已开播',
  upcoming: '当季未开播',
  recent: '最近半年',
  classic: '经典回顾（2011 前）',
}

export default function RecommendResults({ answers }: { answers: AnswerMap }) {
  const [recs, setRecs] = useState<Recommendation[] | null>(null)
  const [mode, setMode] = useState<Mode>('all')
  const [rankBy, setRankBy] = useState<RankBy>('blend')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const answered = Object.values(answers).filter((a) => a.choice !== 'skip').length

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
    <div>
      {error && (
        <div className="mb-4 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm">
          {error}
        </div>
      )}

      {answered === 0 && (
        <p className="mb-4 text-sm text-(--color-muted)">
          还没有打分数据——先去「填写问卷」或「动画打分」评几部，推荐才有依据。
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2 rounded-xl border border-(--color-line) bg-(--color-surface) px-3 py-2.5">
        <select
          value={mode}
          onChange={(e) => setMode(e.target.value as Mode)}
          className="rounded-lg border border-(--color-line) bg-(--color-page) px-2 py-1.5 text-sm"
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
          className="rounded-lg border border-(--color-line) bg-(--color-page) px-2 py-1.5 text-sm"
          title="match = 纯 tag 模型（第 5 周 baseline 对照用）"
        >
          <option value="blend">匹配度 + 评分</option>
          <option value="match">只看匹配度</option>
          <option value="quality">只看评分</option>
        </select>
        <button
          onClick={getRecs}
          disabled={busy || answered === 0}
          className="ml-auto rounded-lg bg-(--color-brand) px-4 py-1.5 text-sm font-medium text-(--color-on-brand) disabled:opacity-40"
        >
          {busy ? '计算中…' : answered === 0 ? '先去打几个分' : '看推荐'}
        </button>
      </div>

      {recs && (
        <section className="mt-6">
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
