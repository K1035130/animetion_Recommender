"""把抓下来的萌娘百科 HTML 解析成 chunk —— 第 4 周批次 2 的第二阶段。

    fetch_moegirl.py  →  data/raw/moegirl/*.html.gz   （只抓不解析）
    parse_moegirl.py  →  data/interim/moegirl_chunks.jsonl   ← 本脚本
    （之后才是 sql/007 建 plot_chunk 表 + 编码入库）

⚠️ **本脚本不写数据库。** plot_chunk 表还不存在，而它的切分粒度要由这里的
   实际产出来定 —— 先出 JSONL 看真实数字，再据此写 sql/007，顺序反了就是拍脑袋。

--------------------------------------------------------------------------
⚠️ 「留什么」是这个脚本的全部难点，两条判据叠加
--------------------------------------------------------------------------
① **目标是通用动画问答**，不是只答剧情 —— 剧情 / 角色 / OP·ED 都要能答。
② **但只留 Bangumi 没有的** —— 播出日期、话数、制作公司、staff 在
   `anime_profile` 里已是权威来源，再存一份是制造冲突源不是补充。

⇒ 萌娘百科真正独有的只有四样：**详细剧情概要 · 角色描述 · OP/ED 歌曲 · 剧透标记**

体积（前三行是 30 页样本外推，最后一行是全量实测）：

    整页不加区分地切                        122,785 条
    其他表格 140,661 / 各话列表 35,229 / 演职员 24,414   ← 全部不要
    ✅ 全量实测：2,233 个条目 → 19,526 条（prose 16,354 · songs 3,172）· 47 MB
    ⚠️ 样本外推曾给出 29,700 条，实际只有一半 —— **样本取的是热度前 30 的条目，
       正是页面最大的那批**。用头部样本外推全体必然高估，别再这么估。

📌 **2026-08-15 Neon 升级付费后，体积不再是取舍理由**（全都要也才 $0.16/月，
   原先那条 13.7 万红线来自免费层 500 MB 会挂起项目，已作废）。
⚠️ **但上面的取舍一条都不用改**，因为主要判据始终是 ②「只留 Bangumi 没有的」——
   那是**正确性**问题（两个来源打架时该信谁），不是成本问题。
💡 这恰好说明当初按 ② 而不是按体积来论证是对的：**便宜下来之后结论不用重推。**

--------------------------------------------------------------------------
⚠️ 两个剧透信号，强弱不同，都要用
--------------------------------------------------------------------------
    heimu 行内标记  <span class="heimu">但是是男的</span>     精确但零散
    剧透提示框      「以下内容含有剧透成分，请酌情阅读」        整段剧情概要

**heimu 只有 21/28 个条目有**（无职转生、吹响吧上低音号是 0 个），
所以「有标记的一定是剧透，没标记的不代表不是」。剧透框补上了这个缺口 ——
它标的正是整段剧情概要，恰恰是 heimu 标不到的那部分。
⚠️ 剧透框藏在 `<div class="infoBoxText">` 里，**必须在 DROP_CLASS 删掉它之前
   就地记下来**（见 handle_tables），顺序反了信号就没了。

⬜ 章节级规则（「结局」「最终话」整节算剧透）等全量数据出来再定，现在加是拍脑袋。

--------------------------------------------------------------------------
为什么用 lxml 而不是正则
--------------------------------------------------------------------------
Parsoid HTML 的 <section data-mw-section-id="N"> **最大嵌套 3 层**（h2>h3>h4）。
正则切会把子节内容重复计入父节，或者整段漏掉 —— 不报错，但语料错了，
第 5 周评测才发现。lxml 只在 etl 组，不进主依赖组（线上不碰 HTML）。

用法：
    uv run --group etl python scripts/parse_moegirl.py --limit 20   # 先看几个
    uv run --group etl python scripts/parse_moegirl.py              # 全量
    uv run --group etl python scripts/parse_moegirl.py --stats-only # 只统计不写文件
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import statistics as st
import sys
from collections import Counter
from pathlib import Path

from lxml import html as lxml_html
from tqdm import tqdm

# ⚠️ 进度条在**非 TTY**（重定向到日志文件）时必须压低刷新频率。
#    tqdm 在非 TTY 下每次刷新都追加一段输出，而 set_postfix_str 会触发刷新 ——
#    实测不压的话 200 页就刷出几百行，日志完全没法看。
#    ⇒ TTY 下 0.5 秒刷一次（看着流畅），非 TTY 下 30 秒一次（日志够用）。
TTY = sys.stderr.isatty()
BAR_INTERVAL = 0.5 if TTY else 30.0


def make_bar(total: int, desc: str, unit: str) -> tqdm:
    return tqdm(total=total, desc=desc, unit=unit, ascii=True, ncols=78,
                mininterval=BAR_INTERVAL)


RAW_DIR = Path("data/raw/moegirl")
MANIFEST = RAW_DIR / "manifest.jsonl"
TITLE_MAP = Path("data/interim/moegirl_titles.json")
OUT = Path("data/interim/moegirl_chunks.jsonl")

# 阶段 06 的角色页。⚠️ **切分/清洗规则与作品页共用同一套** —— 实测 36 页角色页
#    产出 prose 193 + songs 4、零 credits 垃圾，章节是「简介/经历/能力」这类，
#    不需要为角色页另写一个解析器。两者唯一的差别是**作用域从哪来**（见 main）。
CHAR_RAW_DIR = Path("data/raw/moegirl_char")
CHAR_MANIFEST = CHAR_RAW_DIR / "manifest.jsonl"
CHAR_OUT = Path("data/interim/moegirl_char_chunks.jsonl")

# 目标 400 字：全量实测 2,233 个条目 → 19,526 条（中位 7 chunk/页、156 字/条）。
# 📌 存储不再是约束（升级付费后按 $0.35/GB-月 线性计费，3 万条约 $0.06/月），
#    所以这个数现在只该按**检索质量**来调，不用再对照什么天花板。
#    ⚠️ 但改小仍会成倍放大条数与编码成本（API 按 token 计费），别无脑调小。
TARGET, MAX_CHARS, MIN_CHARS = 400, 600, 80

# ⚠️ **table 不能一刀切剥掉。** 萌娘百科拿表格当**排版容器**用：剧透提示框、
#    台词引用框、剧情概要框全是 <table>。实测 30 页 1,149 个表格里有 36 个是
#    这种容器，共 40,405 字 —— 一刀切会丢掉《辉夜》的剧情简介和《JOJO》的分部概要。
# 判据（实测定的）：单元格 ≤40 且平均每格 ≥60 字 → 排版容器，保留并转成 <p>；
#                   否则是数据表（分集列表/STAFF表/单行本列表），丢掉。
#    数据表的特征就是「格子多、每格短」，两者分得很开。
#
# 🚨 **上限从 4 放宽到 40（2026-08-21）—— 原值在丢剧情，这是 F.4 ④ 的精确根因。**
#    实测同一页的《上条当麻》给出了完美对照：
#        「旧约」table  cells=2   密度=1715.5 汉字/格 → 保留
#        「新约」table  cells=18  密度= 489.4        → ★整表删除（9,973 字）
#    每格 489 个汉字，**密度判据早就认定它是散文容器**，却被 cells<=4 先否决了 ——
#    那 18 个格子是里面嵌的几个小注释表凑出来的，不是数据表的特征。
#    ⚠️ 密度才是真判据，格子数只该用来挡「格子多且每格短」，不该单独一票否决。
#
#    收益（36 个角色页 + 2,302 个作品页实测）：
#        角色页 chunk 430→520 (+21%)、正文 +28%，新增 91 条里「经历」占 51 条
#               （鲁迪乌斯 3岁-74岁人生历程、上条当麻新约剧情），零垃圾
#        作品页 +0.8% chunk / +1.5% 字，多是「经历>漫画剧情」「用语剧情>时间线」
#    📌 这直接对上 B.1 可回答率那条 **「结局」12/12 全部未命中** —— 结局就住在
#       这些被整表删掉的折叠块里（萌娘用 mw-collapsible 折叠长剧情，而它是 table）。
#
# ⚠️ **为什么是 40 而不是取消上限**：试过 40 / 200 / 无上限，三档产出**逐字节相同**
#    —— 语料里根本没有 cells>40 的散文容器。既然如此就保留一个有限上限，
#    真来了个 500 格的怪东西时还有个兜底，不必赌密度判据在极端情况下也成立。
TABLE_MAX_CELLS, TABLE_MIN_AVG = 40, 60

# 📌 萌娘百科的剧透提示模板，**比 heimu 更强的剧透信号**：
#    heimu 是行内的、只标短语，且只有 21/28 个条目有；这个框标的是**整段剧情概要**。
#    正好补上「heimu 精确但不完整」那个缺口 —— 两个信号一起用。
SPOILER_BOX = re.compile(r"以下内容含有剧透成分[^。]*")

# 整棵子树丢掉的元素。
# ⚠️ **rt/rp 要丢** —— 萌娘百科用 ruby 注音模板，text_content() 会把 <rb> 的正文
#    和 <rt> 的注音拼在一起（见 heimu 探测时见到的 <ruby><rb>导师<rt>…）。
# 📌 **但别把所有"看起来像乱码"的文本都归到 ruby 头上。** 实测有一段
#    「乔乔"（JOJO、GioGio，ジョジョ娇娇、j j ioiojoojoo简江…」曾被误判为注音污染，
#    查原始 HTML 后发现：那是**逐字符的 <a> 链接**（J、O、J、O 各链到一个角色页）
#    加上 heimu 吐槽，**原文就长这样**，解析是忠实的。
#    ⇒ 判断解析对不对，要回去看原始 HTML，不能靠"读起来通不通顺"。
DROP_TAGS = ("style", "script", "figure", "sup", "audio", "video", "img",
             "rt", "rp")
DROP_CLASS = re.compile(
    r"\b(thumb|gallery\w*|navbar|navbox|infoBox\w*|noteTA\w*|toc|reference\w*|"
    r"mw-editsection|noprint|hlist|Tab(Label|Content)?\w*|magnify|mw-empty-elt|"
    r"template-ruby-hidden|"
    # ⚠️ 萌娘百科的导航框**不叫** navbox，叫 menu-*。猜标准 class 名会全漏 ——
    #    实测泄漏成这样：「乔纳森·乔斯达 • 迪奥·布兰度乔瑟夫·乔斯达 • 卡兹空条承太郎…」
    #    它们混在 section 0（前言）里，是纯链接串，对检索是噪声。
    r"menu-(item|content|popout|title)|six divs)\b")

# 这些章节整节丢掉：对剧情问答零价值，且几乎全是链接/模板残渣。
# ⚠️ **CAST / STAFF / 主题曲 这类必须在这里挡**，剥表格挡不住它们 ——
#    萌娘百科很多演职员表写在 <ul>/<dl> 里而不是 <table> 里，会直接漏进正文：
#      「灶门炭治郎：小林亮太→阪本奖悟 灶门祢豆子：高石あかり→…」
#    这种「人名：人名」的串对剧情检索是纯噪声，还会稀释同页的有效 chunk。
# ⚠️ 但**不要**加「制作人员」「音乐」这类 —— 它们剥掉表格后剩下的散文仍有信息量
#    （制作背景、乐曲创作故事）。挡的是**名单**，不是**话题**。
# ⚠️ **不能用 ^...$ 精确匹配。** 实测标题常带前后缀，精确匹配全漏：
#    「舞台剧CAST」「STAFF（仅列前后篇）」「CAST > 中配版（中国大陆）」
SKIP_EXACT = re.compile(
    r"^(外部链接|外部連結|注释|註釋|注释与外部链接|参考资料|參考資料|参考|参见|參見|"
    r"相关条目|相關條目|相关链接|注解|脚注|備註|备注|導航|导航|注釋)$")

# ============================================================
# chunk 分类 —— 决定「留什么」的核心
# ============================================================
# ⚠️ **2026-08-15 Kevin 定，两条叠加的判据：**
#
# ① 目标是**通用动画问答**，不是只答剧情。常见问题至少四类：
#      「什么时候播出的」   「大致剧情是什么」
#      「粉头发的角色叫啥」 「片头曲片尾曲是什么」
#    我最初只按剧情裁剪，把主题曲整章跳过了 —— 那是判断错误。
#
# ② **但只留 Bangumi 没有的。** ⚠️ 这条把 ① 又收了回去：
#    播出时间 / 话数 / 制作公司 / staff，`anime_profile` 里全都有
#    （`air_date` `air_year` `studios` `staff`，推荐的年代窗口筛选就靠它们）。
#    再从萌娘百科存一份**不是补充，是制造第二个事实来源** —— 两边不一致时
#    问答该信哪个？这与 A.9「唯一事实来源是数据库」是同一条纪律。
#
#    ⇒ 萌娘百科真正独有的只有四样：
#         详细剧情概要 · 角色描述 · OP/ED 歌曲 · 剧透标记
#    ⚠️ 声优确实是 Bangumi 的缺口，但**该走 dump 的两跳补齐**
#      （subject-characters ⋈ person-characters，见 CLAUDE.md 第四部分），
#       那是结构化且完整的，用 chunk 凑是错的解法。
#
# 各类体积（30 页样本外推，用于定去留；全量实测见文件顶部）：
#      其他表格   140,661 条  ❌ 价值不明（分集表/单行本表/发售表），量还最大
#      各话列表    35,229 条  ⬜ 贵且 Bangumi 有 episode 数据，暂缓
#      演职员声优  24,414 条  ❌ 走 dump 两跳，不用 chunk
#      信息框       1,960 条  ❌ 便宜，但 Bangumi 已有 → 冲突源
#      歌曲主题曲   3,035 条  ✅ **唯一来源，且几乎免费，留**
#
# 💡 **分类不只是为了取舍，也是为了检索。** 第 4 周是 BM25 + 向量混合检索：
#    散文走向量（语义相似），而歌名是**关键词查找**，走 BM25 更准。
#    存成带 kind 的 chunk，检索时就能按问题类型分流。
KIND_PAT = [
    ("songs",    re.compile(r"主题曲|主題曲|片头曲|片尾曲|片頭曲|插曲|OP$|ED$|"
                            r"歌曲|曲目|音乐|音樂|BGM|角色歌", re.IGNORECASE)),
    ("credits",  re.compile(r"CAST|STAFF|声优|聲優|配音|演员|演職員|職員|制作人员|"
                            r"製作人員|工作人员", re.IGNORECASE)),
    # ⚠️ 全量跑完才发现「各集」没被覆盖 —— 30 页样本里恰好没有这种写法。
    #    《蓝猫淘气3000问 > 各集标题》因此漏进 prose，产出 2,495 字的纯标题串。
    #    📌 教训：**分类规则要在全量上验，样本上「没出现」不等于「不存在」。**
    ("episodes", re.compile(r"各话|各話|各集|话数|話數|分集|集数|集數|剧集|劇集|"
                            r"集标题|集標題|标题列表|標題列表")),
    ("goods",    re.compile(r"单行本|單行本|发售|發售|商品|周边|BD|DVD|蓝光")),
    # galgame 条目常带整篇通关攻略（实测 SHUFFLE! 那条 2,769 字、0 个句号），
    # 对动画问答零价值
    ("guide",    re.compile(r"攻略|流程图|流程圖|路线图|路線圖|存档点|存檔點")),
]
# ⚠️ 分类保留但不入库的。都记着分类而不是直接删，是为了将来改主意时
#    只改这一行，不用重新推导「当初为什么删」。
DROP_KINDS = {"episodes", "goods", "credits", "info", "guide"}


# ⚠️ 复用同一条正则，**不另写一份** —— 两份迟早漂移。
SONGS_PAT = next(p for k, p in KIND_PAT if k == "songs")


def classify(title: str, text: str = "") -> str:
    """按章节标题（必要时看内容）判断 chunk 类型。"""
    for kind, pat in KIND_PAT:
        if title and pat.search(title):
            return kind
    if len(CREDIT_PAT.findall(text)) >= 3:
        return "credits"
    return "prose"


def skip_section(title: str) -> bool:
    return bool(title) and bool(SKIP_EXACT.match(title))

# 正文承载元素。⚠️ dd/dt 要留 —— 萌娘百科大量角色介绍写在定义列表里。
TEXT_TAGS = ("p", "li", "dd", "dt", "blockquote")

SENT_END = re.compile(r"(?<=[。！？!?…])|(?<=\n)")

# 音频播放器等 widget 在 Parsoid 里渲染成占位符，不是内容。
# ⚠️ ￼ 是 OBJECT REPLACEMENT CHARACTER，肉眼几乎看不见但会进向量。
#    ⚠️ 用转义而不是直接贴字符 —— 这两个字符在源码里是隐形的，贴进去没人看得见。
WIDGET_JUNK = re.compile("[\ufffc\u200b]|START_WIDGET.*?END_WIDGET")

# ⚠️ **内容判据，不是标题判据。** 实测按标题挡不住名单：`相关音乐` 一节里
#    既有「歌曲：歌手（CV.…）作词：… 作曲：…」的清单，也有创作背景的散文，
#    整节挡会误伤后者。而剥表格也挡不住 —— 这些写在 <li>/<dd> 里。
# 实测 30 页样本：这条规则命中 105/507 条（20.7% chunk / 27.6% 字数），
# 其中 65 条来自「相关音乐」。散文里连出现 3 次 CV./作词/作曲 的概率极低，误伤可忽略。
CREDIT_PAT = re.compile(r"CV[.:：]|作词|作曲|编曲|演唱[:：]|作詞|編曲")


def is_credit_list(text: str) -> bool:
    """像演职员/歌曲清单而不是散文。"""
    return len(CREDIT_PAT.findall(text)) >= 3 or (text.count("•") + text.count("・")) >= 3


# ⚠️ **OP/ED 的归属信息住在独立的短标题段落里，不在曲目行里。**
#    萌娘的排版是「<p>片头曲（OP）</p>」当小标题，后面跟曲目详情：
#
#        片尾曲(ED)                    ← 7 字，被 12 字下限当"残句"丢掉
#        曲名：偶尔也说说昔日吧…        ← 58 字，保留
#
#    结果：**结构 100% 丢失、内容 100% 保留** —— 语料里只剩一串曲子，
#    分不出哪首是 OP 哪首是 ED，而那正是留住 songs 的全部理由。
#    实测 2,302 页里 1,926 页（83.7%）带这类标注，全部丢失。
#    🚨 这是**第二个** OP/ED 杀手：第一个是 is_credit_list（已在下方修过），
#       这条躲在「丢掉『参见』这类残句」的注释后面，一直没被发现。
#
# ⚠️ **修法是吸收成前缀，不是放宽 12 字下限。** 放宽会把「参见」残句放回来；
#    而且标题单独成 block 时，chunk_blocks 可能把它和曲目切进**不同的 chunk**
#    （药屋的「相关音乐」就切成了 2 条）—— 贴成前缀则切到哪里都不丢归属。
SONG_LABEL = re.compile(
    r"^(?:"
    r"第[0-9一二三四五六七八九十]+[期季部]|"
    r"(?:片头曲|片頭曲|片尾曲|片尾|片头|主题曲|主題曲|插曲|插入曲|插入歌|"
    r"印象曲|印象歌|角色歌|character\s*song)"
    r"(?:\s*[（(][^）)]{0,8}[）)])?\s*\d*|"
    r"(?:OP|ED|IN|IM)\s*\d*"
    r")$", re.IGNORECASE)

# 一首歌的字段被拆成多段时的续行（《电波女与青春男》：曲名一段、作词一段、
# 歌手一段）。逐段贴前缀会得到「片头曲(OP)：作词…」这种割裂的重复行。
SONG_CONT = re.compile(r"^(?:作词|作曲|编曲|作詞|編曲|演唱|歌|CV)[、，,]?[^:：]{0,6}[:：]")

# 期数与曲类是两级：`第1期` 之下可以有 OP/ED/插曲各若干。
SONG_STAGE = re.compile(r"^第[0-9一二三四五六七八九十]+[期季部]$")


def attach_song_labels(blocks: list) -> list:
    """把 songs 章节里的分组标题吸收成随后曲目行的前缀。

    ⚠️ 标题 block 本身**从列表里去掉** —— 它已经贴进后面的行了，留着会
       重复，而且它本来就撑不过 12 字下限。
    ⚠️ 只吸收「标题后面真的跟着曲目」的情况；一节末尾孤零零的标题直接丢，
       与原行为一致。
    """
    stage = kind = ""
    out = []
    for txt, hm, boxed in blocks:
        if SONG_LABEL.match(txt):
            if SONG_STAGE.match(txt):
                stage, kind = txt, ""     # 换期时曲类归零，否则会串到下一期
            else:
                kind = txt
            continue
        if out and SONG_CONT.match(txt):
            prev = out[-1]
            out[-1] = (prev[0] + " " + txt, prev[1] + hm, prev[2] or boxed)
            continue
        prefix = " ".join(x for x in (stage, kind) if x)
        out.append((f"{prefix}：{txt}" if prefix else txt, hm, boxed))
    return out


def n_cjk(s: str) -> int:
    return len(re.findall(r"[一-鿿]", s))


def handle_tables(root) -> None:
    """数据表丢掉；排版容器型表格保留，就地换成 <p> 以便走后续正文逻辑。"""
    for tb in root.xpath(".//table"):
        parent = tb.getparent()
        if parent is None:
            continue
        text = re.sub(r"\s+", " ", tb.text_content()).strip()
        c = n_cjk(text)
        cells = len(tb.xpath(".//td|.//th"))
        if c >= 40 and cells <= TABLE_MAX_CELLS and c / max(cells, 1) >= TABLE_MIN_AVG:
            p = lxml_html.Element("p")
            # ⚠️ **剧透标记必须在这里就地记下来。** 那句「以下内容含有剧透成分」
            #    在 <div class="infoBoxText"> 里，而 DROP_CLASS 的 infoBox\w*
            #    紧接着就会把它删掉 —— 等到切 chunk 时再找已经没了。
            #    这是「清洗顺序决定能不能拿到信号」的典型，别把它挪到后面。
            if SPOILER_BOX.search(text):
                p.set("data-spoiler-box", "1")
            # 保留原表格里的 heimu 子树 —— 直接塞纯文本会丢掉行内剧透标记
            for child in list(tb):
                p.append(child)
            p.text = tb.text or ""
            parent.replace(tb, p)
        else:
            parent.remove(tb)


def restore_language_variants(root) -> None:
    """把 LanguageVariant 藏在属性里的文本填回节点。

    ⚠️ **不做这一步会静默吃掉日文原名和歌曲名。** Parsoid 在语言转换被禁用时
       把内容放进 data-mw-variant **属性**，节点本身是空的：

         <span lang="ja"><span typeof="mw:LanguageVariant"
               data-mw-variant='{"disabled":{"t":"紅蓮の弓矢"}}'/></span>

       后果：「片头曲(OP)」那条只剩「歌：Linked Horizon 作词、作曲：Revo」，
       **曲名恰恰是缺的那部分** —— 而留住 songs 的全部理由就是回答
       「片头曲是什么」。同一根因也造成过「《进击的巨人》（日语：；英语：）」的空括号。
    """
    for el in root.xpath(".//*[@data-mw-variant]"):
        if (el.text_content() or "").strip():
            continue
        try:
            d = json.loads(el.get("data-mw-variant") or "{}")
        except (ValueError, TypeError):
            continue
        t = ((d.get("disabled") or {}).get("t")) or ""
        t = re.sub(r"<[^>]+>", "", t)
        # ⚠️ 属性里放的是 **wikitext 片段**，不是 HTML —— 只去标签会留下
        #    「想風]]」「蒼空の炎]]」这类残尾。内链取显示名（[[A|B]] → B）。
        t = re.sub(r"\[\[(?:[^\[\]|]*\|)?([^\[\]|]*)\]\]", r"\1", t)
        t = t.replace("[[", "").replace("]]", "").strip()
        if t:
            el.text = t


def clean_tree(root) -> None:
    """就地删掉图注、导航框等非正文子树；表格另行处理（见 handle_tables）。"""
    # ⚠️ 必须在任何删除之前跑 —— 它是"补内容"，被删掉的节点补不回来
    restore_language_variants(root)
    handle_tables(root)
    # ⚠️ 必须先 xpath 收集再删。边 .iter() 边删会打乱迭代器，静默漏掉元素。
    for tag in DROP_TAGS:
        for el in root.xpath(f".//{tag}"):
            p = el.getparent()
            if p is not None:
                p.remove(el)
    for el in root.xpath(".//*[@class]"):
        if DROP_CLASS.search(el.get("class") or ""):
            p = el.getparent()
            if p is not None:
                p.remove(el)


def section_title(sec) -> str:
    """取本节自己的标题（h2/h3/h4），不含子节的。"""
    for h in sec.xpath("./h2|./h3|./h4|./h5"):
        t = re.sub(r"\s+", " ", h.text_content()).strip()
        if t:
            return t
    return ""


def own_blocks(sec) -> list:
    """本节**直属**的正文元素 —— 排除嵌套子 <section> 里的。

    ⚠️ 这是用 lxml 的全部理由。直接 .//p 会把子节的段落也捞进来，
       父节和子节各存一份 → chunk 重复、检索时同一段话反复命中。
    """
    out = []
    for el in sec.iter(*TEXT_TAGS):
        a = el.getparent()
        skip = False
        while a is not None and a is not sec:
            # 子 section 的内容归子 section
            # ⚠️ 另一个必须挡的：**祖先本身也是正文元素**。表格被降级成 <p> 之后，
            #    单元格里原有的 <p>/<li> 会被再采一次 → 同一段话进两条 chunk。
            if a.tag == "section" or a.tag in TEXT_TAGS:
                skip = True
                break
            a = a.getparent()
        if not skip:
            out.append(el)
    return out


def block_text(el) -> tuple[str, int]:
    """返回 (正文, 其中被 heimu 包裹的字数)。

    📌 **已知且不修：日文/英文原名会缺失**，产出「《JOJO的奇妙冒险》（日语：；英语：）」
       这种空括号。查过原始 HTML，**不是解析 bug** —— Parsoid 在语言转换被禁用时
       把内容放进**属性**而不是文本节点：
           <span lang="ja"><span typeof="mw:LanguageVariant"
                 data-mw-variant='{"disabled":{"t":"<b>ジョジョの奇妙な冒険</b>"}}'/></span>
       span 本身是空的，text_content() 返回空是对的。
       不修的理由：原名在 anime_profile.name 里已经有了，对剧情问答也无价值。

    ⚠️ heimu 是**行内**的（一句话里嵌一个剧透短语），不是整段。所以剧透判定
       只能落到 chunk 级：算出这段里有多少字来自 heimu，再按比例决定。
    """
    txt = WIDGET_JUNK.sub("", re.sub(r"\s+", " ", el.text_content())).strip()
    hm = 0
    for sp in el.xpath('.//*[contains(@class,"heimu")]'):
        hm += len(re.sub(r"\s+", " ", sp.text_content()).strip())
    # handle_tables 在清洗前就把剧透框标在这个属性上了（那时原文还没被删）
    boxed = el.get("data-spoiler-box") == "1"
    return txt, min(hm, len(txt)), boxed


def split_sentences(text: str) -> list[str]:
    """按句末标点切；**再对超长片段做硬切兜底**。

    ⚠️ 没有兜底的话，一段没有句号的长文本会整个变成一条 chunk ——
       全量实测出现 39 条超过 MAX_CHARS，最长 **2,769 字**
       （SHUFFLE! 的 galgame 攻略，全文 0 个句号）。
       这类 chunk 会在 embedding 里稀释成一团噪声，还可能超模型输入预期。
    ⚠️ **分类规则总会有漏网**（「各集标题」就漏过），所以硬切是必须的最后一道 ——
       不能指望上游把所有列表类内容都识别出来。
    """
    parts = [p for p in SENT_END.split(text) if p and p.strip()]
    parts = parts or ([text] if text.strip() else [])
    out: list[str] = []
    for p in parts:
        while len(p) > MAX_CHARS:
            # 优先在次级标点断开，避免把词切碎；找不到就按长度硬切
            cut = max((p.rfind(c, 0, MAX_CHARS) for c in "，、；)）】」』 "), default=-1)
            if cut < MAX_CHARS // 2:
                cut = MAX_CHARS
            out.append(p[:cut + 1])
            p = p[cut + 1:]
        if p:
            out.append(p)
    return out


def chunk_blocks(blocks: list[tuple[str, int, bool]]) -> list[tuple[str, int, bool]]:
    """把同一节的若干段落拼成 ~TARGET 字的 chunk，尽量在句子边界断开。

    返回 [(chunk 正文, 其中的 heimu 字数, 是否来自剧透框)]。
    """
    chunks: list[tuple[str, int, bool]] = []
    buf, buf_hm, buf_box = "", 0, False
    for text, hm, boxed in blocks:
        # 段落内 heimu 占比，按字数摊到句子上（够用，不追求精确到字）
        ratio = hm / len(text) if text else 0.0
        for sent in split_sentences(text):
            if len(buf) + len(sent) > MAX_CHARS and len(buf) >= MIN_CHARS:
                chunks.append((buf.strip(), buf_hm, buf_box))
                buf, buf_hm, buf_box = "", 0, False
            buf += sent
            buf_hm += int(len(sent) * ratio)
            buf_box = buf_box or boxed
            if len(buf) >= TARGET:
                chunks.append((buf.strip(), buf_hm, buf_box))
                buf, buf_hm, buf_box = "", 0, False
    if buf.strip():
        # ⚠️ 尾巴太短就并回上一条，避免产出一堆 10 字的碎片
        if chunks and len(buf.strip()) < MIN_CHARS:
            prev, prev_hm, prev_box = chunks[-1]
            chunks[-1] = (f"{prev}{buf.strip()}", prev_hm + buf_hm, prev_box or buf_box)
        else:
            chunks.append((buf.strip(), buf_hm, buf_box))
    return chunks


def parse_page(html: str) -> list[dict]:
    doc = lxml_html.fromstring(html)
    clean_tree(doc)
    out: list[dict] = []
    for sec in doc.xpath(".//section[@data-mw-section-id]"):
        title = section_title(sec)
        # 面包屑：父节标题 > 本节标题，检索时能看出上下文
        crumb, a = [], sec.getparent()
        while a is not None:
            if a.tag == "section":
                t = section_title(a)
                if t:
                    crumb.append(t)
            a = a.getparent()
        crumb.reverse()

        # ⚠️ 祖先被跳过的话子节也要跳。实测「CAST > 中配版（中国大陆）」——
        #    子节自己的标题不含 CAST，只判自己会把整张中配表放进语料。
        if any(skip_section(t) for t in [*crumb, title]):
            continue
        path = " > ".join([*crumb, title]) if title else " > ".join(crumb)

        blocks = [block_text(el) for el in own_blocks(sec)]
        # ⚠️ **必须在 12 字下限之前** —— 分组标题只有 6~7 字，过滤之后就没了。
        if SONGS_PAT.search(" ".join([*crumb, title])):
            blocks = attach_song_labels(blocks)
        blocks = [b for b in blocks if len(b[0]) >= 12]      # 丢掉「参见」这类残句
        if not blocks:
            continue

        # 分类看**本节到根的整条路径** —— 子节标题常是「第1期」这种无信息量的，
        # 类型信息在父节（「主题曲 > 第1期」）。
        kind = classify(" ".join([*crumb, title]), " ".join(b[0] for b in blocks))
        if kind in DROP_KINDS:
            continue

        made = chunk_blocks(blocks)
        for text, hm, boxed in made:
            # 剧透框那句模板套话本身不是内容，从正文里剔掉（信号已单独记）
            boxed = boxed or bool(SPOILER_BOX.search(text))
            text = SPOILER_BOX.sub("", text).strip()
            # ⚠️ **一节只有这一条时放宽下限。** 原先一律按 MIN_CHARS 丢，
            #    结果把开篇那句最重要的摘要删了 ——
            #    「《进击的巨人》是由谏山创创作的一部漫画，于讲谈社…连载」只有 76 字。
            #    短不等于没用；真正没用的是**碎片**，而独占一节的短句不是碎片。
            floor = 40 if len(made) == 1 else MIN_CHARS
            if len(text) < floor:
                continue
            # ⚠️ 名单过滤**只对 prose 生效**。songs 里天然全是「作词/作曲」，
            #    拿同一条规则去卡会把主题曲整类删光 —— 正是最初丢掉 OP/ED 的原因。
            if kind == "prose" and is_credit_list(text):
                continue
            out.append({
                "kind": kind,
                "section": path or "(前言)",
                "section_id": int(sec.get("data-mw-section-id")),
                "text": text,
                "n_chars": len(text),
                "heimu_chars": hm,
                # 两个独立的剧透信号，分开记以便下游分别调
                "spoiler_box": boxed,
                "spoiler_level": 1 if (hm > 0 or boxed) else 0,
            })
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="解析萌娘百科 HTML 为 chunk")
    ap.add_argument("--limit", type=int, help="只处理前 N 个条目")
    ap.add_argument("--stats-only", action="store_true", help="只统计不写文件")
    ap.add_argument("--kind", choices=("series", "character"), default="series",
                    help="解析作品页（默认）还是角色页")
    a = ap.parse_args()

    char = a.kind == "character"
    raw_dir = CHAR_RAW_DIR if char else RAW_DIR
    manifest = CHAR_MANIFEST if char else MANIFEST
    out_path = CHAR_OUT if char else OUT

    rows = [json.loads(x) for x in
            manifest.read_text(encoding="utf-8").splitlines() if x.strip()]
    # 同一 pageid 可能被抓过两次（重跑），保留最后一条
    rows = list({r["pageid"]: r for r in rows}.values())
    if a.limit:
        rows = rows[:a.limit]

    # pageid → 它覆盖的系列根（一个条目可能对应多个系列，见 fetch 脚本的去重说明）
    # ⚠️ **两种页面的作用域来源不同，这是它们唯一的实质差别**：
    #    作品页靠标题解析（moegirl_titles.json：series_root → pageid）；
    #    角色页没有标题解析这一步，作用域是**抓取时**从「哪个作品页链到它」
    #    推出来的，已经记在 char manifest 的 series_roots 里。
    #    ⚠️ 一个角色可以属于多部作品（Fate 系列尤其），所以那是个列表不是单值。
    if char:
        roots = {r["pageid"]: r.get("series_roots", []) for r in rows}
    else:
        tmap = json.loads(TITLE_MAP.read_text(encoding="utf-8"))
        roots = {}
        for root, v in tmap.items():
            roots.setdefault(v["pageid"], []).append(int(root))

    fh = None if a.stats_only else out_path.open("w", encoding="utf-8")
    per_page, sec_counter, n_chunks, n_spoil, sizes = [], Counter(), 0, 0, []
    skipped = 0
    bar = make_bar(len(rows), "解析", "页")
    for r in rows:
        f = raw_dir / f"{r['pageid']}.html.gz"
        if not f.exists():
            skipped += 1
            continue
        html = gzip.decompress(f.read_bytes()).decode("utf-8")
        chunks = parse_page(html)
        per_page.append(len(chunks))
        for k, c in enumerate(chunks):
            sec_counter[c["section"].split(" > ")[0]] += 1
            sizes.append(c["n_chars"])
            n_spoil += c["spoiler_level"]
            if fh:
                fh.write(json.dumps({
                    "pageid": r["pageid"], "title": r["title"],
                    "series_roots": roots.get(r["pageid"], []),
                    "chunk_no": k, **c,
                }, ensure_ascii=False) + "\n")
        n_chunks += len(chunks)
        bar.update(1)
        # refresh=False：让它等下一次自然刷新，别每条 chunk 都重绘
        bar.set_postfix_str(f"{n_chunks:,} chunk", refresh=False)
    bar.close()
    if fh:
        fh.close()

    print(f"\n条目 {len(per_page):,} 个"
          + (f"（缺文件跳过 {skipped}）" if skipped else ""))
    print(f"chunk {n_chunks:,} 条 · 每页中位 {st.median(per_page):.0f} · "
          f"均 {st.mean(per_page):.1f}")
    print(f"chunk 字数 中位 {st.median(sizes):.0f} · 均 {st.mean(sizes):.0f} · "
          f"max {max(sizes)}")
    print(f"带剧透标记 {n_spoil:,} 条 ({n_spoil / n_chunks:.1%})")

    done = len(per_page)
    # ⚠️ 这里曾把条目总数写死成 1,494，语料一扩就变成了错的输出。
    #    用 --limit 时才需要外推，全量时直接报实数。
    total_pages = len(rows) if not a.limit else None
    if a.limit:
        # 外推基数取 manifest 的真实条目数，不要写死
        allrows = sum(1 for x in manifest.read_text(encoding="utf-8").splitlines() if x.strip())
        est = n_chunks / done * allrows
        print(f"\n外推到 {allrows:,} 个条目: {est:,.0f} 条 "
              f"（约 {est * 2400 / 1e6:.0f} MB · 存储 ${est * 2400 / 1e9 * 0.35:.2f}/月）")
    else:
        print(f"\n全量 {total_pages:,} 个条目 · {n_chunks:,} 条 "
              f"（约 {n_chunks * 2400 / 1e6:.0f} MB · "
              f"存储 ${n_chunks * 2400 / 1e9 * 0.35:.2f}/月）")
    print("\n章节来源 top 12:")
    for s, n in sec_counter.most_common(12):
        print(f"   {n:>6,}  {s[:34]}")
    if not a.stats_only:
        print(f"\n→ {out_path}")


if __name__ == "__main__":
    main()
