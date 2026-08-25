"""问答配额：每人 24 小时 10 条（Kevin 2026-08-24 定）。

## 为什么必须走数据库

🚨 线上是 Vercel serverless —— 每个函数实例各自独立、随时被回收，
   **进程内计数器在架构上就不成立**。这与 A.6「永远不能在函数里加载模型」
   同一个根因：没有常驻状态。⇒ 唯一的共享状态是数据库。

## 为什么是滚动窗口而不是自然日

自然日会让人在 23:59 用完 10 条、00:01 再用 10 条 —— 2 分钟内 20 条。
滚动 24 小时没有这个洞。代价是 `count(*)` 而不是读一个计数列，
但每人每窗口最多 10 行，且 `idx_ask_log_user_time` 直接覆盖这个查询。

## 为什么要「先扣后退」而不是「先跑后记」

两种顺序各有一个洞，选了洞更小的那个：

    先跑后记   并发发 20 条请求，20 条全部在计数之前跑完 → 配额形同虚设，
              而这恰恰是要防的那种滥用
    先扣后退   请求失败（LLM 挂了 503）时用户白白损失一条 → 所以
              **失败路径必须退还**（refund），这是本模块存在的复杂度来源

⇒ 采用先扣后退，并且 `reserve()` 与 `refund()` 必须成对出现。
⚠️ 只退**服务端故障**（503：embedding/LLM 不可用）。
   「没认出是哪部作品」「离题被拦下」都是**正常业务结果**，照常计费 ——
   否则一个人可以无限次地问离题问题，配额同样形同虚设。
"""

import psycopg

DAILY_LIMIT = 10
WINDOW = "24 hours"


class QuotaExceeded(Exception):
    """配额用尽。带上 retry_after 让前端能显示「几点之后可以再问」。"""

    def __init__(self, used: int, reset_at):
        self.used = used
        self.reset_at = reset_at
        super().__init__(f"24 小时内已问满 {used} 条")


def used_since(conn: psycopg.Connection, user_id: int) -> int:
    """当前滚动窗口内已用条数。只读，不加锁 —— 展示用。"""
    with conn.cursor() as cur:
        cur.execute(
            f"SELECT count(*) FROM ask_log "
            f"WHERE user_id = %s AND created_at > now() - interval '{WINDOW}'",
            (user_id,),
        )
        return cur.fetchone()[0]


def status(conn: psycopg.Connection, user_id: int) -> dict:
    """给前端的配额面板：已用 / 上限 / 最早那条何时滚出窗口。"""
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT count(*), min(created_at) + interval '{WINDOW}'
            FROM ask_log
            WHERE user_id = %s AND created_at > now() - interval '{WINDOW}'
            """,
            (user_id,),
        )
        used, reset_at = cur.fetchone()
    return {
        "used": used,
        "limit": DAILY_LIMIT,
        "remaining": max(0, DAILY_LIMIT - used),
        # ⚠️ 只有用满了 reset_at 才有意义 —— 没用满时「最早一条何时过期」
        #    对用户毫无信息量（他现在就能问），给了反而让人以为要等。
        "reset_at": reset_at.isoformat() if reset_at and used >= DAILY_LIMIT else None,
    }


def reserve(conn: psycopg.Connection, user_id: int, question: str,
            route: str | None = None) -> int:
    """占一条配额，返回 ask_id（用于失败时 refund）。超限抛 QuotaExceeded。

    🚨 **必须先锁 app_user 那一行。** 不锁的话两个并发请求会同时读到
       count=9，各自判断「还没满」，然后双双插入 → 11 条。
       这是典型的 check-then-act 竞态，而配额检查正是它的教科书场景。
       ⇒ `SELECT ... FOR UPDATE` 把同一个用户的请求串行化。
    ⚠️ 锁的粒度是**单个用户的行**，不是表 —— 不同用户之间零竞争，
       不会因为加锁把并发吞吐压垮。
    ⚠️ 调用方必须在事务里用（psycopg 默认非 autocommit 即可）：
       锁在 COMMIT/ROLLBACK 时才释放，autocommit 下这行锁瞬间就没了，
       等于没加。
    """
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM app_user WHERE user_id = %s FOR UPDATE",
                    (user_id,))
        if cur.fetchone() is None:
            raise ValueError(f"user_id={user_id} 不存在")

        cur.execute(
            f"""
            SELECT count(*), min(created_at) + interval '{WINDOW}'
            FROM ask_log
            WHERE user_id = %s AND created_at > now() - interval '{WINDOW}'
            """,
            (user_id,),
        )
        used, reset_at = cur.fetchone()
        if used >= DAILY_LIMIT:
            raise QuotaExceeded(used, reset_at)

        cur.execute(
            "INSERT INTO ask_log (user_id, question, route) VALUES (%s, %s, %s) "
            "RETURNING ask_id",
            (user_id, question, route),
        )
        return cur.fetchone()[0]


def set_route(conn: psycopg.Connection, ask_id: int, route: str) -> None:
    """回填实际走的分支（reserve 时还不知道会回落到哪条）。"""
    with conn.cursor() as cur:
        cur.execute("UPDATE ask_log SET route = %s WHERE ask_id = %s",
                    (route, ask_id))


def refund(conn: psycopg.Connection, ask_id: int) -> None:
    """退还一条配额。**只在服务端故障时调用**，见模块注释。

    ⚠️ 用 DELETE 而不是打个 refunded 标记：配额判据是 `count(*)`，
       留着行就得给那个查询加 `WHERE NOT refunded`，而漏加一次
       就是静默的配额错误。删掉最简单，也不丢什么 —— 那次请求本来就没成功。
    """
    with conn.cursor() as cur:
        cur.execute("DELETE FROM ask_log WHERE ask_id = %s", (ask_id,))
