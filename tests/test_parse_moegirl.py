"""解析器规则的回归测试 —— 锁住 2026-08-21 那两条改动。

⚠️ **为什么用手写的最小 HTML 而不是真实页面**：真实的 Parsoid 页面动辄
   几十上百 KB，塞进 git 既臃肿又会随萌娘编辑而失效；而这里要锁的是**规则**，
   规则只需要能触发它的最小结构。真实页面的验证由抓取后的实测承担。

📌 覆盖两条改动（都是实测逼出来的，见 parse_moegirl.py 里的注释）：
   ① songs 分组标题吸收成前缀 —— 原先 12 字下限把「片头曲（OP）」当残句丢掉，
      导致 songs chunk 只剩一串曲名，分不出哪首是 OP。实测全库 83.7% 的页面受影响。
   ② TABLE_MAX_CELLS 4 → 40 —— 密度判据本来就能识别散文容器，
      但 cells<=4 的硬上限先把它否决了，《上条当麻》「新约」9,973 字因此整表丢失。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytest.importorskip("lxml", reason="解析器测试需要 uv sync --group etl")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

import parse_moegirl as pm


def _page(*sections: str) -> str:
    return "<html><body>" + "".join(sections) + "</body></html>"


def _sec(sid: int, title: str, body: str) -> str:
    h = f"<h2>{title}</h2>" if title else ""
    return f'<section data-mw-section-id="{sid}">{h}{body}</section>'


def _texts(html: str) -> list[str]:
    return [c["text"] for c in pm.parse_page(html)]


# ============================================================
# ① songs 分组标题
# ============================================================
SONGS_BODY = (
    "<p>片头曲（OP）</p>"
    "<p>花になって（第1~12话） 歌：绿黄色社会 作词：长屋晴子，作曲：穴见真吾，编曲：川口圭太</p>"
    "<p>片尾曲（ED）</p>"
    "<p>アイコトバ（第1~12话） 歌：アイナ・ジ・エンド 作词、作曲：石崎ひゅーい，编曲：トオミヨウ</p>"
)


def test_song_label_becomes_prefix():
    """标题贴到随后的曲目上 —— 没有它就分不出哪首是 OP。"""
    joined = " ".join(_texts(_page(_sec(1, "相关音乐", SONGS_BODY))))
    assert "片头曲（OP）：花になって" in joined
    assert "片尾曲（ED）：アイコトバ" in joined


def test_song_label_not_emitted_alone():
    """⚠️ 标题**本身**要从 block 列表里去掉，否则会重复出现一条孤立的『片头曲（OP）』。"""
    for t in _texts(_page(_sec(1, "相关音乐", SONGS_BODY))):
        assert t.strip() not in ("片头曲（OP）", "片尾曲（ED）")


def test_song_stage_is_two_level():
    """期数与曲类是两级：`第2期` 之下的 OP 不能串到第 1 期。"""
    body = ("<p>第1期</p><p>片头曲（OP）</p>"
            "<p>花になって（第1~12话） 歌：绿黄色社会 作词：长屋晴子，作曲：穴见真吾</p>"
            "<p>第2期</p><p>片头曲（OP）</p>"
            "<p>百花繚乱（第25~36话） 作词、作曲、歌：几田莉拉，编曲：KOHD</p>")
    joined = " ".join(_texts(_page(_sec(1, "相关音乐", body))))
    assert "第1期 片头曲（OP）：花になって" in joined
    assert "第2期 片头曲（OP）：百花繚乱" in joined


def test_song_stage_resets_kind():
    """⚠️ 换期时曲类必须归零 —— 否则第 2 期的第一首会继承第 1 期的『片尾曲』。"""
    body = ("<p>第1期</p><p>片尾曲（ED）</p>"
            "<p>アイコトバ（第1~12话） 歌：アイナ・ジ・エンド 作词、作曲：石崎ひゅーい</p>"
            "<p>第2期</p>"
            "<p>百花繚乱（第25~36话） 作词、作曲、歌：几田莉拉，编曲：KOHD 这一条前面没有曲类标题</p>")
    joined = " ".join(_texts(_page(_sec(1, "相关音乐", body))))
    assert "第2期 片尾曲（ED）：百花繚乱" not in joined
    assert "第2期：百花繚乱" in joined


def test_song_continuation_merged():
    """一首歌拆成多段时（《电波女与青春男》），续行并回上一条而不是各贴一个前缀。"""
    body = ("<p>片头曲（OP）</p><p>Os-宇宙人</p>"
            "<p>作词、作曲：の子，编曲：神圣放逐乐队</p>"
            "<p>歌：エリオをかまってちゃん（大龟明日香、神圣放逐乐队）</p>")
    joined = " ".join(_texts(_page(_sec(1, "相关音乐", body))))
    assert "片头曲（OP）：Os-宇宙人 作词、作曲：の子" in joined
    assert "片头曲（OP）：作词" not in joined
    assert "片头曲（OP）：歌：" not in joined


def test_song_label_only_in_songs_section():
    """⚠️ 只在 songs 章节生效 —— 散文里出现『插曲』二字不该触发前缀。"""
    body = ("<p>插曲</p>"
            "<p>这一段是普通的剧情描述，讲的是主角在旅途中遇到的一段插曲，"
            "与音乐无关，不应该被当成分组标题处理掉。</p>")
    joined = " ".join(_texts(_page(_sec(1, "剧情简介", body))))
    assert "：这一段是普通的剧情描述" not in joined


# ============================================================
# ② 表格判据
# ============================================================
def _cells(n: int, per: int) -> str:
    """n 个单元格，每格 per 个汉字。"""
    return "".join(f"<td><p>{'剧情' * (per // 2)}</p></td>" for _ in range(n))


def test_dense_multicell_table_kept():
    """🚨 《上条当麻》「新约」那一类：cells=18 但每格 489 汉字，是散文容器不是数据表。

    旧判据 cells<=4 会把它整表删掉（9,973 字），而密度判据本来就认得出来。
    """
    html = _page(_sec(1, "经历", f"<table><tbody><tr>{_cells(6, 100)}</tr></tbody></table>"))
    assert any("剧情剧情" in t for t in _texts(html)), "高密度多单元格表格被误删"


def test_sparse_table_dropped():
    """反向：格子多、每格短 = 真数据表（分集列表/STAFF 表），仍然要丢掉。"""
    html = _page(_sec(1, "各话标题", f"<table><tbody><tr>{_cells(10, 6)}</tr></tbody></table>"))
    assert _texts(html) == [], "低密度数据表没有被丢掉"


def test_table_cell_limit_is_finite():
    """⚠️ 上限是有限值而不是取消 —— 40/200/无上限三档产出相同，
    保留有限上限是为了给极端结构留个兜底。"""
    assert 4 < pm.TABLE_MAX_CELLS < 10**6


# ── 套话式 (前言)（2026-08-23）──────────────────────────────────

@pytest.mark.parametrize(("text", "drop", "why"), [
    ("伊丽莎白（日语：エリザベス）是由空知英秋创作的漫画《银魂》及其衍生作品的登场角色。",
     True, "纯套话：名字 + 日文读音 + 属于哪部作品，三样都在 alias/scope 里"),
    ("姬蒲（日语：姫蒲（ひめがま））是由A-1 Pictures制作的原创动画《Lycoris Recoil》的登场角色。",
     True, "同上"),
    ("楠幸村是平坂读创作的轻小说《我的朋友很少》及其衍生的动画等作品的登场角色，女主角之一。",
     False, "🚨 保护②：泛称在**召回层是有用信号**（女主角是谁 → 0.999 命中）"),
    ("张楚岚是米二创作的漫画《一人之下》及其衍生作品的男主角。",
     False, "保护②"),
    ("辩护律师：绫里千寻见面：美柳勇希学生、恋人：美柳千奈美尾并田美散是Capcom所创作的游戏《逆转裁判》及其衍生作品的登场角色。登场于《逆转裁判3》。",
     False, "🚨 保护③：前半截是人物关系表残渣，多句判据把它挡在外面"),
    ("《进击的巨人》是由谏山创创作的一部漫画，于讲谈社《别册少年Magazine》连载，讲述人类与巨人的战斗。",
     False, "作品页开篇摘要，不是角色定义句"),
])
def test_boilerplate_lede(text, drop, why):
    """🚨 实测：24 道作品级问题里套话前言占前 8 席位的 8.2%，且集中爆发
    （《我们的重制人生》4/8 席、《一人之下》3/8 席）。机制是短文本 + 标题词
    让向量相似度虚高。⚠️ 三条保护缺一不可，理由见 _is_boilerplate_lede 注释。
    """
    assert pm._is_boilerplate_lede({"section": "(前言)", "text": text}) is drop, why


def test_boilerplate_lede_never_empties_a_page():
    """保护①：整页只有这一条时原样退回 —— 否则那个角色整个从检索里消失。

    ⚠️ 实测全库有 13 页属于这种情况。
    """
    only = [{"section": "(前言)", "text": "伊丽莎白是《银魂》及其衍生作品的登场角色。"}]
    assert len(pm._drop_boilerplate_lede(only)) == 1
    withmore = [*only, {"section": "经历", "text": "正文" * 60}]
    assert len(pm._drop_boilerplate_lede(withmore)) == 1


# ── 专题模板 chrome（CHROME_CLASS）─────────────────────────────

def _chrome(inner: str, cls: str = "mw-collapsible toggle-template-container") -> str:
    return f'<div class="{cls}"><p>{inner}</p></div>'


def test_chrome_banner_is_dropped():
    """站务公告属于模板 chrome，不是条目内容。

    实测 84 个柯南条目、12 个崩坏3 条目的 (前言) 都以这类横幅开头，
    它们让同一 IP 下几十个页面的开头**完全相同**，检索时互相抬轿。
    """
    banner = "欢迎您一同参与建设名侦探柯南的相关条目♥编辑交流群：24733986"
    body = "《名侦探柯南》是由青山刚昌创作的漫画，讲述高中生侦探工藤新一" * 3
    out = " ".join(_texts(_page(_sec(0, "", _chrome(banner) + f"<p>{body}</p>"))))
    assert "编辑交流群" not in out, "站务公告应被 CHROME_CLASS 删掉"
    assert "青山刚昌" in out, "同一节里的正文必须原样保留"


def test_chrome_rule_never_eats_long_content():
    """🚨 回归：584,805 字那次事故。

    首版把 `toggle-template-button` 无条件放进 DROP_CLASS —— 而它是
    mw-collapsible 的**内容体**，装什么完全看条目：
        名侦探柯南   站务公告        34 字      ← chrome
        崩坏3       版本公告        842~1,150 ← chrome
        逆转裁判     **每一章的剧情** 1,724~2,582
        数码宝贝     **整篇正文**     14,284
    实测 126 个页面受损、4 页被清空，《逆转裁判》86 条 chunk → 21 条。

    ⚠️ 加长度阈值那一版被逆转裁判证伪：chrome 最大 1,150、内容最小 1,724，
       **只隔 574 字**，任何绝对阈值都会两头都错（与 B.4「1e-3 地板调不稳」同构）。
    ⇒ 最终只保留结构上恒为导航的 container/columns hint/ztdh，
       **`toggle-template-button` 必须不在名单里**。这条测试就锁这一点。
    """
    plot = "成步堂龙一发现了此案真相，把真凶山野从证人席上揪了出来。" * 40
    html = _page(_sec(0, "", _chrome(plot, "mw-collapsible toggle-template-button")))
    assert "成步堂龙一" in " ".join(_texts(html)), (
        "toggle-template-button 里可能是真剧情，不能无条件删")
    # 体量上限同样不能救它 —— 判据必须是 class 而不是长度
    assert not pm.CHROME_CLASS.search("mw-collapsible toggle-template-button")


# ============================================================
# ⑤ 元信息章节的 heimu 豁免（META_SECTION）
# ============================================================
#
# 🚨 触发案例《神枪少女》：「衍生作品 > 动画版」176 字**全是播出日期、话数、
#    制作阵容**，一句剧情都没有，却因末句吐槽里 14 字 heimu（8.0%）被整条判为
#    剧透 ⇒ 问「什么时候播出的」在默认路径下答不出来。
#    实测全库 31.6% 的 chunk 标了剧透、95% 靠 heimu、heimu 中位占比只有 7.4%。


def _spoil(section: str, body: str) -> int:
    """给定章节标题与正文，返回解析出来的 spoiler_level。

    ⚠️ **章节名不能随便挑**：「制作人员」「各话标题」这类会被 KIND_PAT 判成
       credits / episodes，而 `DROP_KINDS` 整类不入库 ⇒ 一条 chunk 都没有，
       断言拿到空列表。写这里的样本要先确认它真的产出 chunk。
    ⚠️ 正文也要够长：单节页面的长度地板是 40 字，而 SPOILER_BOX 那 27 字套话
       是**先被剥掉**再算长度的。
    """
    out = pm.parse_page(_page(_sec(1, section, body)))
    assert out, "样本没产出 chunk，测试本身失效了"
    return out[0]["spoiler_level"]


META_BODY = ('<p>动画第1期改编自原作前两卷，于2003年10月8日起播放，全13话。'
             '第2期于2008年1月7日起播放，全13话。2期将1期的制作阵容完全更换，'
             '<span class="heimu">原作者表示对1期的风格不怎么感冒</span></p>')
PLOT_BODY = ('<p>男主角在最终话与宿敌决战，最后'
             '<span class="heimu">与女主角一同离开了这座城市</span>，'
             '故事就此落幕，留下了许多未解之谜与伏笔等待续作揭晓。</p>')


@pytest.mark.parametrize("section, expect", [
    ("衍生作品", 0),      # 触发案例本身
    ("相关音乐", 0),
    ("出版信息", 0),
    ("跨媒体作品", 0),
    ("剧情简介", 1),      # ⚠️ 讲剧情的章节一律不豁免
    ("人物经历", 1),
    ("结局", 1),
])
def test_meta_section_exempts_heimu(section, expect):
    assert _spoil(section, META_BODY) == expect


def test_plot_section_keeps_heimu_spoiler():
    """回归：豁免只针对元信息章节，剧情章节的 heimu 仍然是剧透信号。"""
    assert _spoil("剧情简介", PLOT_BODY) == 1


def test_spoiler_box_is_never_exempted():
    """🚨 剧透框**不豁免**，哪怕它出现在元信息章节里。

    heimu 是行内的、大量用于吐槽；而剧透框是编者明确声明「以下内容含有剧透」，
    标的是整段剧情概要 —— 出现在哪个章节都该当真。
    ⚠️ 灌库前实测确认「被豁免的 chunk 里仍带剧透框的 = 0 条」，这条测试锁住它。
    """
    body = ('<p>以下内容含有剧透成分，请酌情阅读。动画第2期的结局中，主角一行人'
            '最终解散，各奔东西，男主角独自踏上旅途，留下了一个开放式的结尾，'
            '令许多观众意犹未尽。</p>')
    assert _spoil("衍生作品", body) == 1


def test_meta_exemption_does_not_touch_text():
    """⚠️ 只改标注，**正文一个字都不能动** —— 灌库实测总 chunk 数 62,651 不变。"""
    out = pm.parse_page(_page(_sec(1, "衍生作品", META_BODY)))
    assert "2003年10月8日" in out[0]["text"]
    assert "不怎么感冒" in out[0]["text"], "heimu 的文字仍在正文里，只是不再触发门控"
    assert out[0]["heimu_chars"] > 0, "字数照记 —— 将来要改判据还得靠它"
