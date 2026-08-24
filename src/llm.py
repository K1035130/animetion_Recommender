"""LLM 客户端 —— HyDE 查询改写 + 剧情问答生成（流程 B / C）。

⚠️ **本模块与 src/embed.py 结构相似，但有三处刻意相反，别照抄错了**（CLAUDE.md A.8）：

  ┌────────────┬──────────────────────────┬──────────────────────────────┐
  │            │ embed.py                 │ 本模块                        │
  ├────────────┼──────────────────────────┼──────────────────────────────┤
  │ 换模型      │ ❌ **硬锁**。换 = 库里向量  │ ✅ **允许 fallback**。输出是    │
  │            │    全作废，且不报错        │    一段用完即弃的文本          │
  │ 指纹       │ 进 build_meta，读向量前校验 │ 只用于**记录**实际服务方        │
  │ 失败时     │ 停下，绝不降级             │ 顺链路往下试；全挂则由调用方     │
  │            │                          │    退回纯 BM25                │
  └────────────┴──────────────────────────┴──────────────────────────────┘

  判据（A.8）：**看这个调用的输出是不是「相对于某个语料库的坐标」。**
  embedding 是，所以锁死；LLM 输出是自然语言，用完即弃，所以可换。

⚠️ **但第 5 周评测时 LLM 也要锁死**（A.8 纪律 4）——
   fallback 只允许出现在线上演示路径。评测入口一律传 `allow_fallback=False`，
   否则同一份评测跑两次可能落在不同模型上，数字不可复现。

⚠️ **为什么在 src/ 而不是 scripts/**：线上要用（server/ 的检索端点），
   而 .vercelignore 排掉了 scripts/。与 embed.py 同一条理由。
   httpx 已在主依赖组，本模块可被 server/ 安全 import。

⬜ **模型未定（2026-08-16）。** 填 PRIMARY / FALLBACKS 两个常量即可，
   本模块的其余部分不用改。选型注意事项见 PRIMARY 上方的注释。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import threading
import time
from dataclasses import dataclass, field

import httpx
from dotenv import load_dotenv

# 429/5xx 的退避重试。⚠️ 必须有上限 —— 额度耗尽若表现为 429，
# 无上限重试会变成永久空转（与 embed.py 同一条理由）。
MAX_RETRIES = 3
TIMEOUT = 120.0          # ⚠️ 比 embedding 的 60s 长：生成几百 token 本来就慢

# 生成参数。⚠️ **temperature 默认 0** —— 第 5 周评测要可复现，
#    而 HyDE 的随机性不带来任何收益（它只是把查询改写成文档口吻）。
#    真要多样性应该在检索层做（RRF 融合多路），不是靠采样抖动。
TEMPERATURE = 0.0
MAX_TOKENS_HYDE = 400
MAX_TOKENS_ANSWER = 800
MAX_TOKENS_FIND_GATE = 5   # 只要一个 y/n token，见 find_gate()
MAX_TOKENS_INTENT = 8      # 只要一个英文单词，见 classify_intent()


# ============================================================
# 供应商配置
# ============================================================
@dataclass(frozen=True)
class Provider:
    """一个可用的 LLM 端点。

    ⚠️ 做成结构体而不是几个裸常量，是因为 **fallback 可能跨厂商** ——
       换厂商要同时换 base_url / model / api key，三者必须绑在一起。
       只留一个 MODEL 常量的话，加第二个供应商时整个函数签名都要改。
    """

    name: str                 # 人类可读的标识，进日志和 descriptor()
    base_url: str             # OpenAI 兼容的 /chat/completions 全路径
    model: str                # 厂商侧的模型 id
    key_env: str = "SILICONFLOW_API_KEY"
    # 厂商专属请求参数，随 payload 一起发。⚠️ 目前唯一的用途是给 Qwen3 系
    # 混合思考模型关思考——src/translate.py 早就踩过这个坑
    # （EXTRA_PARAMS["Qwen/Qwen3-8B"]={"enable_thinking": False}），
    # 这里复用同一个发现，不是重新猜的。空 dict 时行为与之前完全一致。
    extra: dict = field(default_factory=dict)


# ⬜ **待定（2026-08-16，Kevin 研究中）。** 填之前请求会直接报错，不会静默降级。
#
# 选型时值得对照的几点：
#   · 硅基流动是**已有的路**：SILICONFLOW_API_KEY 已在 .env、余额已充、
#     base_url 同域名，加进来零额外配置。默认首选它。
#   · 真正烧配额的是**问答生成**不是 HyDE（A.8）：HyDE 约 200 token/次，
#     而问答每次要生成几百 token 的回答，高一两个数量级。
#     ⇒ 挑模型时按「问答质量」权衡，HyDE 那点量随便哪个都够。
#   · fallback 链**按质量降序排**，不是按价格 —— 它是故障时的兜底，
#     不是省钱手段。真要省钱应该换主力，不是靠 fallback 分流。
#   · ⚠️ 中文能力是硬需求：语料全是中文，HyDE 要产出中文假想简介。
#
# 形状示例（把 name/model 换成实际选定的即可）：
#   PRIMARY = Provider(
#       name="siliconflow/<model>",
#       base_url="https://api.siliconflow.cn/v1/chat/completions",
#       model="<厂商侧模型 id>",
#   )
# ✅ 已定（2026-08-18）。选型依据见 CLAUDE.md 的 G.5b / G.5e。
#
# ⚠️ **判据是「拿到 chunk 后能不能正确作答」，不是延迟排名。** 实测三道题
#    （答案在 chunk 里 1 道、本人 chunk 未被召回 2 道），Qwen3-14B 三题全对：
#    该答的答出来、答案不在资料里的老实说没有。
PRIMARY: Provider | None = Provider(
    name="siliconflow/Qwen3-14B",
    base_url="https://api.siliconflow.cn/v1/chat/completions",
    model="Qwen/Qwen3-14B",
)

# 按质量降序。空元组 = 不降级，主力挂了就直接报错
# （调用方据此退回纯 BM25 —— 那才是 A.8 认可的降级方向）。
#
# ⚠️ **`tencent/Hunyuan-A13B-Instruct` 曾是第一顺位候选，实测后剔除。**
#    它在 G.5b 的延迟排名里是最快的（1.3s，比 Qwen3-14B 快一倍），
#    但问答实测三题里错两题，且其中一题是**最坏的错法**：
#      问「冈部伦太郎有什么特别的能力」→ 检索召回的是**菲利斯**的 chunk
#      （"自称只要注视对方眼睛就知道内心想法"）→ 它把那段能力**安到了冈部头上**
#    ⚠️ 同题 Qwen3-14B 与 GLM-4.5-Air 都回答"资料中没有提到"，**那才是对的**。
#    ⇒ 张冠李戴比拒答危险得多：用户看不出来，而且它绕过了 G.4 状态③ 的短路设计。
#    📌 **教训：fallback 必须按问答质量选，不能按延迟选** —— 它是故障时顶上来的，
#       顶上来之后答错比慢更糟。
FALLBACKS: tuple[Provider, ...] = (
    Provider(
        name="siliconflow/GLM-4.5-Air",
        base_url="https://api.siliconflow.cn/v1/chat/completions",
        model="zai-org/GLM-4.5-Air",
    ),
)

# ⚠️ **独立于 PRIMARY/FALLBACKS 链，不走 complete()**（Kevin 2026-08-24 定）。
#    这个任务（判断问句意图）现在**每一条请求都要跑**——不像 HyDE/answer 是
#    单次高价值调用，用主力 14B 模型跑分类任务是浪费。Qwen/Qwen3-8B 已经在
#    src/translate.py 的四模型协作管道里跑过 4 万+ 次，是验证过便宜可靠的选择。
#    没有配 fallback：分类任务挂了直接退回"不校验、信任原路由"（main.py 里
#    catch LLMError），比换一个模型再猜一次更省事，也更安全。
INTENT_PROVIDER = Provider(
    name="siliconflow/Qwen3-8B",
    base_url="https://api.siliconflow.cn/v1/chat/completions",
    model="Qwen/Qwen3-8B",
    # 🚨 **不关思考，实测单次分类调用 11.38 秒**——Qwen3-8B 是混合思考模型，
    #    默认会先生成一大段隐藏的思维链再吐最终答案，`max_tokens=8` 只截得住
    #    可见输出，截不住思考过程。这个坑 src/translate.py 在 2026-08-17 就
    #    踩过并记了 `EXTRA_PARAMS["Qwen/Qwen3-8B"]={"enable_thinking": False}`，
    #    这里复用同一个发现。⚠️ 这道校验**每条请求都要跑**，不关思考的话
    #    延迟会比 /ask 本身还夸张，整个设计就不成立了。
    extra={"enable_thinking": False},
)


class LLMError(RuntimeError):
    """LLM 请求失败。"""


class QuotaExhausted(LLMError):
    """额度耗尽 / 鉴权失败 / 模型名不对 —— **对当前供应商不要重试**，
    但**可以换下一个供应商**（这点与 embed.py 相反）。"""


class NotConfigured(LLMError):
    """PRIMARY 还没填。⚠️ 显式报错而不是静默返回空串 ——
    静默失败会让 HyDE 退化成「用原查询检索」，效果变差但不报错，
    正是本项目反复吃亏的那类故障。"""


def chain(allow_fallback: bool = True) -> list[Provider]:
    """本次调用可用的供应商，按优先级。"""
    if PRIMARY is None:
        raise NotConfigured(
            "src/llm.py 的 PRIMARY 还没填 —— 见该常量上方的选型注释")
    return [PRIMARY, *FALLBACKS] if allow_fallback else [PRIMARY]


def prompt_digest() -> str:
    """三条 system prompt 的联合摘要。改任何一条的任何一个字符它都会变。"""
    h = hashlib.sha256()
    h.update(HYDE_SYSTEM.encode())
    h.update(b"\x00")                    # 分隔符：防止跨条拼接出同一串字节
    h.update(ANSWER_SYSTEM.encode())
    h.update(b"\x00")
    h.update(FIND_GATE_SYSTEM.encode())
    return h.hexdigest()[:16]


def descriptor(served_by: Provider) -> dict:
    """记录**实际**服务方，写进评测日志。

    ⚠️ 与 embed.fingerprint() 的用途不同：那个是**校验**（读向量前比对，
       不符就拒绝）；这个纯粹是**记录**，因为 LLM 换了不会让已有数据失效。
       但第 5 周报告必须写清「这批数字是哪个模型跑的」，否则不可复现。

    ⚠️ **指纹必须覆盖 prompt 与 max_tokens（2026-08-19 补）。**
       此前只有 provider/model/temperature —— 改 ANSWER_SYSTEM 指纹一个
       字符都不变，第 5 周评测日志会声称两批数字同源而实际不是。
       同一条纪律 embed（指纹校验）和 translate_cache（键含 PROMPT_VERSION）
       都做对了，唯独这条漏了。max_tokens 也进指纹：它决定回答会不会被截断。
    """
    payload = json.dumps(
        {"provider": served_by.name, "model": served_by.model,
         "temperature": TEMPERATURE,
         "max_tokens": [MAX_TOKENS_HYDE, MAX_TOKENS_ANSWER, MAX_TOKENS_FIND_GATE],
         "hyde_system": HYDE_SYSTEM, "answer_system": ANSWER_SYSTEM,
         "find_gate_system": FIND_GATE_SYSTEM},
        sort_keys=True, ensure_ascii=False,
    )
    return {
        "provider": served_by.name,
        "model": served_by.model,
        "temperature": TEMPERATURE,
        # 单独暴露 prompt 摘要：排查「两批日志指纹不同」时，先看是 prompt
        # 变了还是模型变了，不用逐字段 diff
        "prompts": prompt_digest(),
        "fingerprint": hashlib.sha256(payload.encode()).hexdigest()[:16],
    }


# ============================================================
# 共享连接池
# ============================================================
# ⚠️ 与 embed.py 各持一个 Client，**不共用**。两者的超时差一倍
# （生成 120s vs 编码 60s），而 httpx 的 timeout 是 Client 级的。
# 且 fallback 可能指向别的厂商，连接池按 host 复用，混在一起没有收益。
#
# ⚠️ 惰性创建，不在模块级构造 —— 线上是 serverless，import 时建池
#    等于给每次冷启动加一笔开销。与 embed.py 同一条理由。
_client: httpx.Client | None = None
_client_lock = threading.Lock()


def _get_client() -> httpx.Client:
    global _client
    with _client_lock:
        if _client is None:
            _client = httpx.Client(
                timeout=TIMEOUT,
                limits=httpx.Limits(max_connections=16, max_keepalive_connections=16),
            )
        return _client


def close_client() -> None:
    """释放连接池。长耗时的离线阶段结束后调用；线上常驻不必调。"""
    global _client
    with _client_lock:
        if _client is not None:
            _client.close()
            _client = None


def api_key(p: Provider) -> str:
    load_dotenv()
    key = os.environ.get(p.key_env, "").strip()
    if not key:
        raise QuotaExhausted(f"缺少环境变量 {p.key_env}（供应商 {p.name}）")
    return key


def _post(p: Provider, messages: list[dict], max_tokens: int) -> str:
    """向单个供应商发一次请求。

    ⚠️ **错误分类与 embed.py 一致：4xx 不重试（429 除外），5xx 重试。**
       区别只在于「不重试」在这里意味着**换下一个供应商**，而不是彻底停下。
    """
    payload = {
        "model": p.model,
        "messages": messages,
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens,
        "stream": False,
    }
    payload.update(p.extra)      # 见 Provider.extra 的注释——目前只用来关思考
    r = _get_client().post(
        p.base_url, json=payload,
        headers={"Authorization": f"Bearer {api_key(p)}"},
    )

    if r.status_code == 200:
        body = r.json()
        try:
            text = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as e:
            raise LLMError(f"{p.name} 返回结构异常：{str(body)[:300]}") from e
        # ⚠️ 空回复要当失败处理，不能当成"模型认为无话可说" ——
        #    静默返回空串会让上游 HyDE 退化成用原查询检索。
        if not (text or "").strip():
            raise LLMError(f"{p.name} 返回空内容")
        return text.strip()

    detail = r.text[:400]
    if r.status_code == 429:
        raise LLMError(f"{p.name} 429 限流：{detail}")
    if 400 <= r.status_code < 500:
        raise QuotaExhausted(f"{p.name} HTTP {r.status_code}（不重试该供应商）：{detail}")
    raise LLMError(f"{p.name} HTTP {r.status_code}：{detail}")


def _post_with_retry(p: Provider, messages: list[dict], max_tokens: int) -> str:
    """对**单个**供应商做退避重试。跨供应商的切换在 complete() 里。"""
    delay = 1.0
    last: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return _post(p, messages, max_tokens)
        except QuotaExhausted:
            raise                       # 该供应商没救了，交给 complete() 换下一个
        except (LLMError, httpx.HTTPError) as e:
            last = e
            if attempt == MAX_RETRIES - 1:
                break
            time.sleep(delay + random.uniform(0, delay * 0.3))
            delay *= 2
    raise LLMError(f"{p.name} 重试 {MAX_RETRIES} 次仍失败：{last}")


def complete(
    messages: list[dict],
    *,
    max_tokens: int = MAX_TOKENS_ANSWER,
    allow_fallback: bool = True,
) -> tuple[str, Provider]:
    """跑一次对话补全，返回 (文本, 实际服务的供应商)。

    ⚠️ **返回 Provider 不是多余的。** 第 5 周评测必须记录数字出自哪个模型；
       线上排查「今天回答质量怎么变差了」也要靠它 —— fallback 是静默发生的，
       不记录就查不出来。

    ⚠️ `allow_fallback=False` 用于**评测入口**（A.8 纪律 4）。
    """
    errors: list[str] = []
    for p in chain(allow_fallback):
        try:
            return _post_with_retry(p, messages, max_tokens), p
        except LLMError as e:
            errors.append(str(e))
            continue
    raise LLMError(
        "所有供应商均失败，调用方应退回纯 BM25（A.8）：\n  " + "\n  ".join(errors))


# ============================================================
# 两个任务入口
# ============================================================
# ⚠️ 拆成独立函数而不是让调用方自己拼 messages，理由与 embed.py 把
#    embed_documents / embed_query 分开一样：**让用错的写法写不出来**。
#    prompt 是检索质量的一部分，散在调用点就没法统一调、也没法做 ablation。
#
# 约定：**prompt 字符串里不写 emoji**（Kevin 2026-08-20）。
#    本文件注释里那些警示符号是写给人看的，**不进 prompt**；
#    迫近发展的改动很容易顺手把它们拷进字符串。
#    排版标点（破折号 / 省略号 / 箭头 / 「」【】）不在此列，照用。

# ⬜ 措辞待第 4 周实测调整。⚠️ 调它会改变 HyDE 产出，进而改变检索结果 ——
#    第 5 周评测期间**不要动**，否则前后数字不可比。
HYDE_SYSTEM = (
    "你是动画资料库的检索助手。用户会给出一句模糊的找番需求，"
    "你要写出一段**假想的动画简介**，就像它真的存在于资料库里一样。\n"
    "要求：\n"
    "1. 只写简介正文，不要解释、不要加标题、不要用列表\n"
    "2. 150–250 字，第三人称，与百科条目的口吻一致\n"
    "3. 具体化：写出题材、设定、人物关系、基调，而不是复述用户的话\n"
    "4. 不要编造具体作品名、人名、公司名"
)

# 🚨 **改动这段就是改评测口径。** 它进 `descriptor()` 的 fingerprint，
#    测试会在改一个字符时红灯。改之前先看 CLAUDE.md 那条：
#    「baseline 之后改一次 prompt，前面所有数字作废」。
#
# 📌 **第 2、3 条是 2026-08-20 加的（Kevin 定），各自对应一次实测发现：**
#
#    第 2 条 ← 定点重测「《龙王的工作！》的片头曲是什么」：songs 章节确实被
#      检索到了，里面写着三首曲子和各自的话数（其中一首明说是片尾曲），
#      **但全库 89.7% 的 songs chunk 都没标明哪首是片头曲**
#      （含 OP 标记仅 10.3%）—— 于是模型答「资料中没有提到。」就结束了。
#      ⚠️ 这个回答**不算错**，但它把手上有的东西全扔了：用户问 OP，
#         资料至少能告诉他"本作用过这几首曲子、分别在哪些话"。
#
#    第 3 条 ← 报告 §4.5：`[44][38]` 两道角色关系题，资料里有间接线索
#      （两人都"喜欢牡丹"、酒井是前辈）但没有直接表述，**模型不做推断直接拒答**。
#      这是"角色关系类 20% 命中率"的主因 —— 语料只写个体不写关系，
#      而关系恰恰是可以从个体描述里推出来的。
#
# 🚨 **第 3 条是本文件里最危险的一条改动，四道闸门缺一不可 ——
#    每一道都是实测出回归之后补上的，不是预防性写的。**
#    ⚠️ 2026-08-20 重排后，四道闸门在 prompt 里的落点是：
#       ① ② 仍在第 3 条内；③ → 第 4 条；④ → 第 5 条；串味 → 第 6 条。
#       **重排本身不改变任何一条的含义，只把作用域从"推断时"扩到"全部回答"**
#       —— 见下面「为什么要提升为独立条目」。
#
#    ① **推断的依据只能来自资料** —— 否则退化成 §4.4 那个格子
#       「资料里没有 → 用训练记忆编一个」（22.2%，因为答对了用户看不出来）。
#    ② **推断必须标出来** —— 用户分不出哪句有出处时，既没法判断可信度，
#       剧透门控也跟着失效（门控管的是资料，管不了模型脑补出来的剧情）。
#
#    ③ **不许丢限定词** ← 实测回归：「芬里斯和弗里奥是什么关系」
#       资料写「**假扮的**新婚夫妻」，只加了①②的版本答成「芬里斯是弗里奥的
#       **妻子**」。⚠️ 这是**为了把话说圆而弱化限定词**，比编造更隐蔽。
#
#    ④ **罗列式条目不许靠位置/篇幅猜归属** ← 实测回归：「∀高达的片头曲」
#       资料只列了六首曲子和各自话数、**没标哪首是 OP**，模型挑了话数最多的
#       AURA 说"可能是片头曲"——**错的**（从话数分布看
#       ターンAターン 第2–38 + CENTURY COLOR 第39–50 首尾相接覆盖全剧，
#       才是 OP 的形态；AURA/月の繭/限りなき旅路 是 ED 序列）。
#       🚨 **这条是全库性的**：89.7% 的 songs chunk 没有 OP/ED 标注，
#          放开猜归属就等于在这一大片语料上稳定产出"听起来合理的错答"。
#       📌 **判据是「从什么推」而不是「允不允许推」**：从描述性文字推关系
#          （芬里斯、安兹那类）可靠；从罗列式条目的位置或篇幅推分类归属不可靠。
#
#    ⚠️ 还有一个已知风险是**同作用域内串味**（§4.5 把番外篇属性安到本篇上）：
#       实测「进击的巨人的原作者还做过什么作品」会把 6 部自家衍生条目列成
#       "其他作品" ⇒ 第 6 条"衍生条目不算另一部作品"那句不能删。
#
# 📌 **为什么把 ③④串味 提升为独立条目（2026-08-20 重排）**：它们讲的是
#    **怎么转述资料**，直接引用时同样适用，而原先它们是第 3 条「允许推断」
#    的子句 —— 模型完全可以读成「只有做推断时才要守」。
#    ⚠️ 而芬里斯那个回归**不必经过推断路径**：直接复述「假扮的新婚夫妻」
#    时把限定词吞掉，同样是这个错。⇒ 作用域写错的风险比多占几行大。
#
# 🔧 **新增第 5 条第二句（标注不顺延），针对下面这条三轮没消掉的残留：**
#    「龙王的工作！」那题资料只给第一首标了「片尾曲」，模型会把这个标签
#    顺延给同段里另外两首只有话数的曲子。与 ④ 同族但更弱（同一段落内的
#    属性顺延）。此前三轮迭代靠的是 ④ 那句泛化表述，没有点名"顺延"这个动作。
#    ⬜ **未实测**：这是本轮唯一新增的约束，其余都是重排。要在那题上定点复验。
ANSWER_SYSTEM = (
    "你是动画剧情问答助手。**回答必须建立在给出的资料之上**。\n"
    "要求：\n"
    "1. **不要用你自己的知识补充资料里没有的事实**。资料里查不到的，如实说明。\n"
    "2. 但**不要只说「资料中没有提到」就结束**：资料里有相关、只是不足以直接"
    "回答的内容时，先说明缺的是什么，再把这些内容总结出来。\n"
    "   例：问片头曲，而资料只列了曲目没标明哪首是片头 → 「资料没有说明哪首是"
    "片头曲，但提到本作用过这些曲子：A（第1话、第12话，用作片尾曲）、"
    "B（第2话、第4~6话）…」\n"
    "   ⚠️ 下面这个「已知剧情推进到」的写法**只在问结局、问最新进度时用**。"
    "问「讲了什么故事」这类**概述题，直接概述资料里的内容就好** —— 不要逐条"
    "罗列进度，也不要额外声明剧情推进到哪里，那会把一段概述拆成流水账。\n"
    "   例：问结局，而资料只写到剧情中途 → 「资料里没有写到结局。已知剧情推进"
    "到：…」，把资料里**时间上最靠后**的剧情节点总结出来，并明确说这是"
    "**已知进度**、不是结局。不要因为某个节点看起来像收尾就称它为结局。\n"
    "   ⚠️「已知剧情推进到」后面只能写**故事里发生的事**（谁做了什么、结果"
    "如何）。连载／播出／上映／发售日期、卷数、话数、制作阵容这些是**出版"
    "信息，不是剧情**，不能放在这个位置冒充进度。\n"
    "   但资料里**连剧情都没有**时（只有上述那些元信息），**就直接说资料里"
    "没有相关剧情内容，然后停下**：不要拿元信息充数凑成一段回答，也不要顺着"
    "作品类型、篇名或「这是最终作」之类的线索去推测走向 —— 那是编造，"
    "比直说没有更糟。\n"
    "3. **允许基于资料做合理推断**，但必须让读者分得出哪些是资料写的、哪些是你"
    "推的：推断要用「从资料看…」「资料没有直说，但…」这类说法标出来，并点明"
    "依据的是资料里的哪一条。推断的**依据只能来自资料**，且必须指向资料里具体"
    "的某一句；资料里找不到线索时就说找不到，不要改用你自己的知识去推，也不要"
    "泛泛猜测作品的主题或剧情走向。\n"
    "\n"
    "下面 4–6 条讲的是**怎么转述资料**，直接引用和推断时都要遵守：\n"
    "4. **不要丢掉或弱化资料里的限定词**。资料写「假扮的新婚夫妻」就不能"
    "说成「妻子」，写「疑似」「可能」「曾经」也要保留。\n"
    "5. 资料只是**罗列**条目、没有说明各条的归属或分类时（例如列了曲目却"
    "没标哪首是片头曲、列了角色却没说各自阵营），**不要靠出现顺序、篇幅长短或"
    "话数多少去猜归属** —— 如实说资料没有标明，把条目原样列出来就好。\n"
    "   同理，**某一条上的标注只对那一条生效**：同一段里另外几条没有标注时，"
    "不要把邻近条目的标注（如「片尾曲」）顺延给它们。\n"
    "6. **不要把资料里属于其他作品、衍生作品或其他角色的信息，说成是"
    "问题所问对象的**；同一部作品的衍生条目（OAD／剧场版／总集篇／外传）"
    "不算「另一部作品」。\n"
    "\n"
    "7. 回答简洁：先说结论，再给出资料依据；可以补充解释或背景，但不要为了"
    "展开而复述与问题无关的内容。\n"
    "8. 资料条目前的【】里是角色名或章节名，可用于指代"
)

# 流程 B 找番的门控（2026-08-24 加，Kevin 提出）——用一次 LLM 判断代替
# 手写规则/阈值。⚠️ **只判"这句话像不像一段找番描述"，不判是否合规/有害**，
# 后者不是本项目的问题域。
FIND_GATE_SYSTEM = (
    "你是动画找番助手的前置判断器。用户会输入一句话，"
    "你只需要判断：这句话**是不是**在描述一类动画作品的内容特征"
    "（题材/设定/人物关系/基调/剧情梗概等），值得拿去做语义检索。\n"
    "算「是」的例子：主角很强但很低调的番、在异世界开餐厅的故事、"
    "讲乐队和音乐的动画、扑朔迷离的悬疑推理番。\n"
    "算「否」的例子：具体的人名/作品名（那应该走别的通道，不是描述）、"
    "与动画无关的问题（天气/代码/闲聊）、无意义的乱码、过于空泛以至于"
    "无法检索的话（如「有什么好看的」「随便推荐一个」）。\n"
    "只回答一个字：「是」或「否」，不要解释、不要标点、不要别的内容。"
)


def find_gate(query: str, *, allow_fallback: bool = True) -> tuple[bool, Provider]:
    """判断一句话是不是「值得拿去找番」的内容描述。

    ⚠️ **用 LLM 判断而不是手写规则/余弦地板**（Kevin 2026-08-24 定）。
       起因：`find()` 的语义检索对任何输入都会返回一个 top-k（cos 排序
       不存在"零结果"），而实测离题查询的余弦分数与在题查询**有重叠**
       （「今天天气怎么样」top1=0.540，高于不少真实找番查询）——手调一个
       绝对地板是 B.4 那次"rerank 分噪声 ~1e-3，绝对地板调不稳"的同一个坑，
       样本太少也标定不出可靠阈值。⇒ 交给 LLM 做这类语义边界判断。

    ⚠️ **解析要宽容但不能默认为真**：只要回答的第一个非空字符是「是」
       就判 True，其余（含"否"、多余解释、空回答）一律 False —— **失败要
       安全**：判不清就当作"不够具体"，比自信地展示一堆离题结果更好，
       与「自信地答错很贵，多问一次很便宜」同一条纪律。
    """
    text, served = complete(
        [{"role": "system", "content": FIND_GATE_SYSTEM},
         {"role": "user", "content": query}],
        max_tokens=MAX_TOKENS_FIND_GATE,
        allow_fallback=allow_fallback,
    )
    return text.strip().startswith("是"), served


# 意图校验（2026-08-24 加，Kevin 提出）——起因是实测发现除 voice 外的按钮
# 分支（season/find）选错时会"自信地答非所问"：season 按钮问剧情问题会
# 无视问题内容直接展示当季新番列表；find 按钮问"三笠的声优是谁"会硬凑一堆
# 题材相近的番。voice 表现正确纯粹是因为它自己有"找不到人名就回落"的内部
# 检查，season/find 没有等价机制。⇒ 补一道**每条请求都跑**的判断，覆盖
# 自动分派与按钮强制两种情况（Kevin 定："auto + 所有按钮都校验"）。
INTENT_VALUES = ("ask", "voice", "season", "find", "off_topic")

INTENT_SYSTEM = (
    "你是动画问答系统的意图判断器。用户会输入一句话，"
    "你需要判断它最符合下面哪一类，只回答对应的英文单词，不要解释、"
    "不要加标点、不要输出除了那个单词以外的任何内容：\n"
    "ask         问某部具体作品或角色的剧情/设定/结局/关系等内容\n"
    "voice       问某位声优配过哪些角色\n"
    "season      问某个季度/年份有哪些新番，或按档期浏览\n"
    "find        没有说出具体作品名，而是用一段描述（题材/设定/基调）找番\n"
    "off_topic   与动画完全无关，或者内容无意义、过于空泛以至于无法处理\n"
    "只回答 ask / voice / season / find / off_topic 中的一个单词。"
)


def classify_intent(query: str) -> tuple[str | None, Provider]:
    """判断问句的意图属于 ask/voice/season/find/off_topic 中的哪一类。

    ⚠️ **不走 complete()/PRIMARY-FALLBACKS 链**，直接打 `INTENT_PROVIDER`
       （Qwen/Qwen3-8B）——这是分类任务，成本要压到最低，且**每条请求都要跑**，
       用主力问答模型划不来。没有配 fallback：这道关本来就是"锦上添花"的
       校验层，挂了直接由调用方 catch LLMError 退回"不校验"，不必为它
       再多打一次别的模型。

    ⚠️ **解析失败要返回 None，不能猜一个默认值**：调用方据此判断"这道校验
       到底跑没跑成"，与 find_gate() 的"判不清就当否"不是同一回事——那边
       两个结果（是/否）都是合法业务状态，这边"解析失败"必须能和"判断出
       off_topic"区分开，否则调用方没法决定要不要信任原路由。
    """
    text = _post_with_retry(
        INTENT_PROVIDER,
        [{"role": "system", "content": INTENT_SYSTEM},
         {"role": "user", "content": query}],
        MAX_TOKENS_INTENT,
    )
    low = text.strip().lower()
    for v in INTENT_VALUES:
        if low.startswith(v):
            return v, INTENT_PROVIDER
    return None, INTENT_PROVIDER


def hyde(query: str, *, allow_fallback: bool = True) -> tuple[str, Provider]:
    """把用户查询改写成一段假想的动画简介（流程 B 的第一步）。

    ⚠️ **本函数只产出文本，不负责编码** —— 怎么把它变成向量是检索层的事。
       这不是洁癖：**「假想文档该走 embed_documents 还是 embed_query」
       目前没有定论**，而 Qwen3 是非对称编码（两者 cos 仅 0.797，A.7 实测）。
       ⬜ 这是阶段 05 要实测的一个开关，把决定权留在检索层才能做 A/B。
    """
    return complete(
        [{"role": "system", "content": HYDE_SYSTEM},
         {"role": "user", "content": query}],
        max_tokens=MAX_TOKENS_HYDE,
        allow_fallback=allow_fallback,
    )


def answer(
    question: str,
    chunks: list[tuple[str | None, str]],
    *,
    allow_fallback: bool = True,
    history: list[tuple[str, str]] | None = None,
) -> tuple[str, Provider]:
    """基于检索到的 chunk 生成回答（流程 C 的最后一步）。

    chunks: [(section, text)] —— section 是角色名或章节名，可能为 None。
            ⚠️ 阶段 03 之后 69,996 条角色 chunk 的 section 已填成角色名，
               所以「艾伦的母亲」这种无主语的简介在 prompt 里能归属到人（F.7b ①②）。

    ⚠️ **chunks 为空时不要调用本函数。** G.4 状态③ 明确要求短路 ——
       检索为空还把问题丢给 LLM，它会用训练记忆流畅答出来，
       绕过整条 RAG 链路：没有出处、没有剧透门控、可能是幻觉。
       这里加断言把它变成硬错误，而不是靠调用方自觉。
    """
    if not chunks:
        raise ValueError(
            "chunks 为空 —— 应由调用方短路返回「没有资料」，不要调 LLM（G.4 状态③）")

    body = "\n\n".join(
        f"【{sec}】{text}" if sec else text for sec, text in chunks)

    # ⚠️ **历史以对话消息的形式给，不拼进「资料」里。** 拼进去的话
    #    ANSWER_SYSTEM 第 1 条（只能依据给出的资料回答）就被架空了 ——
    #    上一轮的回答会变成「资料」，而它本身可能是模型说的话，
    #    于是错误会在多轮里自我强化。分成 messages 则边界清楚：
    #    资料是资料，对话是对话。
    # ⚠️ 截断由调用方（retrieve）负责，这里只负责组装。
    msgs: list[dict] = [{"role": "system", "content": ANSWER_SYSTEM}]
    for q, a in (history or []):
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": a})
    msgs.append({"role": "user", "content": f"资料：\n{body}\n\n问题：{question}"})

    return complete(
        msgs,
        max_tokens=MAX_TOKENS_ANSWER,
        allow_fallback=allow_fallback,
    )
