"""Qwen3-Embedding 客户端 —— 向量的唯一定义处。

⚠️ **为什么在 src/ 而不是 scripts/**：两个调用方。第 3 周的离线建库
   （scripts/build_embeddings.py）和第 4 周的线上查询编码（server/）。
   而 .vercelignore 排掉了 scripts/，线上要用的东西必须在 src/。
   放一份两边用 —— 与 tag 向量只在 src/tagvec.py 定义一次是同一条纪律。

⚠️ **httpx 目前只在 pyproject 的 etl 组里，主依赖组没有 HTTP 客户端。**
   第 3 周纯离线不受影响；但第 4 周 server/ 一旦 import 本模块，
   线上就会 ModuleNotFoundError —— 与首次部署缺 fastapi 是同一类故障。
   ⬜ 那时必须把 httpx 挪进主依赖组。

实测行为（2026-08-14，scripts/probe_embedding_api.py，详见 CLAUDE.md A.7）：

  · 返回已 L2 归一化（范数 1.000000）→ pgvector 可用 `<#>` 内积算子
  · **不是逐请求确定的**：服务端连续批处理，同一文本两次请求逐位差 0~2e-3。
    控制自己的 batch size 也规避不了 —— 你的请求会和其他用户在飞的请求拼一起。
    信噪比 3341×，对检索质量无影响；但**可复现性只能靠缓存层**（src/embed_cache.py）。
  · instruct 前缀确实改变向量（cos 0.797）→ query/doc 非对称编码可控
  · 客户端截断 vs 服务端 `dimensions` 参数 cos = 1.000000 → 客户端截断是纯 MRL
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time

import httpx
import numpy as np
from dotenv import load_dotenv

BASE_URL = "https://api.siliconflow.cn/v1/embeddings"

# ============================================================
# 锁死的参数
# ============================================================
# ⚠️ **MODEL 是硬锁。** 换模型 = 换向量空间的基 = 库里 11,453 条向量全部作废，
#    而且**不报错** —— 余弦照算、结果照排，只是全是噪声（CLAUDE.md A.8）。
#    改这里必须同时重跑 scripts/build_embeddings.py，靠 build_meta 表的指纹校验兜底。
MODEL = "Qwen/Qwen3-Embedding-0.6B"

# 向 API 请求的维度。⚠️ 恒为 1024，**不用服务端的 `dimensions` 参数降维**：
# 缓存要存完整维度才能随时改主意（CLAUDE.md A.9），
# 而客户端截断实测与服务端截断 cos=1.000000，没有任何损失。
DIM = 1024

# 喂给模型的文本字段。⚠️ 只有 summary —— 不拼 name、不拼 tags（CLAUDE.md A.10）。
# 拼 tags 会让「embedding 模型」这条 baseline 秘密变成 tag+embedding 混合体，
# 第 5 周的 ablation 就失去意义了。
SOURCE_FIELD = "summary"

# ⚠️ **QUERY_INSTRUCT 是软锁，与 MODEL 不同。**
#    Qwen3 是非对称编码：query 加指令前缀，document 不加。
#    库里存的全是 document（无前缀），所以改这里**不会污染已有向量** ——
#    只改变查询侧行为，影响的是检索质量而非正确性。
#    因此它**不进指纹**（见 fingerprint()），否则改个措辞就触发假警报。
#
# ⬜ 第 4 周做检索时按实际效果调措辞。现在没有检索场景，调不出好的。
# ⬜ 将来加「按描述找相似角色」时**新增一条**常量，不要改这条 ——
#    两种检索意图不同，共用一个前缀会互相拖累。等角色表建好再说。
QUERY_INSTRUCT = "Instruct: 根据描述检索相似的作品\nQuery: "

# 单次请求的文本条数。TPM=1,000,000 是瓶颈，RPM=2,000 不是（A.7），
# 所以真正的节流在调用方按 token 做，这里只限制单请求不要太大。
MAX_BATCH = 32

# 429/5xx 的退避重试。⚠️ 必须有上限 —— 若额度耗尽表现为 429，
# 无上限重试会变成永久空转（A.8：额度耗尽是停止条件，不是重试条件）。
MAX_RETRIES = 5
TIMEOUT = 60.0


class EmbedError(RuntimeError):
    """embedding 请求失败。"""


class QuotaExhausted(EmbedError):
    """额度耗尽或鉴权失败 —— **不要重试**，重试只会空转。"""


def fingerprint() -> str:
    """当前配置的指纹，写进 build_meta 表并在读向量前比对。

    ⚠️ **只包含会让已存向量失效的参数。**
       QUERY_INSTRUCT 不在内：文档侧不加前缀，改它不影响库里的向量。
       把它算进来会导致「改个查询措辞 → 指纹不符 → 以为要重灌全库」。
    """
    payload = json.dumps(
        {"model": MODEL, "dim": DIM, "source": SOURCE_FIELD, "normalized": True},
        sort_keys=True,
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def descriptor(**extra: object) -> dict:
    """写进 build_meta.value 的完整描述符。

    存完整字段而非只存哈希：指纹只能告诉你「不一致」，
    排查时真正要的是「**哪里**不一致」。
    """
    return {
        "model": MODEL,
        "dim": DIM,
        "source": SOURCE_FIELD,
        "normalized": True,
        "fingerprint": fingerprint(),
        **extra,
    }


def api_key() -> str:
    load_dotenv()
    key = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not key:
        raise QuotaExhausted("缺少环境变量 SILICONFLOW_API_KEY（见 .env.example）")
    return key


def _post(texts: list[str], key: str) -> np.ndarray:
    """发一次请求。错误分类见下。

    ⚠️ **分类原则：4xx 不重试（429 除外），5xx 重试。**
       硅基流动「额度耗尽」具体返回哪个码未实测确认，但只要它是 4xx，
       这条默认规则就会正确地停下来而不是空转。
    """
    r = httpx.post(
        BASE_URL,
        json={"model": MODEL, "input": texts, "encoding_format": "float"},
        headers={"Authorization": f"Bearer {key}"},
        timeout=TIMEOUT,
    )

    if r.status_code == 200:
        data = r.json()["data"]
        # ⚠️ 不能假设返回顺序与输入一致
        data.sort(key=lambda d: d["index"])
        return np.array([d["embedding"] for d in data], dtype=np.float32)

    body = r.text[:400]
    if r.status_code == 429:
        raise EmbedError(f"429 限流：{body}")
    if 400 <= r.status_code < 500:
        # 401 key 不对 / 402 余额不足 / 400 模型名不对 —— 全都不该重试
        raise QuotaExhausted(f"HTTP {r.status_code}（不重试）：{body}")
    raise EmbedError(f"HTTP {r.status_code}：{body}")


def _post_with_retry(texts: list[str], key: str) -> np.ndarray:
    delay = 1.0
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return _post(texts, key)
        except QuotaExhausted:
            raise                       # 不重试，直接冒泡
        except (EmbedError, httpx.HTTPError) as e:
            last = e
            if attempt == MAX_RETRIES - 1:
                break
            # 指数退避 + jitter。jitter 是为了避免多进程重跑时同步撞墙
            time.sleep(delay + random.uniform(0, delay * 0.3))
            delay *= 2
    raise EmbedError(f"重试 {MAX_RETRIES} 次仍失败：{last}")


def embed_documents(texts: list[str], key: str | None = None) -> np.ndarray:
    """编码文档（建库侧）。**不加 instruct 前缀。**

    返回 (n, DIM) 的 float32。⚠️ 返回原值不降精度 —— 缓存要存 fp32（A.9），
    转 fp16 是写库那一步的事。
    """
    if not texts:
        return np.empty((0, DIM), dtype=np.float32)
    if len(texts) > MAX_BATCH:
        raise ValueError(f"单批最多 {MAX_BATCH} 条，收到 {len(texts)}；请在调用方分批")
    return _post_with_retry(texts, key or api_key())


def embed_query(text: str, key: str | None = None) -> np.ndarray:
    """编码查询（检索侧）。**加 instruct 前缀。**

    ⚠️ 与 embed_documents 拆成两个函数，是为了让「把文档当查询编码」
       这种错误写不出来。实测两者 cos=0.797，混用是静默的质量下降。
    """
    return _post_with_retry([QUERY_INSTRUCT + text], key or api_key())[0]


def truncate(vecs: np.ndarray, dim: int) -> np.ndarray:
    """Matryoshka 截断：取前 dim 维再重新 L2 归一化。

    ⚠️ **重新归一化不能省。** 截断后范数不再是 1，而 pgvector 的 `<#>`
       内积算子假定输入已归一化 —— 漏掉这一步，内积算出来的不是余弦。

    实测与服务端 `dimensions` 参数的结果 cos=1.000000，两者等价；
    用客户端截断是因为缓存里存的是完整 1024 维，随时可以改主意。
    """
    if dim > vecs.shape[-1]:
        raise ValueError(f"截断维度 {dim} 大于原始维度 {vecs.shape[-1]}")
    out = vecs[..., :dim].astype(np.float32)
    norms = np.linalg.norm(out, axis=-1, keepdims=True)
    return out / np.maximum(norms, 1e-12)
