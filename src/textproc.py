"""文本归一化与中文分词 —— 建库与查询的唯一实现。

⚠️ 这个模块存在的唯一理由是**纪律**：
    BM25 的建库端和查询端必须用同一个分词器 + 同一套自定义词典。
    版本或词典漂移会让查询词切出来和库里对不上，召回直接崩，
    而且是静默的 —— 不报错，只是搜不到东西。
    所以 ETL 和将来的 FastAPI 查询层都必须 import 这里，不要各写一份。

Neon 装不了 zhparser/pgroonga（`pg_available_extensions` 只有 vector 和 pg_trgm），
Postgres 内置分词器又全部按空格切词，中文整句会变成一个 token。
因此中文必须在 Python 侧切好、空格连接，再交给 `to_tsvector('simple', ...)`。
"""

import hashlib
import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

import jieba

VOCAB_PATH = Path(__file__).resolve().parent.parent / "data" / "interim" / "tag_vocab.json"

# 切词逻辑的版本号。改动 tokenize() 的行为时必须 +1 ——
# 它进 dict_fingerprint()，这样查询端能发现「库是用旧规则建的」。
TOKENIZER_VERSION = 4


@lru_cache(maxsize=1)
def _vocab() -> dict:
    with open(VOCAB_PATH, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def keep_tags() -> frozenset[str]:
    """418 个题材 tag（df>=8 且 classify() 判为 KEEP）。"""
    return frozenset(_vocab()["keep"])


@lru_cache(maxsize=1)
def _tokenizer() -> jieba.Tokenizer:
    """独立 Tokenizer 实例，不用 jieba 的全局单例。

    自定义词典直接复用清洗后的题材 tag 词表：「轻小说」「异世界」「泡面番」
    「萝卜」这类动画圈专有名词通用词典会切错，而它们全在词表里，零额外成本。
    """
    tk = jieba.Tokenizer()
    tk.initialize()
    for word in keep_tags():
        # freq 交给 jieba 按词长自动估算，保证该词不会被切开
        tk.add_word(word)
    return tk


def dict_fingerprint() -> str:
    """词典指纹：jieba 版本 + 词表内容的 sha256 前 16 位。

    建库时记下来，查询端启动时比对。不一致说明词典漂移过，
    此时召回质量已经不可信 —— 宁可启动失败也别静默降级。
    """
    payload = (
        f"{jieba.__version__}|v{TOKENIZER_VERSION}|" + "\0".join(sorted(keep_tags()))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


# 归一化时要丢掉的 unicode 大类：标点 P*、符号 S*、分隔 Z*、控制 C*
_DROP_CATEGORIES = ("P", "S", "Z", "C")


def norm_name(s: str) -> str:
    """别名归一化，用于 alias 表的精确匹配。

    NFKC 先把全角/半角、罗马数字、带圈字符等变体折叠到一起
    （「ＥＶＡ」→「EVA」、「Ⅱ」→「II」），再统一小写并去掉所有
    标点与空白。这样「Fate/stay night」「fate stay night」
    「ＦＡＴＥ／ＳＴＡＹ　ＮＩＧＨＴ」会落到同一个键上。
    """
    s = unicodedata.normalize("NFKC", s)
    s = s.casefold()
    return "".join(ch for ch in s if unicodedata.category(ch)[0] not in _DROP_CATEGORIES)


# 按书写系统切段。⚠️ 不能直接把整段文本喂给 jieba：
# jieba 的默认切词正则是 `[一-鿕a-zA-Z0-9+#&._%-]+`，**不含假名区段**，
# 于是日文原名会被逐字打散 —— 实测「新世紀エヴァンゲリオン」切成
# 「新世紀 / エ / ヴ / ァ / ン / ゲ / リ / オ / ン」。单字假名在 BM25 里
# 等于噪声（几乎匹配所有日文条目），而日文原名正是 alias 表的主力来源。
# 所以先按书写系统分段，只把汉字段交给 jieba，假名段整段保留。
# 片假名与平假名要**分开**切段：日文标题几乎都是「片假名实词 + 平假名助词」，
# 粘在一起会切出「ジョジョの」「ハルヒの」这种带尾巴的 token，
# 而「ジョジョ」「ハルヒ」才是用户真正会输入的检索词。
# ⚠️ 一律用 \uXXXX 转义写区间，不要直接贴字面汉字/假名 ——
#    字面量在编辑器与终端间转手时会被改坏，且看不出来。
_HAN = "々一-鿿㐀-䶿"   # 汉字 + 叠字符 々（美鳥の日々）
_KATA = "ァ-ヺー-ヿ"       # 片假名，含长音符 ー，跳过中点 ・(U+30FB)
_HIRA = "ぁ-ゟ"
_HANGUL = "가-힯"
# CJK 相关区段整体，供下面的兜底分支排除
_CJK = "　-ヿ㐀-䶿一-鿿가-힯"

_RUNS = re.compile(
    f"[{_HAN}]+"
    f"|[{_KATA}]+"
    f"|[{_HIRA}]+"
    f"|[{_HANGUL}]+"
    # 兜底：其余一切字母与数字整段保留 —— 拉丁（含带变音符号的罗马音
    # Shōjo / Uchū）、西里尔、希腊、希伯来、阿拉伯……
    # 早先写死 [0-9a-z] 会把它们切碎甚至整条丢空：实测 347 行别名受影响，
    # 其中 25 行切完是空的（俄语/希伯来语标题），等于在 BM25 里搜不到。
    # [^\W_] 是「unicode 字母或数字」的惯用写法，再排除上面已单独处理的区段。
    f"|[^\\W_{_CJK}]+"
)

_HAS_HAN = re.compile(f"[{_HAN}]")


def tokenize(text: str) -> str:
    """预分词：切好词后空格连接，供 to_tsvector('simple', ...) 使用。

    返回空格连接的字符串而不是 list —— 调用方直接塞进 SQL 参数即可。

    ⚠️ 假名段整段作为一个 token，不做日语形态分析（那要 MeCab/fugashi，
    为几千条日文标题引入一个新依赖不划算）。对标题够用：
    「けいおん！」→「けいおん」、「エヴァンゲリオン」→ 整词。
    但这意味着日文**长句**会糊成一个巨型 token —— 目前只有标题和别名
    走日文，简介是中文，不受影响。将来若要灌日文正文，这里得换方案。
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text).casefold()
    out: list[str] = []
    for run in _RUNS.findall(text):
        if not _HAS_HAN.search(run):
            out.append(run)          # 假名/谚文/拉丁/西里尔…：整段保留
            continue
        for tok in _tokenizer().cut(run):
            tok = tok.strip()
            if tok:
                out.append(tok)
    return " ".join(out)
