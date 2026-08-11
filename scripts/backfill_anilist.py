"""动作 4b：补 AniList 独有的字段 —— anilist_id / idMal / 英文名 / popularity。

用法：
    PYTHONIOENCODING=utf-8 uv run python scripts/backfill_anilist.py --limit 200
    PYTHONIOENCODING=utf-8 uv run python scripts/backfill_anilist.py

staff / studios **不在这里**，它们走 backfill_staff.py 从 dump 取 ——
AniList 对国产只覆盖 7.1%、欧美 0.1%，而 dump 是 83.6% / 58.2%。
这里只做 AniList 真正独有的事：跨站 id（Phase 2 taste gap 的锚点）、
英文标题、全球热度。

ID 来源是 bangumi-data 的 `sites`（含 mal 与 aniList）。实测 200 部抽样：
年份 100% 吻合、idMal 100% 一致，只有 6% 是「OVA vs MOVIE」这类
分类口径差异，不是配错作品。

⚠️ 但它不是零错误的：鲁邦三世第一期(1971) 的 aniList id 指向了
   卡里奥斯特罗城剧场版(1979)。所以**年份差 >1 年一律不写**，
   计入 rejected 而不是硬猜 —— 文档第 4 节的原则是「宁可漏，不可错」，
   错误的实体合并会污染整个 taste gap 且事后极难发现。

⚠️ 只 UPDATE anilist_id / name_en / popularity / external_ids 四项。
   dump 派生列归 load_profiles.py，staff/studios 归 backfill_staff.py。
"""

import argparse
import json
import sys
import time
from pathlib import Path

import httpx
from psycopg.types.json import Jsonb
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db

API = "https://graphql.anilist.co"
ID_MAP = Path("data/interim/id_map.json")
RAW_DIR = Path("data/interim/anilist")      # 原始响应落**本地盘**，不进 Postgres
BATCH = 50                                  # AniList Page 的 perPage 上限
MAX_YEAR_DIFF = 1

QUERY = """
query ($ids: [Int]) {
  Page(perPage: 50) {
    media(id_in: $ids, type: ANIME) {
      id
      idMal
      popularity
      format
      title { romaji english }
      startDate { year }
    }
  }
}
"""

UPDATE_SQL = """
UPDATE anime_profile
SET anilist_id   = %(anilist_id)s,
    name_en      = COALESCE(%(name_en)s, name_en),
    popularity   = %(popularity)s,
    external_ids = external_ids || %(ext)s::jsonb,
    updated_at   = now()
WHERE subject_id = %(subject_id)s
"""


def fetch(client: httpx.Client, ids: list[int]) -> list[dict]:
    """取一批。429 按 Retry-After 退避，不写死速率 —— AniList 的实际
    限流长期低于其文档标称值，写死必然踩坑。"""
    for attempt in range(6):
        r = client.post(API, json={"query": QUERY, "variables": {"ids": ids}})
        if r.status_code == 429:
            wait = int(r.headers.get("Retry-After", 60)) + 1
            tqdm.write(f"  429 限流，等待 {wait}s")
            time.sleep(wait)
            continue
        if r.status_code >= 500:
            time.sleep(2 ** attempt)
            continue
        r.raise_for_status()
        body = r.json()
        if body.get("errors"):
            raise RuntimeError(body["errors"])
        return body["data"]["Page"]["media"]
    raise RuntimeError("重试 6 次仍失败")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="只处理 N 部（0 = 全部）")
    ap.add_argument("--sleep", type=float, default=2.0, help="每批之间的间隔秒数")
    args = ap.parse_args()

    id_map = json.loads(ID_MAP.read_text(encoding="utf-8"))
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    with db.connect() as conn:
        with conn.cursor() as cur:
            # anilist_id IS NULL 天然就是断点续传的游标，不用另存状态
            cur.execute("""SELECT subject_id, air_year FROM anime_profile
                           WHERE anilist_id IS NULL ORDER BY fav_done DESC""")
            todo = [(s, y) for s, y in cur.fetchall()
                    if id_map.get(str(s), {}).get("anilist")]
        if args.limit:
            todo = todo[:args.limit]
        print(f"待补 {len(todo)} 部，约 {(len(todo) + BATCH - 1) // BATCH} 次请求\n")

        ours = {s: y for s, y in todo}
        # ⚠️ 必须是一对多：34 组 AniList 条目对应我们 73 部作品 ——
        # Bangumi 把「前篇/后篇」「第一章~第四章」拆成独立 subject，
        # AniList 合并成一条（大和号 REBEL3199 四章 → 一个 id）。
        # 写成一对一字典的话，同组里除了最后一部之外都永远填不上，
        # 而且因为 anilist_id 一直是 NULL，每次续跑都会重新请求它们。
        al2sid: dict[int, list[int]] = {}
        for s, _ in todo:
            al2sid.setdefault(id_map[str(s)]["anilist"], []).append(s)
        written = rejected = missing = 0
        bad: list[str] = []

        with httpx.Client(timeout=60) as client:
            for i in tqdm(range(0, len(todo), BATCH), desc="拉取", unit="批"):
                chunk = todo[i:i + BATCH]
                # 去重：同组的「前篇/后篇」共用一个 anilist id，
                # 不去重会白白占掉本批 50 个名额里的位置
                ids = list(dict.fromkeys(id_map[str(s)]["anilist"] for s, _ in chunk))
                media = fetch(client, ids)
                (RAW_DIR / f"{ids[0]}.json").write_text(
                    json.dumps(media, ensure_ascii=False), encoding="utf-8")

                rows = []
                got = set()
                for d in media:
                    for sid in al2sid.get(d["id"], []):
                        if sid not in ours:
                            continue
                        got.add(sid)
                        year = (d["startDate"] or {}).get("year")
                        # 硬门槛：年份对不上就当没匹配到
                        if year is None or abs(year - ours[sid]) > MAX_YEAR_DIFF:
                            rejected += 1
                            bad.append(f"{sid} 我们={ours[sid]} AniList={year} "
                                       f"{(d['title']['romaji'] or '')[:34]}")
                            continue
                        ext = {"anilist": d["id"]}
                        if d.get("idMal"):
                            ext["mal"] = d["idMal"]
                        rows.append({
                            "subject_id": sid,
                            "anilist_id": d["id"],
                            "name_en": (d["title"] or {}).get("english"),
                            "popularity": d.get("popularity"),
                            "ext": Jsonb(ext),
                        })
                # got 可能含别的批次的作品（一对多），所以只数本批漏掉的
                missing += sum(1 for s, _ in chunk if s not in got)
                if rows:
                    with conn.cursor() as cur:
                        cur.executemany(UPDATE_SQL, rows)
                    conn.commit()
                    written += len(rows)
                time.sleep(args.sleep)

    print(f"\n写入 {written} 部")
    print(f"年份不符被拒 {rejected} 部（宁可漏不可错）")
    print(f"AniList 查无此 id {missing} 部")
    if bad:
        print("\n被拒样本（最多 15 条）:")
        for b in bad[:15]:
            print("   ", b)


if __name__ == "__main__":
    main()
