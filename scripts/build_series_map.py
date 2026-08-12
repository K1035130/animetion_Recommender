"""产出「续作 → 系列第一部」的映射，供问卷选题使用。

用法：
    PYTHONIOENCODING=utf-8 uv run python scripts/build_series_map.py

问题：问卷若抽到系列续作，用户没看过前作就只能选「没看过」，等于白问一题。
而纯热度选题恰恰会优先抽到续作 —— 实测 TV+WEB 非 nsfw 的 6,496 部候选池里
30.3% 是直接续作，热度前列几乎全是（CLANNAD AFTER STORY、轻音少女 第二季、
进击的巨人 第三季 Part.2、辉夜二三期）。

判据（实测定稿，码表由已知系列反推）：
    subject-relations 里 relation_type=2（前传）指向的作品，
    且该前传播出**不晚于**本作 → 本作是续作，用它的根节点替代。

⚠️ 不能简单按「有更早的关联作」判定 —— 那会误杀平行/外传作品。
   Fate/Zero 与 Fate/stay night 是平行关系、超电磁炮 与 魔法禁书目录 是
   主线/番外（rt=12），都可独立观看，必须保留。

⚠️ 「播出不晚于」这条补丁不能省：rt=2 是**故事顺序**不是播出顺序。
   少了它会误杀 94 部先播、后来才出前传的独立作品 ——
   Fate/stay night(2006) 的前传 Fate/Zero 是 2011 年的。

relation_type 码表：1=改编 2=前传 3=续集(故事序) 4/5=总集篇
6/12=番外篇/主线故事 8/9=相同/不同世界观 11=衍生 7=角色出演
"""

import json
import sys
from collections import defaultdict
from pathlib import Path

import orjson
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db

RELATIONS = Path("data/raw/dump/subject-relations.jsonlines")
OUT = Path("data/interim/series_root.json")
PREQUEL = 2
# 防环的安全上界。真正保证终止的是 root_of() 里的 seen 集合（图是有限的），
# 这个常数只是兜底。
# ⚠️ 原先设成 12，理由是「柯南/海贼这类超长系列也远不到这个深度」—— 错了。
#    哆啦A梦剧场版是**逐年链式**关联（1993→1992→…→1980），实测最长链深 40，
#    101 条链超过 12 跳。走到 12 跳就停，链条中段的节点会被当成「根」，
#    同一系列于是分裂成两组，问卷和推荐里各出现一次。自检脚本
#    「根节点自身不再是续作」抓到了这个。
MAX_HOPS = 100


def main() -> None:
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT subject_id, air_year, COALESCE(name_cn, name) "
                    "FROM anime_profile")
        rows = cur.fetchall()
    year = {r[0]: r[1] for r in rows}
    name = {r[0]: r[2] for r in rows}
    print(f"库内 {len(year)} 部")

    # 本作 → 它的前传们（只保留库内、且播出不晚于本作的）
    prequels: dict[int, list[int]] = defaultdict(list)
    with open(RELATIONS, "rb") as f:
        for line in tqdm(f, desc="读 subject-relations", unit="条"):
            r = orjson.loads(line)
            if r.get("relation_type") != PREQUEL:
                continue
            s, t = r.get("subject_id"), r.get("related_subject_id")
            if s in year and t in year and year[t] <= year[s]:
                prequels[s].append(t)

    print(f"存在前传的作品: {len(prequels)}")

    def root_of(sid: int) -> int:
        """沿前传链上溯到根。多个前传时取播出最早的那条。"""
        seen = {sid}
        cur_id = sid
        for _ in range(MAX_HOPS):
            ps = [p for p in prequels.get(cur_id, []) if p not in seen]
            if not ps:
                break
            cur_id = min(ps, key=lambda p: (year[p], p))
            seen.add(cur_id)
        return cur_id

    mapping = {s: root_of(s) for s in prequels}
    mapping = {s: r for s, r in mapping.items() if r != s}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")

    depth: dict[int, int] = defaultdict(int)
    for s, r in mapping.items():
        depth[r] += 1
    print(f"\n写出 {len(mapping)} 条续作→根 映射 → {OUT}")
    print(f"涉及 {len(depth)} 个系列根节点\n")
    print("续作最多的系列（根节点 ← 续作数）:")
    for r, n in sorted(depth.items(), key=lambda x: -x[1])[:8]:
        print(f"   {name[r][:34]:<36}← {n} 部")
    print("\n抽样:")
    for s in list(mapping)[:8]:
        print(f"   {name[s][:32]:<34}→ {name[mapping[s]][:30]}")


if __name__ == "__main__":
    main()
