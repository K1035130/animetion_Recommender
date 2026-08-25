import { useState } from 'react'
import { useSession } from './session-context'

type Mode = 'login' | 'register'

export default function AuthDialog({
  onClose,
  initialMode = 'login',
  hint,
}: {
  onClose: () => void
  initialMode?: Mode
  hint?: string
}) {
  const { login, register, answered } = useSession()
  const [mode, setMode] = useState<Mode>(initialMode)
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(e: React.FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      if (mode === 'login') await login(username.trim(), password)
      else await register(username.trim(), password)
      onClose()
    } catch (err) {
      setError(String((err as Error).message ?? err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/40 px-4"
      onClick={onClose}
    >
      <form
        onSubmit={submit}
        onClick={(e) => e.stopPropagation()}
        className="w-full max-w-sm rounded-2xl border border-(--color-line) bg-(--color-surface) p-6"
      >
        <div className="mb-1 flex items-center justify-between">
          <h2 className="text-lg font-semibold">{mode === 'login' ? '登录' : '注册'}</h2>
          <button
            type="button"
            onClick={onClose}
            className="text-sm text-(--color-muted) hover:text-(--color-ink)"
          >
            关闭
          </button>
        </div>

        <p className="mb-4 text-xs leading-relaxed text-(--color-muted)">
          {hint ?? '登录后评分会保存到账号，换设备也能用。'}
          {answered > 0 && (
            <>
              <br />
              本机已有 {answered} 部评分，
              {mode === 'register' ? '注册后会一并保存。' : '登录后会并入账号（不覆盖账号里已有的）。'}
            </>
          )}
        </p>

        {error && (
          <div className="mb-3 rounded-lg border border-red-500/40 bg-red-500/10 px-3 py-2 text-xs">
            {error}
          </div>
        )}

        <label className="mb-3 block">
          <span className="mb-1 block text-xs text-(--color-muted)">
            用户名{mode === 'register' && '（2–20 字，可用中文）'}
          </span>
          <input
            type="text"
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            required
            maxLength={20}
            // ⚠️ 用 username 而不是 email —— 浏览器/密码管理器靠这个值决定
            //    自动填什么，写错的话它会往用户名框里填邮箱。
            autoComplete="username"
            className="w-full rounded-lg border border-(--color-line) bg-(--color-page) px-3 py-2 text-sm outline-none focus:border-(--color-brand)"
          />
        </label>

        <label className="mb-4 block">
          <span className="mb-1 block text-xs text-(--color-muted)">
            密码{mode === 'register' && '（至少 8 位）'}
          </span>
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            required
            minLength={mode === 'register' ? 8 : undefined}
            autoComplete={mode === 'register' ? 'new-password' : 'current-password'}
            className="w-full rounded-lg border border-(--color-line) bg-(--color-page) px-3 py-2 text-sm outline-none focus:border-(--color-brand)"
          />
        </label>

        <button
          type="submit"
          disabled={busy}
          className="w-full rounded-lg bg-(--color-brand) px-4 py-2 text-sm font-medium text-(--color-on-brand) disabled:opacity-40"
        >
          {busy ? '处理中…' : mode === 'login' ? '登录' : '注册'}
        </button>

        <button
          type="button"
          onClick={() => {
            setMode(mode === 'login' ? 'register' : 'login')
            setError(null)
          }}
          className="mt-3 w-full text-center text-xs text-(--color-muted) underline"
        >
          {mode === 'login' ? '还没有账号？去注册' : '已有账号？去登录'}
        </button>

        <p className="mt-4 text-[11px] leading-relaxed text-(--color-muted)">
          只存用户名和密码哈希，不收邮箱、不存任何其他个人信息。
          <br />
          ⚠️ 也因此<strong>没有找回密码的途径</strong>，请记好。
        </p>
      </form>
    </div>
  )
}
