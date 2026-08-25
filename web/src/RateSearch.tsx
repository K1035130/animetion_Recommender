import { useEffect, useState } from 'react'
import { api, type Choice, type SearchHit } from './api'
import type { AnswerMap } from './storage'

const CHOICES: [Choice, string][] = [
  ['seen', '看过'],
  ['wish', '想尝试'],
  ['pass', '不感兴趣'],
]

export default function RateSearch({
  answers,
  onAnswer,
}: {
  answers: AnswerMap
  onAnswer: (id: number, choice: Choice, score?: number) => void
}) {
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<SearchHit[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    const query = q.trim()
    if (!query) {
      setHits([])
      setError(null)
      return
    }
    setBusy(true)
    const t = window.setTimeout(() => {
      api
        .search(query)
        .then((r) => setHits(r))
        .catch((e) => setError(String(e.message ?? e)))
        .finally(() => setBusy(false))
    }, 300)
    return () => window.clearTimeout(t)
  }, [q])

  return (
    <div>
      <p className="mb-3 text-sm text-(--color-muted)">
        搜任意作品直接打分——问卷没抽到的、冷门的都能补上。
      </p>
      <input
        value={q}
        onChange={(e) => setQ(e.target.value)}
        placeholder="搜作品名…"
        className="w-full rounded-lg border border-(--color-line) bg-(--color-surface) px-3 py-2 text-sm outline-none focus:border-(--color-brand)"
      />

      {error && (
        <div className="mt-3 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-sm">
          {error}
        </div>
      )}

      <ul className="mt-3 space-y-2">
        {hits.map((h) => (
          <HitRow key={h.subject_id} hit={h} answer={answers[h.subject_id]} onAnswer={onAnswer} />
        ))}
        {!busy && q.trim() && hits.length === 0 && !error && (
          <p className="text-sm text-(--color-muted)">没搜到，换个写法试试。</p>
        )}
      </ul>
    </div>
  )
}

function HitRow({
  hit,
  answer,
  onAnswer,
}: {
  hit: SearchHit
  answer?: AnswerMap[number]
  onAnswer: (id: number, choice: Choice, score?: number) => void
}) {
  const seen = answer?.choice === 'seen'
  return (
    <li className="rounded-xl border border-(--color-line) bg-(--color-surface) px-3 py-2.5">
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-medium">{hit.name_cn ?? hit.name}</span>
        <span className="text-xs text-(--color-muted)">
          {hit.year} · {hit.done.toLocaleString()} 人看过
          {hit.bgm_score ? ` · 评分 ${hit.bgm_score.toFixed(1)}` : ''}
        </span>
      </div>

      <div className="mt-2 flex flex-wrap items-center gap-1.5">
        {CHOICES.map(([c, label]) => (
          <button
            key={c}
            onClick={() => onAnswer(hit.subject_id, c, c === 'seen' ? (answer?.score ?? 8) : undefined)}
            className={`rounded-md border px-2 py-1 text-xs transition ${
              answer?.choice === c
                ? 'border-(--color-brand) bg-(--color-brand) text-(--color-on-brand)'
                : 'border-(--color-line) hover:border-(--color-brand)'
            }`}
          >
            {label}
          </button>
        ))}

        {seen && (
          <label className="ml-1 flex items-center gap-2 text-xs">
            <input
              type="range"
              min={1}
              max={10}
              step={0.5}
              value={answer?.score ?? 8}
              onChange={(e) => onAnswer(hit.subject_id, 'seen', Number(e.target.value))}
              className="w-28 accent-(--color-brand)"
            />
            <span className="w-8 tabular-nums font-medium">{answer?.score ?? 8}</span>
          </label>
        )}
      </div>
    </li>
  )
}
