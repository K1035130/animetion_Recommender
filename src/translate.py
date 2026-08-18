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

import difflib
import os
import random
import re
import threading
import time

import httpx
from dotenv import load_dotenv

BASE_URL = "https://api.siliconflow.cn/v1/chat/completions"

# ⚠️ MODEL 是**首选**模型，不再是唯一模型（2026-08-17 Kevin 定：多模型并跑）。
#    单靠 MT 的持续吞吐只有 29 字符/s，角色层 562 万字符要 54 小时 ——
#    而各模型是**独立的服务端容量**，并跑基本不互相抢，吞吐可加。
MODEL = "tencent/Hunyuan-MT-7B"

# 协作模型 —— **每一个都是实测选进来的，别凭名字加人**（2026-08-17，真实语料）：
#      Qwen/Qwen2.5-7B-Instruct   0/6 对齐，完全不守 <<<N>>> 协议
#      THUDM/GLM-4-9B-0414        6/6 对齐、HTTP 200，却**把日文原样抄回**
#                                 （假名率 0.3161）—— 只看对齐率和状态码发现不了
HELPERS = (
    "Qwen/Qwen3-8B",                          # 48/48 对齐 · 假名 0.0204 · 最快
    "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B",  # 6/6 · 假名 0.0000 · 推理模型，慢
    "THUDM/GLM-Z1-9B-0414",                   # 6/6 · 假名 0.0000 · 推理模型，慢
)

# ⚠️ **顺序即优先级**：同一条源文本若被多个模型翻过，靠前的赢。
#    必须是确定性的 —— 否则同一份缓存两次建库会得到不同语料，
#    第 5 周评测的可复现性就没了（与 embedding 锁死模型同一条理由）。
ACCEPTED = (MODEL, *HELPERS)

# 每个模型各自的并发。⚠️ **不是同一个数**：MT 实测有效并发上限约 2–4，
#    开到 8 会被服务端吊着不响应（无 429、无异常，只是超时）。
CONCURRENCY = {
    MODEL: 4,
    "Qwen/Qwen3-8B": 16,
    "deepseek-ai/DeepSeek-R1-0528-Qwen3-8B": 8,
    "THUDM/GLM-Z1-9B-0414": 8,
}

# 模型专属请求参数。⚠️ 只在支持的模型上发 —— 乱发会 400。
EXTRA_PARAMS: dict[str, dict] = {
    # 实测关掉思考对速度几乎没影响（12→13 字符/s），但省 token 不亏。
    "Qwen/Qwen3-8B": {"enable_thinking": False},
}

# ⚠️ 改这个字符串 = 译文会变 → 必须同时改 PROMPT_VERSION，
#    否则缓存会把旧译文当有效结果复用（见 translate_cache.key_of）。
PROMPT = ("把下面每一段分别翻译成中文。严格保持 <<<数字>>> 分隔符原样输出，"
          "每个分隔符后面跟对应段落的译文。不要额外解释、不要合并段落。\n\n")
PROMPT_VERSION = "v1"

SEP = re.compile(r"<<<\s*(\d+)\s*>>>")

# ⚠️ **按字符预算分批，不用固定条数。** 首版写死 25 条，在角色简介（均 130 字）
#    上工作正常，换到作品简介（均 265 字）立刻崩：25×265=6,625 字源文本，
#    译文约 4,400 token > MAX_TOKENS，**输出被截断 → 每批只回来 1 条**（实测 2/50）。
#    ⚠️ 而截断**不报错** —— 表现为"模型漏译"，很容易误判成模型质量问题。
#    按字符预算则两个语料通用：角色简介约 15 条/批，作品简介约 7 条/批。
BATCH_CHARS = 600
MAX_TOKENS = 3000

# 单批条数上限。字符预算之外再加一道 —— 防止一批塞进几百条超短文本，
# 那样 <<<N>>> 的编号本身就会占掉大量 token。
BATCH_MAX_ITEMS = 20

TIMEOUT = 420.0        # ⚠️ 为 MT 的冷启动留的，别调小
MAX_RETRIES = 4

# ── 限流（2026-08-17 Kevin 提供的条款）──────────────────────
#   RPM 1,000 · TPM 80,000
# ⚠️ **TPM 才是瓶颈，RPM 够不着**：每请求约 900 token（输入 420 + prompt 60
#    + 输出 420），80,000/900 ≈ 89 请求/分钟，而 RPM 上限是 1,000 —— 差 11 倍。
#    ⇒ 节流只需按 token 做，请求数不用管。
TOKENS_PER_MIN = 80_000
# 每个**源字符**折算的 token，含输出与 prompt 开销。日文约 0.7 tok/字，
# 输出中文约等量，再加 prompt ⇒ 1.6 是带余量的估计。
# ⚠️ 宁可高估：低估会撞 429，而 429 的退避比慢跑代价大得多。
TOKENS_PER_CHAR = 1.6

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


def _post(content: str, key: str, model: str = MODEL) -> str:
    payload = {"model": model, "temperature": 0.0, "max_tokens": MAX_TOKENS,
               "messages": [{"role": "user", "content": content}]}
    payload.update(EXTRA_PARAMS.get(model, {}))
    r = _get_client().post(
        BASE_URL, json=payload,
        headers={"Authorization": f"Bearer {key}"})
    if r.status_code == 200:
        return r.json()["choices"][0]["message"]["content"].strip()
    body = r.text[:300]
    if r.status_code == 429:
        raise TranslateError(f"429 限流：{body}")
    if 400 <= r.status_code < 500:
        raise QuotaExhausted(f"HTTP {r.status_code}（不重试）：{body}")
    raise TranslateError(f"HTTP {r.status_code}：{body}")


def _post_with_retry(content: str, key: str, model: str = MODEL) -> str:
    delay = 2.0
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return _post(content, key, model)
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


def _hiragana_ratio(t: str) -> float:
    return sum("぀" <= c <= "ゟ" for c in t) / len(t) if t else 0.0


def looks_untranslated(src: str, dst: str) -> bool:
    """译文其实还是日文？—— 拦下来当作没翻，交给别的模型重试。

    ⚠️ **这道闸不是可选的。** 实测四个模型**都会**间歇性地把原文一字不差抄回来
       （2026-08-17：5,994 条里 12 条纯回声 + 若干改写），而那时
       HTTP 200、`<<<N>>>` 对齐完美、条数也对得上 ——
       **只有看内容才发现一个字没翻**。GLM-4-9B 就是因为稳定犯这个毛病被否掉的，
       但被选中的三个只是犯得少，不是不犯。
       ⚠️ 而 to_chinese() 查到译文就用、查不到才回退原文，
          所以漏网的回声会**原样灌进库且全程不报错**。

    两条判据并联，各管一种形态：
        相似度 > 0.8    整段回声（实测 0.8~0.9 之间一条都没有，是真断层）
        平假名 > 0.20   改写成另一段日文（相似度躲得过，但平假名躲不过）

    ⚠️ **用平假名率而不是总假名率。** 专有名词绝大多数是**片假名**
       （チェンタウロ、ナカマ），合格译文的总假名率能顶到 0.40；
       而日文句子必须有平假名助词（の・を・は）。实测源文本平假名率中位 0.385，
       合格译文中位 **0.000** —— 换这个指标之后区分度才出来。

    ⚠️ **已知会误伤"整句都是日文标题"的条目**（如《朝からずっしりミルクポット》
       那类，平假名 0.227 却是合格译文）。**接受这个误伤**，因为代价不对称：
       误伤 = 换个模型再翻一次（几乎免费），漏网 = 日文静默进语料库。
    """
    if not dst:
        return True
    if _hiragana_ratio(dst) > 0.20:
        return True
    return (len(src) > 40
            and difflib.SequenceMatcher(None, src, dst).ratio() > 0.8)


def translate_batch(texts: list[str], key: str | None = None,
                    model: str = MODEL) -> dict[str, str]:
    """翻译一批，返回 {源文本: 译文}。**只包含成功对齐的那些。**

    ⚠️ 调用方必须检查返回条数 —— 少于输入就是有条目没对齐，
       应当把缺的那些单独重试，而不是当作"翻译完了"。
    """
    if not texts:
        return {}
    k = key or api_key()
    body = "\n".join(f"<<<{i + 1}>>>\n{t}" for i, t in enumerate(texts))
    got = _parse(_post_with_retry(PROMPT + body, k, model), len(texts))
    # ⚠️ 没真翻的直接丢掉 —— 复用「未对齐」那条路：不进缓存、计入 missed、
    #    下一轮由别的模型重试。**不要在这里回退成原文**，那等于把日文
    #    伪装成译文写进缓存，比不翻更糟（永远不会再被重试）。
    return {texts[i - 1]: v for i, v in got.items()
            if not looks_untranslated(texts[i - 1], v)}


def make_batches(texts: list[str]) -> list[list[str]]:
    """按字符预算切批。⚠️ 单条超预算的自成一批 —— 不能丢，也不能硬拼。"""
    out: list[list[str]] = []
    cur: list[str] = []
    n = 0
    for t in texts:
        if cur and (n + len(t) > BATCH_CHARS or len(cur) >= BATCH_MAX_ITEMS):
            out.append(cur)
            cur, n = [], 0
        cur.append(t)
        n += len(t)
    if cur:
        out.append(cur)
    return out


def warm_up(key: str | None = None, model: str = MODEL) -> float:
    """先打一发把模型唤醒，返回耗时秒数。

    ⚠️ 不是可选的优化 —— 冷启动实测 300s+，而批量循环里第一批
       撞上冷启动会把整个进度条卡住，看起来像挂了。
    """
    t = time.perf_counter()
    try:
        _post_with_retry(PROMPT + "<<<1>>>\n緑髪の少女。", key or api_key(), model)
    except TranslateError:
        pass
    return time.perf_counter() - t
