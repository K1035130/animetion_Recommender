import { useState } from 'react'
import AskPanel from './AskPanel'
import AuthDialog from './AuthDialog'
import RecommendHub from './RecommendHub'
import { useSession } from './session-context'

type View = 'home' | 'recommend' | 'ask'

export default function App() {
  const [view, setView] = useState<View>('home')
  const [authOpen, setAuthOpen] = useState(false)

  return (
    // ⚠️ 整页 flex 撑满视口，内容区拿 flex-1 —— 子页面里就不用写
    //    calc(100vh - 顶栏高度) 这种硬编码了（改一次顶栏字号就得跟着改）。
    //    min-h-0 不能省：不加的话 flex 子项按内容撑高，问答页的滚动区失效。
    <div className="flex h-screen flex-col">
      {/* ⚠️ 顶栏不套 max-w-3xl：内容区居中是对的，但导航跟着居中会让
          「← 首页 / 动漫推荐」在宽屏上悬在半空，看着像浮层而不是页头。 */}
      <nav className="shrink-0 border-b border-(--color-line) bg-(--color-page)/95 backdrop-blur">
        <div className="flex items-center gap-2.5 px-5 py-3">
          {view === 'home' ? (
            <span className="text-lg font-semibold">动漫推荐</span>
          ) : (
            <>
              <button
                onClick={() => setView('home')}
                className="flex items-center gap-1 text-base text-(--color-muted) transition hover:text-(--color-brand)"
              >
                <span aria-hidden>←</span> 首页
              </button>
              <span className="text-base text-(--color-line)">/</span>
              <span className="text-lg font-semibold">
                {view === 'recommend' ? '动漫推荐' : '动漫问答'}
              </span>
            </>
          )}
          <div className="ml-auto">
            <AccountMenu onOpenAuth={() => setAuthOpen(true)} />
          </div>
        </div>
      </nav>

      {/* ⚠️ main 自己不滚动，滚动交给子页面 —— 问答页要「消息区滚动 +
          输入框钉底」，这需要它自己管滚动容器。 */}
      <main className="min-h-0 flex-1">
        {view === 'home' && <Home onEnter={setView} />}
        {view === 'recommend' && <RecommendHub />}
        {view === 'ask' && <AskPanel onOpenAuth={() => setAuthOpen(true)} />}
      </main>

      {authOpen && <AuthDialog onClose={() => setAuthOpen(false)} />}
    </div>
  )
}

function AccountMenu({ onOpenAuth }: { onOpenAuth: () => void }) {
  const { user, loading, logout } = useSession()

  // ⚠️ 冷启动查登录态时不要渲染「登录」按钮 —— 已登录用户会看到它闪一下
  //    再变成邮箱，像是被登出过。
  if (loading) return <span className="text-xs text-(--color-muted)">…</span>

  if (!user) {
    return (
      <button
        onClick={onOpenAuth}
        className="rounded-lg border border-(--color-line) px-3 py-1.5 text-sm text-(--color-muted) transition hover:border-(--color-brand) hover:text-(--color-brand)"
      >
        登录
      </button>
    )
  }

  return (
    <div className="flex items-center gap-2.5 text-xs">
      <span className="hidden text-(--color-muted) sm:inline" title={user.email}>
        {user.email}
      </span>
      <span
        className="rounded-full bg-(--color-brand)/12 px-2 py-0.5 text-(--color-brand)"
        title={`问答每 24 小时 ${user.quota.limit} 条`}
      >
        今日剩 {user.quota.remaining}
      </span>
      <button
        onClick={() => void logout()}
        className="text-(--color-muted) underline hover:text-(--color-brand)"
      >
        登出
      </button>
    </div>
  )
}

function Home({ onEnter }: { onEnter: (v: View) => void }) {
  const { user, answered } = useSession()
  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col justify-center overflow-y-auto px-4 py-16">
      <header className="mb-10 text-center">
        <h1 className="text-3xl font-semibold">动漫推荐</h1>
        <p className="mt-2 text-sm text-(--color-muted)">
          给看过的番打分，或者直接问点什么
        </p>
      </header>

      <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
        <FeatureCard
          title="动漫推荐"
          desc="填问卷、给动画打分，系统学你的口味，推给你可能会喜欢的番"
          emoji="🎯"
          note={answered > 0 ? `已打分 ${answered} 部` : '游客也能用'}
          onClick={() => onEnter('recommend')}
        />
        <FeatureCard
          title="动漫问答"
          desc="问剧情、问声优、问档期，或者描述一下找番——一个输入框全搞定"
          emoji="💬"
          note={user ? `今日还能问 ${user.quota.remaining} 条` : '需要登录'}
          onClick={() => onEnter('ask')}
        />
      </div>
    </div>
  )
}

function FeatureCard({
  title,
  desc,
  emoji,
  note,
  onClick,
}: {
  title: string
  desc: string
  emoji: string
  note?: string
  onClick: () => void
}) {
  return (
    <button
      onClick={onClick}
      className="group flex flex-col items-start gap-3 rounded-2xl border border-(--color-line) bg-(--color-surface) p-6 text-left transition hover:-translate-y-0.5 hover:border-(--color-brand) hover:shadow-lg"
    >
      <span className="text-3xl">{emoji}</span>
      <span className="text-lg font-medium group-hover:text-(--color-brand)">{title}</span>
      <span className="text-sm leading-relaxed text-(--color-muted)">{desc}</span>
      {note && <span className="text-xs text-(--color-muted)">{note}</span>}
      <span className="mt-1 text-sm text-(--color-brand) opacity-0 transition group-hover:opacity-100">
        进入 →
      </span>
    </button>
  )
}
