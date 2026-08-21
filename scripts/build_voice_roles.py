"""灌 person + voice_role —— 声优与配役（回答「XXX 配过哪些角色」）。

跑法：
    uv run --group etl python scripts/build_voice_roles.py --dry-run   # 先看账
    uv run --group etl python scripts/build_voice_roles.py

前置：sql/009_voice_role.sql

⚠️ **职责边界**：本脚本只写 person / voice_role 两张表，以及 alias 里
   entity_type='person' 的行（source 一律 person_* 前缀）。
   绝不碰 anime_profile、不碰 alias 里的 subject/character 行 ——
   与 load_profiles.py（subject 行）、build_char_chunks.py（char_* 前缀）
   三者的 source 值永不相交，这是 C 节那条「各管各的列」在 alias 表上的落地。

⚠️ **不做截断。** 这是问答不是推荐特征：「花泽香菜配过哪些角色」只答主角役
   就是答错。role_type 存下来只用于**排序**（主角排前面），不用于过滤。
   📌 推荐侧的声优特征（staff_vec）是另一件事，那里才需要「仅主角 + df>=8」，
      且要改 sql/006 的列宽与 staffvec.DIM，风险与时机都不同，别混做一次。

📌 数据形状（2026-08-21 实测）：
     person.jsonlines             99,291 人，career 含 seiyu 的 11,768
     person-characters            落在候选集内 147,243 行，98.7% 是 seiyu
     ⇒ 8,215 个声优 · 145,306 条配役 · 覆盖 8,529 部作品（候选集的 74.5%）
   角色重要度来自 subject-characters.type（1=主角 2=配角 3=客串）。
   ⚠️ 它可能缺行 —— person-characters 有配对而 subject-characters 没有，
      此时 role_type 存 NULL，不要编一个默认值。
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bgm_tv_wiki
import orjson
from tqdm import tqdm

from src import db
from src.textproc import norm_name

DUMP = Path(__file__).resolve().parent.parent / "data" / "raw" / "dump"
WRITE_BATCH = 2000

UPSERT_PERSON = """
    INSERT INTO person (person_id, name, name_cn, career)
         VALUES (%s, %s, %s, %s)
    ON CONFLICT (person_id) DO UPDATE
       SET name = EXCLUDED.name, name_cn = EXCLUDED.name_cn,
           career = EXCLUDED.career
"""

UPSERT_ROLE = """
    INSERT INTO voice_role (person_id, subject_id, character_id, role_type)
         VALUES (%s, %s, %s, %s)
    ON CONFLICT (person_id, subject_id, character_id) DO UPDATE
       SET role_type = EXCLUDED.role_type
"""

INSERT_ALIAS = """
    INSERT INTO alias (name, norm_name, entity_type, person_id, source)
         VALUES (%s, %s, 'person', %s, %s)
    ON CONFLICT ON CONSTRAINT alias_uniq DO NOTHING
"""

# 重跑时先按这几个 source 删再插 —— 条目改名后旧别名不会留在库里。
# ⚠️ 与 load_profiles.py 的 DUMP_ALIAS_SOURCES 用同一套写法，但值不相交。
PERSON_ALIAS_SOURCES = ("person_name", "person_name_cn", "person_alias")


def person_names(rec: dict) -> tuple[list[tuple[str, str, int, str]], str]:
    """产出 ((name, norm_name, person_id, source) 列表, 中文名)，按 norm_name 去重。

    ⚠️ 用官方 parser 解析 infobox，不自己写 wiki 语法正则 —— 与
       load_profiles.infobox_aliases 同一条理由。
       ⚠️ 但字段不同：person 用的是 Infobox Crt 模板，中文名在「简体中文名」，
          而别名块里还嵌着「日文名」「罗马字」「昵称」这些子项，值可能为空
          （`[第二中文名|]`），必须过滤掉。
    """
    pid = rec["id"]
    rows: list[tuple[str, str, int, str]] = []
    seen: set[str] = set()

    def add(raw: str, source: str) -> None:
        raw = (raw or "").strip()
        if not raw:
            return
        key = norm_name(raw)
        if not key or key in seen:
            return
        seen.add(key)
        rows.append((raw, key, pid, source))

    add(rec.get("name") or "", "person_name")
    cn = ""
    ib = rec.get("infobox") or ""
    if ib:
        try:
            wiki = bgm_tv_wiki.parse(ib)
        except bgm_tv_wiki.WikiSyntaxError:
            wiki = None
        if wiki is not None:
            for field in wiki.fields:
                val = field.value
                if field.key == "简体中文名" and isinstance(val, str):
                    cn = val.strip()
                    add(cn, "person_name_cn")
                elif field.key == "别名":
                    if isinstance(val, str):
                        add(val, "person_alias")
                    elif isinstance(val, tuple):
                        for item in val:
                            if isinstance(item.value, str):
                                add(item.value, "person_alias")
    return rows, cn


def load_dump(candidates: set[int]):
    """读三个 dump 文件，返回 (persons, roles, aliases)。不碰数据库。"""
    print("[1/3] person.jsonlines …", flush=True)
    seiyu: dict[int, dict] = {}
    with (DUMP / "person.jsonlines").open("rb") as f:
        for line in f:
            d = orjson.loads(line)
            if "seiyu" in (d.get("career") or []):
                seiyu[d["id"]] = d

    print("[2/3] subject-characters.jsonlines（取角色重要度）…", flush=True)
    role_type: dict[tuple[int, int], int] = {}
    with (DUMP / "subject-characters.jsonlines").open("rb") as f:
        for line in f:
            d = orjson.loads(line)
            if d["subject_id"] in candidates:
                role_type[(d["subject_id"], d["character_id"])] = d.get("type")

    print("[3/3] person-characters.jsonlines …", flush=True)
    roles: list[tuple[int, int, int, int | None]] = []
    seen: set[tuple[int, int, int]] = set()
    with (DUMP / "person-characters.jsonlines").open("rb") as f:
        for line in f:
            d = orjson.loads(line)
            pid, sid, cid = d["person_id"], d["subject_id"], d["character_id"]
            if sid not in candidates or pid not in seiyu:
                continue
            key = (pid, sid, cid)
            if key in seen:          # dump 里同一配对可能出现多次（不同 type）
                continue
            seen.add(key)
            roles.append((pid, sid, cid, role_type.get((sid, cid))))

    used = {r[0] for r in roles}
    persons, aliases = [], []
    for pid in sorted(used):
        rec = seiyu[pid]
        rows, cn = person_names(rec)
        persons.append((pid, rec["name"], cn or None, rec.get("career") or []))
        aliases.extend(rows)
    return persons, roles, aliases


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="只算账不写库")
    args = ap.parse_args()

    # ── 第 1 段：读候选集（短连接）───────────────────────────
    with db.connect() as conn, conn.cursor() as cur:
        for t in ("person", "voice_role"):
            cur.execute("SELECT to_regclass(%s)", (t,))
            if cur.fetchone()[0] is None:
                raise SystemExit(f"✗ 表 {t} 不存在。先跑 sql/009_voice_role.sql")
        cur.execute("SELECT subject_id FROM anime_profile")
        candidates = {r[0] for r in cur.fetchall()}
    print(f"候选集 {len(candidates):,} 部")

    # ── 第 2 段：读 dump（几分钟，期间不持有连接）─────────────
    persons, roles, aliases = load_dump(candidates)
    by_type = Counter(r[3] for r in roles)
    per_work = Counter(r[1] for r in roles)
    print(f"\n声优 {len(persons):,} 人 · 配役 {len(roles):,} 条 · "
          f"覆盖作品 {len(per_work):,} 部")
    print(f"  角色重要度分布 {dict(by_type)}（None = subject-characters 里没有对应行）")
    print(f"  别名行 {len(aliases):,} 条 · 有中文名的声优 "
          f"{sum(1 for p in persons if p[2]):,} 人")
    if args.dry_run:
        return 0

    # ── 第 3 段：重新连库写入 ───────────────────────────────
    with db.connect() as conn, conn.cursor() as cur:
        cur.executemany(UPSERT_PERSON, persons)
        conn.commit()
        print(f"person：{len(persons):,} 行")

        bar = tqdm(total=len(roles), desc="写 voice_role", unit="行",
                   ascii=True, ncols=78)
        for i in range(0, len(roles), WRITE_BATCH):
            batch = roles[i:i + WRITE_BATCH]
            cur.executemany(UPSERT_ROLE, batch)
            conn.commit()
            bar.update(len(batch))
        bar.close()

        # ⚠️ 先删后插，范围严格限定在本脚本负责的 source 上。
        cur.execute(
            "DELETE FROM alias WHERE entity_type = 'person' AND source = ANY(%s)",
            (list(PERSON_ALIAS_SOURCES),))
        deleted = cur.rowcount
        for i in range(0, len(aliases), WRITE_BATCH):
            cur.executemany(INSERT_ALIAS, aliases[i:i + WRITE_BATCH])
        conn.commit()
        print(f"alias：删旧 {deleted:,} 行 · 插入 {len(aliases):,} 行")

        cur.execute("SELECT count(*) FROM voice_role")
        n_role = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM alias WHERE entity_type = 'person'")
        n_alias = cur.fetchone()[0]
        print(f"\n✓ voice_role {n_role:,} 行 · alias(person) {n_alias:,} 行")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
