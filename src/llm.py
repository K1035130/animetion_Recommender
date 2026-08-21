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
from dataclasses import dataclass

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
    """两条 system prompt 的联合摘要。改任何一条的任何一个字符它都会变。"""
    h = hashlib.sha256()
    h.update(HYDE_SYSTEM.encode())
    h.update(b"\x00")                    # 分隔符：防止跨条拼接出同一串字节
    h.update(ANSWER_SYSTEM.encode())
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
         "max_tokens": [MAX_TOKENS_HYDE, MAX_TOKENS_ANSWER],
         "hyde_system": HYDE_SYSTEM, "answer_system": ANSWER_SYSTEM},
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
    r = _get_client().post(
        p.base_url,
        json={
            "model": p.model,
            "messages": messages,
            "temperature": TEMPERATURE,
            "max_tokens": max_tokens,
            "stream": False,
        },
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
#       "其他作品" ⇒ 第 3 条里"衍生条目不算另一部作品"那句不能删。
#
# ⬜ **已知残留（未修，影响小）**：「龙王的工作！」那题资料只给第一首标了
#    「片尾曲」，模型会把这个标签顺延给同段里另外两首只有话数的曲子。
#    与 ④ 同族但更弱（同一段落内的属性顺延），三轮 prompt 迭代都没消掉。
ANSWER_SYSTEM = (
    "你是动画剧情问答助手。**回答必须建立在给出的资料之上**。\n"
    "要求：\n"
    "1. **不要用你自己的知识补充资料里没有的事实**。资料里查不到的，如实说明。\n"
    "2. 但**不要只说「资料中没有提到」就结束**：资料里有相关、只是不足以直接"
    "回答的内容时，先说明缺的是什么，再把这些内容总结出来。\n"
    "   例：问片头曲，而资料只列了曲目没标明哪首是片头 → 「资料没有说明哪首是"
    "片头曲，但提到本作用过这些曲子：A（第1话、第12话，用作片尾曲）、"
    "B（第2话、第4~6话）…」\n"
    "3. **允许基于资料做合理推断**，但必须让读者分得出哪些是资料写的、哪些是你"
    "推的：推断要用「从资料看…」「资料没有直说，但…」这类说法标出来，并点明"
    "依据的是资料里的哪一条。\n"
    "   ⚠️ 推断的**依据只能来自资料**，且必须指向资料里具体的某一句。"
    "资料里找不到线索时就说找不到，不要改用你自己的知识去推，"
    "也不要泛泛猜测作品的主题或剧情走向。\n"
    "   ⚠️ **不要丢掉或弱化资料里的限定词**。资料写「假扮的新婚夫妻」就不能"
    "说成「妻子」，写「疑似」「可能」「曾经」也要保留。\n"
    "   ⚠️ 资料只是**罗列**条目、没有说明各条的归属或分类时（例如列了曲目却"
    "没标哪首是片头曲、列了角色却没说各自阵营），**不要靠出现顺序、篇幅长短或"
    "话数多少去猜归属** —— 这类如实说资料没有标明，把条目原样列出来就好。\n"
    "   ⚠️ 不要把资料里属于其他作品、衍生作品或其他角色的信息，说成是"
    "问题所问对象的；**同一部作品的衍生条目（OAD／剧场版／总集篇／外传）"
    "不算「另一部作品」**。\n"
    "4. 回答简洁，直接说结论，但可以添加一些解释或背景信息\n"
    "5. 资料条目前的【】里是角色名或章节名，可用于指代"
)


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
