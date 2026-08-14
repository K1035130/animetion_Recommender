"""embedding 的本地缓存 —— 可复现性的唯一来源。

⚠️ **这不是「优化」，是唯一能让第 5 周评测可复现的东西。**
   实测（CLAUDE.md A.7）：API 不是逐请求确定的，服务端连续批处理会把你的
   请求和其他用户在飞的请求拼成一个 batch，同一条文本两次请求逐位差 0~2e-3，
   **控制自己的 batch size 也规避不了**。
   ⇒ 重发请求永远拿不回同一批数字，只有从缓存重放才是精确的。

三个作用，按重要性：

  1. **可复现**   —— 重建库 = 从缓存回放 = bit-identical
  2. **可续传**   —— 全量 27 分钟的任务挂在第 20 分钟，已编码的不白跑
  3. **省重复**   —— 改切分策略时只有真正变了的文本要重新请求

存哪：data/interim/embed_cache/vectors.sqlite
⚠️ 这个路径**已被现有忽略规则覆盖**，不用改配置 —— .gitignore 和 .vercelignore
   都是 `data/interim/*` + `!data/interim/tag_vocab.json` 的写法。

为什么用 SQLite：
  · 标准库自带，不引新依赖（parquet 要 pyarrow）
  · 一文本一文件的话是 11 万个小文件，Windows 上很糟
  · 随机读 + 原子写 + 崩溃安全，续传是白送的
"""

from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import numpy as np

from src import embed

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "interim" / "embed_cache"
CACHE_PATH = CACHE_DIR / "vectors.sqlite"


def key_of(text: str) -> str:
    """缓存键 = hash(模型 + 维度 + 文本)。

    ⚠️ **模型和维度必须进键。** 否则换了模型之后旧向量会被当成有效缓存
       复用 —— 那正是 A.8 说的「静默失效」：不报错，只是全是噪声。
       进了键的话，换模型自然全部未命中、自然重算，是构造上的安全。

    ⚠️ **batch 组成不进键。** 缓存存的就是 API 返回的那个向量，
       重放天然精确；把 batch 组成算进键既不现实也没必要。
    """
    raw = f"{embed.MODEL}\0{embed.DIM}\0{text}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def connect(path: Path | None = None) -> sqlite3.Connection:
    p = path or CACHE_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    # WAL：写入不阻塞读，且崩溃后不会留下半个事务 —— 续传靠这个
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS vectors (
            k          TEXT PRIMARY KEY,
            dim        INTEGER NOT NULL,
            vec        BLOB    NOT NULL,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.commit()
    return conn


def get_many(conn: sqlite3.Connection, texts: list[str]) -> dict[str, np.ndarray]:
    """批量查缓存，返回 {text: vec}。未命中的键不出现在结果里。"""
    if not texts:
        return {}

    by_key = {key_of(t): t for t in texts}
    out: dict[str, np.ndarray] = {}

    # SQLite 的参数上限默认 999，分片查
    keys = list(by_key)
    for i in range(0, len(keys), 900):
        chunk = keys[i:i + 900]
        ph = ",".join("?" * len(chunk))
        for k, dim, blob in conn.execute(
            f"SELECT k, dim, vec FROM vectors WHERE k IN ({ph})", chunk
        ):
            # ⚠️ np.frombuffer 返回**只读**数组（buffer 是 sqlite 给的 bytes）。
            #    .copy() 让它可写 —— 否则下游任何原地运算都会突然报
            #    "assignment destination is read-only"，而且是在很远的地方炸。
            v = np.frombuffer(blob, dtype=np.float32).copy()
            if v.shape[0] != dim:          # 防御：blob 长度与登记的维度对不上
                continue
            out[by_key[k]] = v
    return out


def put_many(conn: sqlite3.Connection, pairs: list[tuple[str, np.ndarray]]) -> None:
    """批量写入并提交。

    ⚠️ **每批都 commit。** 续传的粒度就是提交的粒度 —— 攒到最后一次性提交，
       中途挂掉就等于没跑。
    ⚠️ 存 float32 原值，**不降精度**：缓存是 API 返回值的存档（A.9），
       转 fp16 是写 Postgres 那一步的事。
    """
    if not pairs:
        return
    rows = [
        (key_of(t), int(v.shape[0]), np.ascontiguousarray(v, dtype=np.float32).tobytes())
        for t, v in pairs
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO vectors (k, dim, vec) VALUES (?, ?, ?)", rows
    )
    conn.commit()


def stats(conn: sqlite3.Connection) -> tuple[int, float]:
    """(条数, 占用 MB)。

    ⚠️ 文件路径从连接本身问出来，不读模块级的 CACHE_PATH ——
       connect() 支持自定义路径，硬读常量会在测试里报出错误的大小。
    """
    n = conn.execute("SELECT count(*) FROM vectors").fetchone()[0]
    size = 0.0
    for _, name, path in conn.execute("PRAGMA database_list"):
        if name == "main" and path:
            p = Path(path)
            if p.exists():
                size = p.stat().st_size / 1e6
    return n, size
