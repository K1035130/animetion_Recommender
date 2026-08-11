"""生成 bangumi_id → {mal, anilist} 映射表，供 backfill_anilist.py 使用。

用法：
    PYTHONIOENCODING=utf-8 uv run python scripts/build_id_map.py          # 用已有的本地副本
    PYTHONIOENCODING=utf-8 uv run python scripts/build_id_map.py --download # 重新下载

产物 data/interim/id_map.json 不入 git（.gitignore 排除了 data/interim/*），
所以换台机器必须先跑这个脚本，否则 backfill_anilist.py 会直接报错。

数据源是 bangumi-data，它的 `sites` 数组里直接带 mal 和 aniList 的 id：
实测 8,593 条目中 mal 覆盖 94.6%、aniList 93.9%，**不需要**经 AniDB 中转。
对我们 11,453 部候选集的覆盖是 56.4%，缺口集中在国产/欧美/R18/OVA ——
所以 staff 和 studios 走 dump（backfill_staff.py），不依赖这份映射。

⚠️ 映射本身不是零错误的：鲁邦三世第一期(1971) 的 aniList id 指向
   卡里奥斯特罗城剧场版(1979)。200 部抽样里年份 100% 吻合、idMal 100%
   一致，但这类错配确实存在，所以 backfill_anilist.py 还有一道年份门槛。
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

import httpx
import orjson

# jsDelivr 比 raw.githubusercontent 稳定，且带 CDN
SOURCE = "https://cdn.jsdelivr.net/npm/bangumi-data@latest/dist/data.json"
LOCAL = Path("data/raw/bangumi-data.json")
OUT = Path("data/interim/id_map.json")


def download() -> None:
    LOCAL.parent.mkdir(parents=True, exist_ok=True)
    print(f"下载 {SOURCE}")
    with httpx.stream("GET", SOURCE, follow_redirects=True, timeout=180) as r:
        r.raise_for_status()
        with open(LOCAL, "wb") as f:
            f.writelines(r.iter_bytes())
    print(f"已存 {LOCAL}（{LOCAL.stat().st_size / 1048576:.1f} MB）")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--download", action="store_true", help="重新下载 bangumi-data")
    args = ap.parse_args()

    if args.download or not LOCAL.exists():
        download()

    bd = orjson.loads(LOCAL.read_bytes())
    stats: Counter = Counter()
    id_map: dict[int, dict[str, int]] = {}

    for item in bd["items"]:
        # ⚠️ 28 条爱奇艺站点项用 url 代替 id，直接 s['id'] 会 KeyError
        sites = {s["site"]: s["id"] for s in (item.get("sites") or []) if "id" in s}
        bgm = sites.get("bangumi")
        if not bgm:
            stats["无 bangumi id"] += 1
            continue
        ext: dict[str, int] = {}
        if sites.get("aniList"):
            ext["anilist"] = int(sites["aniList"])
        if sites.get("mal"):
            ext["mal"] = int(sites["mal"])
        if ext:
            id_map[int(bgm)] = ext
            stats["有外部 id"] += 1
        else:
            stats["只有 bangumi id"] += 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(id_map, ensure_ascii=False), encoding="utf-8")

    both = [v for v in id_map.values() if "anilist" in v and "mal" in v]
    same = sum(1 for v in both if v["anilist"] == v["mal"])
    print(f"\nbangumi-data 条目 {len(bd['items'])}")
    for k, v in stats.most_common():
        print(f"   {k:<16}{v}")
    print(f"\n写出 {len(id_map)} 条映射 → {OUT}")
    # AniList 早期数据库是从 MAL 导入的，老条目沿用同一 id，新条目才分叉。
    # 这个比例接近 50% 属正常；若某天变成 ~100%，说明上游把 mal id
    # 抄进了 aniList 字段，那时这份映射就不能用了。
    print(f"   同时有两个 id 的 {len(both)} 条，其中 anilist == mal "
          f"{same} ({same / len(both) * 100:.1f}%)")
    if same / len(both) > 0.95:
        print("   ⚠️ 比例异常高，怀疑上游把 mal id 复制进了 aniList 字段，请人工核对")
        sys.exit(1)


if __name__ == "__main__":
    main()
