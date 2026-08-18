"""翻译缓存 —— SQLite，键 = hash(模型 + prompt 版本 + 源文本)。

⚠️ **它比 embed_cache 更不能丢。** 编码一次全量是 ¥0.19 / 12 分钟，重来还能忍；
   翻译 47,066 条是**几小时的墙钟时间**（MT 单批 ~40s，还带 5 分钟冷启动）。
   ⇒ 除本地 SQLite 外，译文**同时落库**（约 20 MB，2026-08-17 Kevin 批准），
     缓存丢了也能从库里恢复，不用重跑几小时。

⚠️ **为什么必须有缓存而不是直接写库**：源头是 dump，
   `load_profiles.py` / `build_char_chunks.py` 每次重跑都从 dump 重写文本。
   没有「源文本 → 译文」的映射，重跑就把日文写回去了 ——
   与 src/langclean.py 顶部记的是同一族故障，只是代价大得多。

📌 键里带 **PROMPT_VERSION**：改了翻译 prompt 会改变译文，
   不进键的话旧译文会被当成有效缓存复用（与 embed_cache 把 MODEL 进键同理）。
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "interim" / "translate_cache"
CACHE_PATH = CACHE_DIR / "translations.sqlite"


def key_of(text: str, model: str, prompt_version: str) -> str:
    raw = f"{model}\0{prompt_version}\0{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or CACHE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    # WAL：写入不阻塞读，崩溃后不留半个事务 —— 续传靠这个
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS translations (
            k          TEXT PRIMARY KEY,
            model      TEXT NOT NULL,
            src        TEXT NOT NULL,   -- 存源文本，便于事后审计/重放
            dst        TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def get_many(conn: sqlite3.Connection, texts: list[str],
             model: str, prompt_version: str) -> dict[str, str]:
    """批量查，返回 {源文本: 译文}。未命中的不出现在结果里。"""
    if not texts:
        return {}
    by_key = {key_of(t, model, prompt_version): t for t in texts}
    out: dict[str, str] = {}
    keys = list(by_key)
    # SQLite 参数上限默认 999，分片查
    for i in range(0, len(keys), 900):
        chunk = keys[i:i + 900]
        ph = ",".join("?" * len(chunk))
        for k, dst in conn.execute(
            f"SELECT k, dst FROM translations WHERE k IN ({ph})", chunk
        ):
            out[by_key[k]] = dst
    return out


def get_many_any(conn: sqlite3.Connection, texts: list[str],
                 prompt_version: str,
                 models: tuple[str, ...]) -> dict[str, str]:
    """跨模型查：**任何一个** models 里的模型翻过就算翻过。

    ⚠️ 多模型并跑时，「这条要不要翻」的判据必须是跨模型的 ——
       只查首选模型的话，Qwen3-8B 翻好的那几万条会被判成"没翻"，
       于是 MT 再翻一遍，多模型协作直接退化成重复劳动。

    ⚠️ **优先级 = models 的顺序，靠前的赢。** 必须确定性 ——
       否则同一份缓存两次建库得到不同语料，第 5 周评测不可复现。
    """
    if not texts:
        return {}
    out: dict[str, str] = {}
    # 倒序遍历：后写的覆盖先写的 ⇒ 最终留下的是优先级最高的那个
    for m in reversed(models):
        out.update(get_many(conn, texts, m, prompt_version))
    return out


def put_many(conn: sqlite3.Connection, pairs: list[tuple[str, str]],
             model: str, prompt_version: str) -> None:
    """批量写入并提交。

    ⚠️ **每批都 commit。** 续传粒度 = 提交粒度；攒到最后一次性提交，
       中途挂掉等于没跑（embed_cache 同一条纪律）。
    """
    if not pairs:
        return
    rows = [(key_of(s, model, prompt_version), model, s, d) for s, d in pairs]
    conn.executemany(
        "INSERT OR REPLACE INTO translations (k, model, src, dst) VALUES (?,?,?,?)",
        rows)
    conn.commit()


def stats(conn: sqlite3.Connection) -> tuple[int, float]:
    """(条数, MB)。"""
    n = conn.execute("SELECT count(*) FROM translations").fetchone()[0]
    mb = sum(f.stat().st_size for f in CACHE_DIR.glob("translations.sqlite*")
             if f.exists()) / 1048576
    return n, mb
