"""所有 HTTP 客户端的**统一收尾入口** —— 离线脚本用。

🚨 **为什么需要这个模块（是一个真实发生过的漂移，不是洁癖）**：
   `embed` / `llm` / `rerank` / `translate` 四个模块各自定义了 `close_client()`，
   而 2026-08-20 自检发现 **`llm` 和 `rerank` 那两个从来没有任何调用方** ——
   四个 eval 脚本都用了它们、都只关了 Neon 连接，httpx 的连接池一直挂着。

   ⚠️ 根因不是谁忘了写，是**「该关谁」这件事没有唯一定义处**：
      每加一个带 client 的模块，就要指望作者去翻所有脚本补一行。
      ⇒ 与 CLAUDE.md 那条纪律同构：「新增影响输出的配置时都要问一句，
        它进指纹了吗」—— 这里是「新增带 client 的模块时，它进 `close_all()` 了吗」。
      **加模块时改这一处，所有脚本自动跟上。**

⚠️ **线上（server/）不要调这个。** Vercel 的容器是常驻的，client 跨请求复用正是
   我们要的；关掉只会让下一个请求重建连接。这与 `embed.close_client()` 的
   docstring「长耗时的离线阶段结束后调用；线上常驻不必调」是同一条。

⚠️ **`build_embeddings.py` 里那句 `embed.close_client()` 不要换成本函数。**
   那一句是**阶段性**收尾（API 阶段结束、准备写库），不是进程收尾 ——
   目的是 A.7 那条「长耗时任务不能跨阶段持有连接」，语义不同。

📌 client 都是惰性单例，所以对没用过的模块调 `close_client()` 是纯 no-op，
   脚本不必挑着关。导入这四个模块也没有模块级副作用（不读 env、不建连接）。
"""

from __future__ import annotations

from src import embed, llm, rerank, translate

_MODULES = (embed, llm, rerank, translate)


def close_all() -> None:
    """关掉全部 httpx 客户端。**通常放在 `finally` 里。**

    ⚠️ 单个模块关失败不能打断其余的，也不能让异常盖住 `finally` 之前的真异常
       （那会把"任务为什么失败"换成"收尾为什么失败"，是最难查的一类）。
       ⇒ 逐个吞掉并打印，不向上抛。
    """
    for mod in _MODULES:
        try:
            mod.close_client()
        except Exception as exc:                      # noqa: BLE001
            print(f"  ⚠️ 关闭 {mod.__name__} 的客户端失败：{exc}")
