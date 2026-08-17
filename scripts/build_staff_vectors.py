"""回填 anime_profile.staff_vec，并产出 data/interim/staff_vocab.json。

⚠️ **职责边界**（CLAUDE.md「脚本职责与启动顺序」）：本脚本只写 `staff_vec` 一列
   和 `build_meta` 的 'staff_vec' 一行，绝不碰 studios/staff 本身
   （那两列归 scripts/backfill_staff.py）。幂等，可重跑。

前置：sql/006_staff_vec.sql、scripts/backfill_staff.py 已跑（studios/staff 有数据）。

⚠️ **什么时候必须重跑：**
   · 改了 src/staffvec.py 的 MIN_DF / 特征定义 / 加权方式
   · 重跑过 backfill_staff.py
   · 第 6 周季度同步加入新作品（idf 依赖全库词频，**只能全量重算**）

跑法：
    uv run python scripts/build_staff_vectors.py
    uv run python scripts/build_staff_vectors.py --report   # 只看统计不写库

⚠️ **词表文件要提交进 git。** 它定义了库里每个向量的每一维代表谁 ——
   与 tag_vocab.json 同一条理由。丢了它，库里的向量就成了无法解释的数字。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from psycopg.types.json import Json

from src import db, staffvec


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--report", action="store_true", help="只打印统计，不写库")
    args = ap.parse_args()

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT format_type(atttypid, atttypmod) FROM pg_attribute
                 WHERE attrelid = 'anime_profile'::regclass AND attname = 'staff_vec'
            """)
            row = cur.fetchone()
        if not row:
            print("✗ 缺 staff_vec 列。先跑 sql/006_staff_vec.sql", file=sys.stderr)
            return 1
        col_type = row[0]

        with conn.cursor() as cur:
            cur.execute("SELECT subject_id, studios, staff FROM anime_profile "
                        "ORDER BY subject_id")
            rows = cur.fetchall()

        vocab, idf = staffvec.build_vocab(rows)
        print(f"词表 {len(vocab)} 维（MIN_DF={staffvec.MIN_DF}）· 指纹 "
              f"{staffvec.fingerprint(vocab)}")

        # ⚠️ 维度必须与列宽一致。对不上就停 —— sparsevec 会因维度不符报错，
        #    但更危险的是「恰好能塞进去但含义错位」，所以在这里就拦掉。
        if len(vocab) != staffvec.DIM:
            print(f"✗ 词表 {len(vocab)} 维与 staffvec.DIM={staffvec.DIM} 不符。"
                  f"数据变了就得同时改 DIM 和 sql/006 的 sparsevec(N)，"
                  f"并整列重灌（当前列是 {col_type}）", file=sys.stderr)
            return 1

        index = {f: i for i, f in enumerate(vocab)}
        payload, empty = [], 0
        for sid, studios, staff in rows:
            w = staffvec.weights_of(studios, staff, index, idf)
            if not w:
                empty += 1
                continue
            payload.append((staffvec.to_sparsevec(w), sid))

        nz = sum(len(staffvec.weights_of(s, f, index, idf)) for _, s, f in rows)
        print(f"有特征 {len(payload)} / {len(rows)} 部（{empty} 部无特征存 NULL）"
              f"· 平均非零 {nz / max(len(payload), 1):.1f} 维")

        if args.report:
            print("\n--report：未写库")
            return 0

        staffvec.save_vocab(vocab, idf)
        print(f"词表已写入 {staffvec.VOCAB_PATH}（⚠️ 记得提交进 git）")

        with conn.cursor() as cur:
            # 先整列清空：词表口径变化后，上一轮有向量而这轮无特征的作品
            # 会留下陈旧向量，而打分不会报错、只会静默失准。
            # ⚠️ updated_at 必须设：src/recommend.py 的 npz 缓存键靠 max(updated_at)
            #    发现"就地改值"。不设的话重跑本脚本后 numpy 路径仍读旧矩阵，
            #    而且不报错 —— 2026-08-17 已经这么栽过一次（test_parity 12 项红）。
            cur.execute("UPDATE anime_profile SET staff_vec = NULL, updated_at = now() "
                        "WHERE staff_vec IS NOT NULL")
            for i in range(0, len(payload), 500):
                cur.executemany(
                    "UPDATE anime_profile SET staff_vec = %s, updated_at = now() "
                    "WHERE subject_id = %s",
                    payload[i:i + 500])
            cur.execute("""
                INSERT INTO build_meta (key, value, updated_at)
                     VALUES ('staff_vec', %s, now())
                ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = now()
            """, (Json({
                "min_df": staffvec.MIN_DF, "dim": staffvec.DIM,
                "fingerprint": staffvec.fingerprint(vocab),
                "rows": len(payload), "vocab_size": len(vocab),
            }),))
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FILTER (WHERE staff_vec IS NOT NULL), count(*) "
                        "FROM anime_profile")
            got, total = cur.fetchone()
        print(f"\n✓ staff_vec {got} / {total} 非空")
        if got != len(payload):
            print(f"✗ 写入 {len(payload)} 行但库里只有 {got} 行", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
