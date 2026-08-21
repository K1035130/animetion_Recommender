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
