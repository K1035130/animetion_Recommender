"""bge-reranker 客户端 —— 检索层第 ③ 步（CLAUDE.md G.6）。

⚠️ **本模块与 src/embed.py 的约束完全不同，别照搬那边的纪律。**

    embedding   库里 89,544 条向量是它定义的坐标系  → **锁死，绝不能 fallback**
    reranker    输出是「给定 query 对这批文档的相对排序」，用完即弃，
                不进库、不与任何存量坐标比较        → **可以换模型**

   A.8 那条「换 embedding 模型不报错、只返回排好序的噪声」的铁律
   **不适用于 reranker**。换了它只是排序质量变化，不会产生静默的语义错位。
   所以这里不做指纹校验、不写 build_meta —— 那两样是给建库产物用的。

为什么需要它（实测，CLAUDE.md G.6）：

  纯向量前 8    0/5      ← 目标 chunk 实际排在第 4/13/9/21/38 位
  召回 50+rerank 5/5      ← 本项目单条改动收益最大的一次

   根因是双编码器的固有局限：query 和 doc 各自独立编码再算余弦，
   模型没机会"看着问题读文档"。cross-encoder 把两者拼在一起过一遍，
   准得多，代价是不能预计算 —— 所以只能对召回结果做，不能对全库做。

选型（G.6 实测）：

  BAAI/bge-reranker-v2-m3        1.4–2.2s   目标多数排 #1     ← 采用
  Qwen/Qwen3-Reranker-8B         2.3–5.4s   有一例排到 #3
  netease-youdao/bce-reranker-*  —          Model disabled
"""

from __future__ import annotations

import random
import threading
import time

import httpx

from src import embed

BASE_URL = "https://api.siliconflow.cn/v1/rerank"

# ⚠️ 可换（见模块 docstring）。换之前重跑 G.6 那轮召回宽度扫描 ——
#    那套 k=50 的参数是在这个模型上标定的。
MODEL = "BAAI/bge-reranker-v2-m3"

# 自检实测：50 条文档一次请求 2.04s、HTTP 200。
# ⚠️ 不要为了"省钱"分批 —— cross-encoder 的成本随文档数线性增长，
#    分批不省算力，只多付几次跨国 RTT。
MAX_DOCS = 100

# ⚠️ **本模块只在请求路径上被调用，所以预算按「用户在等」定，不按离线批量定。**
#    阶段 05 实测撞到过一次服务端卡顿：单条查询端到端 883 秒，期间一直握着
#    Neon 连接，连接随后被 serverless 回收。正常延迟只有 0.8–2.1 秒
#    （50 条真实 chunk 实测三次：2.09 / 0.81 / 0.85 秒），
#    ⇒ 超过 20 秒就该认输降级，而不是继续等一个已经病了的端点。
#    最坏 ≈ 20×2 + 退避 1s ≈ 41 秒。
MAX_RETRIES = 2
TIMEOUT = 20.0

_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> httpx.Client:
    """惰性建池，理由同 embed.py：本模块会被 server/ import，
    而线上是 serverless —— import 时就建池等于给每次冷启动加开销。"""
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=TIMEOUT,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=8),
            )
        return _client


def close_client() -> None:
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


class RerankError(RuntimeError):
    """rerank 请求失败。"""


class QuotaExhausted(RerankError):
    """额度耗尽或鉴权失败 —— **不要重试**，重试只会空转。

    ⚠️ 与 embed 的同名异常分开定义，因为**处理方式不同**：
       embedding 挂了整条向量检索就没了（降级方向是纯 BM25，A.8）；
       rerank 挂了只是退回向量序，**检索仍然可用、只是质量下降**。
       调用方应当 catch 本异常并降级，而不是让请求整个失败。
    """


def _post(query: str, documents: list[str], top_n: int, key: str) -> list[tuple[int, float]]:
    r = _get_client().post(
        BASE_URL,
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": MODEL,
            "query": query,
            "documents": documents,
            "top_n": top_n,
            # ⚠️ 必须 False：文档正文是我们自己传进去的，让服务端再回传一遍
            #    等于把 50 条 chunk 跨国传两遍。按 index 回原列表取即可。
            "return_documents": False,
        },
    )
    if r.status_code in (401, 403):
        raise QuotaExhausted(f"鉴权失败 HTTP {r.status_code}：{r.text[:200]}")
    if r.status_code == 402:
        raise QuotaExhausted(f"额度耗尽 HTTP 402：{r.text[:200]}")
    if r.status_code != 200:
        raise RerankError(f"HTTP {r.status_code}：{r.text[:300]}")

    body = r.json()
    if "results" not in body:
        raise RerankError(f"响应缺少 results 字段：{str(body)[:200]}")

    # 自检确认的响应形状：{id, meta, results:[{index, relevance_score}]}
    # ⚠️ usage 在 meta 里，不在顶层 —— 与 /v1/embeddings 不同。
    out = [(int(it["index"]), float(it["relevance_score"])) for it in body["results"]]

    # ⚠️ 不假定服务端已排序。实测它是按分降序返回的，但这是可观察行为不是契约，
    #    而"顺序悄悄变了"恰好是本项目反复吃亏的那类静默故障。自己排一次是零成本。
    out.sort(key=lambda t: -t[1])
    return out


def _post_with_retry(query: str, documents: list[str], top_n: int, key: str):
    delay = 1.0
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return _post(query, documents, top_n, key)
        except QuotaExhausted:
            raise                       # 不重试，直接冒泡给调用方降级
        except (RerankError, httpx.HTTPError) as e:
            last = e
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(delay + random.uniform(0, delay * 0.3))
            delay *= 2
    raise RerankError(f"重试 {MAX_RETRIES} 次仍失败：{last}")


def rerank(
    query: str,
    documents: list[str],
    *,
    top_n: int = 8,
    key: str | None = None,
) -> list[tuple[int, float]]:
    """对 documents 按与 query 的相关性重排。

    返回 [(原列表下标, 相关度分)]，按分降序，最多 top_n 条。
    **返回的是下标不是文本** —— 调用方拿它回原列表取，这样 chunk_id、
    section、spoiler_level 这些随行元数据不会在中途掉队。

    ⚠️ documents 为空时返回空列表而不是报错：作用域内可能一条 chunk 都没有
       （G.4 状态③ 「认出来但没语料」），那是正常状态，应由调用方短路，
       不该在这里炸。
    """
    if not documents:
        return []
    if len(documents) > MAX_DOCS:
        raise RerankError(
            f"一次最多 {MAX_DOCS} 条，收到 {len(documents)} 条 —— "
            f"召回宽度失控了，检查调用方的 k 值（G.6 定的是 50）")

    return _post_with_retry(query, documents, min(top_n, len(documents)),
                            key or embed.api_key())


def descriptor() -> dict:
    """写进响应体的溯源信息，让"这次排序是谁做的"可追。"""
    return {"reranker": MODEL}
