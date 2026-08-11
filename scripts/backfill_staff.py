"""动作 4a：用 dump 的 subject-persons + person 回填 studios / staff。

用法：
    PYTHONIOENCODING=utf-8 uv run python scripts/backfill_staff.py --dry-run
    PYTHONIOENCODING=utf-8 uv run python scripts/backfill_staff.py

为什么不用 AniList（偏离 CLAUDE.md 动作 4 的原始设计）：
    实测 bangumi-data 能给出 AniList id 的只有 56.4%，且缺口不是随机的 ——
    国产 7.1%、欧美 0.1%、R18 3.0%、OVA 25%。而 dump 自带的 person 数据
    覆盖 84.5%（制作公司）/ 93.3%（主要 staff），国产 83.6%、欧美 58.2%。
    「喜欢上海美术电影制片厂」是真实口味维度，只走 AniList 会把它整个抹掉。
    AniList 改为只补它独有的东西（anilist_id / idMal / 英文名 / popularity），
    见 backfill_anilist.py。

⚠️ 只 UPDATE studios / staff 两列。dump 派生列由 load_profiles.py 负责，
   AniList 列由 backfill_anilist.py 负责 —— 三个脚本各管各的列，
   否则谁后跑谁赢，是最难查的那种 bug。
"""

import argparse
import sys
from collections import defaultdict
from pathlib import Path

import orjson
from psycopg.types.json import Jsonb
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db

DUMP = Path("data/raw/dump")

# position 角色码。dump 里没有码表，下面是按已知作品实测反推出来的：
#   EVA → 2:庵野秀明  8:貞本義行  6:鷺巣詩郎     轻音 → 2:山田尚子  3:花田十輝
# 只保留用户真正会关心的主要岗位。作画监督(15)、原画(20, 全库 21 万条)
# 这类量大且不构成口味特征，一律不取。
ROLES = {
    2: "导演",
    1: "原作",
    3: "脚本",
    8: "人物设定",
    6: "音乐",
}
ROLE_ORDER = ["导演", "原作", "脚本", "人物设定", "音乐"]

# 制作公司的回退链：67=动画制作 优先；没有才退到 63=出品 / 42=製作。
# 只在前者为空时回退 —— 日本 TV 番的 42 上挂的是电视台和出版社
# （TBS、ムービック），无条件取会污染。
# 回退救回的是：上海美术电影制片厂(63)、里番厂牌如 Queen Bee(42)、
# 欧美的 20th Century Fox Television(42)。
STUDIO_POSITIONS = (67, 63, 42)

MAX_PER_ROLE = 3      # EVA 挂了 6 个脚本，留 3 个够用且能控住 jsonb 体积
MAX_STUDIOS = 4

UPDATE_SQL = """
UPDATE anime_profile
SET studios = %(studios)s, staff = %(staff)s, updated_at = now()
WHERE subject_id = %(subject_id)s
"""


def load_persons() -> dict[int, str]:
    """person_id → 姓名。9.9 万条，直接进内存。"""
    out: dict[int, str] = {}
    with open(DUMP / "person.jsonlines", "rb") as f:
        for line in tqdm(f, desc="读 person", unit="条"):
            r = orjson.loads(line)
            name = (r.get("name") or "").strip()
            if name:
                out[int(r["id"])] = name
    return out


def collect(wanted: set[int], persons: dict[int, str]):
    """扫 subject-persons，按作品聚合出 studios / staff。"""
    studio_by_pos: dict[int, dict[int, list[str]]] = defaultdict(lambda: defaultdict(list))
    staff_raw: dict[int, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))

    with open(DUMP / "subject-persons.jsonlines", "rb") as f:
        for line in tqdm(f, desc="读 subject-persons", unit="条"):
            r = orjson.loads(line)
            sid = r["subject_id"]
            if sid not in wanted:
                continue
            name = persons.get(r["person_id"])
            if not name:
                continue
            pos = r["position"]
            if pos in STUDIO_POSITIONS:
                studio_by_pos[sid][pos].append(name)
            elif pos in ROLES:
                staff_raw[sid][ROLES[pos]].append(name)

    rows = []
    for sid in wanted:
        studios: list[str] = []
        for pos in STUDIO_POSITIONS:          # 回退链：67 → 63 → 42
            if studio_by_pos[sid].get(pos):
                studios = studio_by_pos[sid][pos]
                break
        studios = list(dict.fromkeys(studios))[:MAX_STUDIOS]   # 去重且保序

        staff = []
        for role in ROLE_ORDER:
            for name in list(dict.fromkeys(staff_raw[sid].get(role, [])))[:MAX_PER_ROLE]:
                staff.append({"role": role, "name": name})

        if studios or staff:
            rows.append({
                "subject_id": sid,
                "studios": studios or None,
                "staff": Jsonb(staff) if staff else None,
            })
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    with db.connect() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT subject_id FROM anime_profile")
            wanted = {r[0] for r in cur.fetchall()}
        print(f"库里 {len(wanted)} 部作品\n")

        persons = load_persons()
        print(f"person 表 {len(persons)} 条")
        rows = collect(wanted, persons)

        n_studio = sum(1 for r in rows if r["studios"])
        n_staff = sum(1 for r in rows if r["staff"])
        print(f"\n可回填 {len(rows)} 部："
              f"有制作公司 {n_studio} ({n_studio / len(wanted) * 100:.1f}%)，"
              f"有 staff {n_staff} ({n_staff / len(wanted) * 100:.1f}%)")

        if args.dry_run:
            print("\n--dry-run，不写库。抽样：")
            for r in rows[:3]:
                print(" ", r["subject_id"], r["studios"],
                      r["staff"].obj[:4] if r["staff"] else None)
            return

        with conn.cursor() as cur:
            for i in tqdm(range(0, len(rows), 500), desc="写库", unit="批"):
                cur.executemany(UPDATE_SQL, rows[i:i + 500])
                conn.commit()
    print("完成")


if __name__ == "__main__":
    main()
