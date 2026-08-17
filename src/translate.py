"""日译中客户端 —— Hunyuan-MT-7B（2026-08-17 Kevin 选定，当前限免）。

⚠️ **与 src/llm.py 分开的三个理由**：
   ① 模型不同：llm.py 是 HyDE/问答用的通用模型，这里是翻译专用模型
   ② 调用形态不同：这里是**批量分隔符协议**，llm.py 是单轮对话
   ③ 生命周期不同：翻译是一次性离线任务，llm.py 在请求路径上
   （共用的只有「OpenAI 兼容 /chat/completions」这个接口形状。）

⚠️ **MT 的冷启动是已知特性，不是故障**（2026-08-17 实测）：
       首次调用 300s+ 超时 · 热起来后每批 3–40s · 闲置一会儿又凉
   ⇒ TIMEOUT 给到 420s，且首次失败必须重试而不是放弃。

⚠️ **批量协议用 <<<N>>> 分隔符，不用「行首编号」。**
   角色简介普遍自带换行（`CV：xxx` 单独一行），行首编号会被内部换行撑破 ——
   首版就是这么误判成"模型漏译"的，实际是解析器只捞到每条第一行。
"""

from __future__ import annotations

import os
import random
import re
import threading
import time

import httpx
from dotenv import load_dotenv

BASE_URL = "https://api.siliconflow.cn/v1/chat/completions"
MODEL = "tencent/Hunyuan-MT-7B"

# ⚠️ 改这个字符串 = 译文会变 → 必须同时改 PROMPT_VERSION，
#    否则缓存会把旧译文当有效结果复用（见 translate_cache.key_of）。
PROMPT = ("把下面每一段分别翻译成中文。严格保持 <<<数字>>> 分隔符原样输出，"
          "每个分隔符后面跟对应段落的译文。不要额外解释、不要合并段落。\n\n")
PROMPT_VERSION = "v1"

SEP = re.compile(r"<<<\s*(\d+)\s*>>>")

# 单批条数。⚠️ 越大越省 prompt 开销，但**一批失败就整批重来**，
#    且超过 max_tokens 会被截断。25 是实测能稳定对齐的规模。
BATCH = 25
MAX_TOKENS = 3000

TIMEOUT = 420.0        # ⚠️ 为 MT 的冷启动留的，别调小
MAX_RETRIES = 4

_client: httpx.Client | None = None
_lock = threading.Lock()


class TranslateError(RuntimeError):
    """翻译请求失败。"""


class QuotaExhausted(TranslateError):
    """额度耗尽 / 鉴权失败 —— 不要重试。"""


def _get_client() -> httpx.Client:
    global _client
    with _lock:
        if _client is None:
            _client = httpx.Client(
                timeout=TIMEOUT,
                limits=httpx.Limits(max_connections=8, max_keepalive_connections=8))
        return _client


def close_client() -> None:
    global _client
    with _lock:
        if _client is not None:
            _client.close()
            _client = None


def api_key() -> str:
    load_dotenv()
    k = os.environ.get("SILICONFLOW_API_KEY", "").strip()
    if not k:
        raise QuotaExhausted("缺少环境变量 SILICONFLOW_API_KEY（见 .env.example）")
    return k


def _post(content: str, key: str) -> str:
    r = _get_client().post(
        BASE_URL,
        json={"model": MODEL, "temperature": 0.0, "max_tokens": MAX_TOKENS,
              "messages": [{"role": "user", "content": content}]},
        headers={"Authorization": f"Bearer {key}"})
    if r.status_code == 200:
        return r.json()["choices"][0]["message"]["content"].strip()
    body = r.text[:300]
    if r.status_code == 429:
        raise TranslateError(f"429 限流：{body}")
    if 400 <= r.status_code < 500:
        raise QuotaExhausted(f"HTTP {r.status_code}（不重试）：{body}")
    raise TranslateError(f"HTTP {r.status_code}：{body}")


def _post_with_retry(content: str, key: str) -> str:
    delay = 2.0
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return _post(content, key)
        except QuotaExhausted:
            raise
        except (TranslateError, httpx.HTTPError) as e:
            last = e
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(delay + random.uniform(0, delay * 0.3))
            delay *= 2
    raise TranslateError(f"重试 {MAX_RETRIES} 次仍失败：{last}")


def _parse(out: str, n: int) -> dict[int, str]:
    """按 <<<N>>> 切开。⚠️ 缺项就是缺项 —— 不猜、不补位。

    补位会让「第 7 条的译文安到第 8 条头上」，而那**不报错**，
    只是语料从此张冠李戴 —— 比整批失败危险得多。
    """
    parts, res = SEP.split(out), {}
    for i in range(1, len(parts) - 1, 2):
        idx = int(parts[i])
        if 1 <= idx <= n:
            body = parts[i + 1].strip()
            if body:
                res[idx] = body
    return res


def translate_batch(texts: list[str], key: str | None = None) -> dict[str, str]:
    """翻译一批，返回 {源文本: 译文}。**只包含成功对齐的那些。**

    ⚠️ 调用方必须检查返回条数 —— 少于输入就是有条目没对齐，
       应当把缺的那些单独重试，而不是当作"翻译完了"。
    """
    if not texts:
        return {}
    k = key or api_key()
    body = "\n".join(f"<<<{i + 1}>>>\n{t}" for i, t in enumerate(texts))
    got = _parse(_post_with_retry(PROMPT + body, k), len(texts))
    return {texts[i - 1]: v for i, v in got.items()}


def warm_up(key: str | None = None) -> float:
    """先打一发把模型唤醒，返回耗时秒数。

    ⚠️ 不是可选的优化 —— 冷启动实测 300s+，而批量循环里第一批
       撞上冷启动会把整个进度条卡住，看起来像挂了。
    """
    t = time.perf_counter()
    try:
        _post_with_retry(PROMPT + "<<<1>>>\n緑髪の少女。", key or api_key())
    except TranslateError:
        pass
    return time.perf_counter() - t
