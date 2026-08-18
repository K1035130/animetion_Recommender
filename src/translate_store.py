"""译文的读取入口与库内备份 —— loader 通过这里把日文换成中文。

⚠️ **接进管道而不是事后 UPDATE。** 文本源头是 dump，
   `load_profiles.py` / `build_char_chunks.py` 每次重跑都从 dump 重写。
   一次性 UPDATE 会被下次重跑静默覆盖回日文 —— 与 src/langclean.py 同一族故障，
   只是代价大得多（几小时的翻译墙钟时间）。

⚠️ **调用顺序固定：先 strip_jp_tail() 再 to_chinese()。**
   缓存的键是**剥离之后**的文本（translate_corpus.py 从库里取的就是剥好的），
   顺序反了必然全部未命中，而那**不报错** —— 只是日文原样留在库里。

三层数据：
    本地 SQLite  data/interim/translate_cache/   ← loader 读这里（无网络往返）
    Postgres     translation 表                  ← 备份，换机器时 restore
    dump         原始日文                        ← 永远的兜底
"""

from __future__ import annotations

import hashlib

from src import translate, translate_cache

_MAP: dict[str, str] | None = None


def _load() -> dict[str, str]:
    """载入**所有受信模型**的译文，按 translate.ACCEPTED 的顺序定优先级。

    ⚠️ **这里曾经写死 `WHERE model = translate.MODEL`。** 多模型并跑之后，
       那样会让协作模型翻出来的几万条译文 loader **一条也查不到** ——
       而 to_chinese() 查不到就按设计返回原文，**不报错**，
       结果是日文原样灌进库。与「不入 git 的文件参与打分链路」同族：
       开发机上看着一切正常，错得完全静默。

    ⚠️ 倒序 update ⇒ 优先级高的最后写入、覆盖低的。顺序必须确定，
       否则两次建库拿到不同语料，第 5 周评测不可复现。
    """
    conn = translate_cache.connect()
    try:
        out: dict[str, str] = {}
        for m in reversed(translate.ACCEPTED):
            out.update(dict(conn.execute(
                "SELECT src, dst FROM translations WHERE model = ?", (m,)).fetchall()))
    finally:
        conn.close()
    return out


def to_chinese(text: str) -> str:
    """有译文就换成译文，没有就**原样返回**。

    ⚠️ 找不到译文时返回原文而不是抛异常：翻译是渐进完成的，
       loader 在译完之前也必须能跑通（只是那部分还是日文）。
       ⚠️ 代价是"漏译"不会报错 —— 所以灌完库要用 langclean.is_japanese()
          抽查一遍，别指望这一层告诉你哪里没译。
    """
    global _MAP
    if _MAP is None:
        _MAP = _load()
    return _MAP.get(text, text)


def reset_cache() -> None:
    """丢弃内存里的映射，下次调用重新载入。测试用。"""
    global _MAP
    _MAP = None


def stats() -> int:
    """当前可用译文条数。"""
    global _MAP
    if _MAP is None:
        _MAP = _load()
    return len(_MAP)


# ============================================================
# 与 Postgres 的双向同步（备份 / 恢复）
# ============================================================
def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def sync_to_db(conn, *, batch: int = 1000) -> int:
    """把本地缓存推进 translation 表。幂等（主键冲突则覆盖）。"""
    # ⚠️ 带上 model 列 —— 每条译文由哪个模型产出必须原样保留。
    #    库表主键是 (src_sha, model, prompt_version)，各模型天然共存；
    #    若统一写成首选模型，恢复回来就分不清出处，也无法事后按模型抽查质量。
    ph = ",".join("?" * len(translate.ACCEPTED))
    local = translate_cache.connect()
    try:
        rows = local.execute(
            f"SELECT src, dst, model FROM translations WHERE model IN ({ph})",
            translate.ACCEPTED).fetchall()
    finally:
        local.close()
    if not rows:
        return 0
    payload = [(_sha(s), m, translate.PROMPT_VERSION, s, d)
               for s, d, m in rows]
    with conn.cursor() as cur:
        for i in range(0, len(payload), batch):
            cur.executemany("""
                INSERT INTO translation
                    (src_sha, model, prompt_version, src, dst)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT ON CONSTRAINT translation_pk
                DO UPDATE SET dst = EXCLUDED.dst, created_at = now()
            """, payload[i:i + batch])
            conn.commit()      # ⚠️ 每批提交：中途挂掉不用从头再来
    return len(payload)


def restore_from_db(conn) -> int:
    """从 translation 表灌回本地缓存 —— 换机器后的第一步。"""
    with conn.cursor() as cur:
        cur.execute("""SELECT src, dst, model FROM translation
                        WHERE model = ANY(%s) AND prompt_version = %s""",
                    (list(translate.ACCEPTED), translate.PROMPT_VERSION))
        rows = cur.fetchall()
    if not rows:
        return 0
    # ⚠️ 按模型分组写回 —— 缓存的键含模型名，混着写会算出错的键，
    #    表现为"恢复成功但下次跑还是全部未命中"。
    by_model: dict[str, list[tuple[str, str]]] = {}
    for s, d, m in rows:
        by_model.setdefault(m, []).append((s, d))
    cache = translate_cache.connect()
    try:
        for m, pairs in by_model.items():
            translate_cache.put_many(cache, pairs, m, translate.PROMPT_VERSION)
    finally:
        cache.close()
    reset_cache()
    return len(rows)
