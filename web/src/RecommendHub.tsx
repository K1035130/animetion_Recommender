import { useState } from 'react'
import QuestionnaireCards from './QuestionnaireCards'
import RateSearch from './RateSearch'
import RecommendResults from './RecommendResults'
import { useSession } from './session-context'

type SubTab = 'quiz' | 'rate' | 'recommend'

const SUB_TABS: [SubTab, string, string, string][] = [
  ['quiz', '📋', '填写问卷', '一题一部番，选看过 / 想尝试 / 不感兴趣'],
  ['rate', '⭐', '动画打分', '搜片名直接打分，补问卷没问到的'],
  ['recommend', '🎯', '开始推荐', '按已有打分算出你可能喜欢的番'],
]

const SUB_TAB_TITLE: Record<SubTab, string> = {
  quiz: '填写问卷',
  rate: '动画打分',
  recommend: '开始推荐',
}

export default function RecommendHub() {
  // ⚠️ 默认不选中任何子页——点进「动漫推荐」不该直接开始出题，
  //    要用户自己点「填写问卷」才进入问卷流程。
  const [tab, setTab] = useState<SubTab | null>(null)
  // 🚨 评分状态全部来自 useSession，本组件**不知道用户登没登录** ——
  //    存 localStorage 还是同步到账号由 session.tsx 决定。
  //    见 session.tsx 模块注释第一条。
  const { user, answers, answered, setAnswer, clearAnswers } = useSession()

  function clearAll() {
    void clearAnswers()
  }

  if (tab === null) {
    return (
      <div className="mx-auto flex h-full max-w-3xl flex-col justify-center overflow-y-auto px-4 py-10">
        <div className="mb-8 text-center">
          <h1 className="text-2xl font-semibold">动漫推荐</h1>
          <p className="mt-2 text-sm text-(--color-muted)">
            {answered > 0 ? `已经给 ${answered} 部番打过分` : '先攒点打分数据，推荐才有依据'}
          </p>
          {/* ⚠️ 只是提示，不是门槛 —— 推荐链路零模型，游客照常可用
              （设计文档「登录不是使用门槛」，被修订的只有问答那一半）。 */}
          {!user && answered > 0 && (
            <p className="mt-1 text-xs text-(--color-muted)">
              评分目前只存在这台浏览器里，登录后可跨设备保存
            </p>
          )}
        </div>

        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          {SUB_TABS.map(([t, emoji, title, desc]) => (
            <button
              key={t}
              onClick={() => setTab(t)}
              className="group flex aspect-square flex-col items-center justify-center gap-3 rounded-2xl border border-(--color-line) bg-(--color-surface) p-5 text-center transition hover:-translate-y-0.5 hover:border-(--color-brand) hover:shadow-lg"
            >
              <span className="text-4xl">{emoji}</span>
              <span className="text-base font-medium group-hover:text-(--color-brand)">{title}</span>
              <span className="text-xs leading-relaxed text-(--color-muted)">{desc}</span>
            </button>
          ))}
        </div>

        {answered > 0 && (
          <div className="mt-6 text-center">
            <button onClick={clearAll} className="text-xs text-(--color-muted) underline">
              清空全部打分
            </button>
          </div>
        )}
      </div>
    )
  }

  return (
    <div className="mx-auto h-full max-w-3xl overflow-y-auto px-4 py-6">
      <div className="mb-5 flex flex-wrap items-center gap-2">
        <button
          onClick={() => setTab(null)}
          className="flex items-center gap-1 rounded-lg border border-(--color-line) px-3 py-1.5 text-sm text-(--color-muted) transition hover:border-(--color-brand) hover:text-(--color-brand)"
        >
          <span aria-hidden>←</span> 返回
        </button>
        <span className="text-sm font-medium">{SUB_TAB_TITLE[tab]}</span>
        <span className="ml-auto text-xs text-(--color-muted)">已打分 {answered} 部</span>
        {answered > 0 && (
          <button onClick={clearAll} className="text-xs text-(--color-muted) underline">
            清空重答
          </button>
        )}
      </div>

      {tab === 'quiz' && (
        <QuestionnaireCards answers={answers} onAnswer={setAnswer} onFinish={() => setTab('recommend')} />
      )}
      {tab === 'rate' && <RateSearch answers={answers} onAnswer={setAnswer} />}
      {tab === 'recommend' && <RecommendResults answers={answers} />}
    </div>
  )
}
