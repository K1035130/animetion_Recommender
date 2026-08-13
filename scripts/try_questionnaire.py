"""交互式问卷 —— 自己答一遍，看推荐准不准。

用法（不需要设 PYTHONIOENCODING，脚本自己处理编码）：
    uv run python scripts/try_questionnaire.py
    uv run python scripts/try_questionnaire.py --n 40
    uv run python scripts/try_questionnaire.py --show
    uv run python scripts/try_questionnaire.py --reset

答案存 data/interim/my_answers.json，可以分多次答，重跑时自动跳过答过的。

⚠️ 这里用本地 JSON 而不是数据库表，是**刻意的**：
   第 2 节的架构铁律要求打分接口无状态（评分随请求传入），
   所以「评分存哪」是上层的事 —— 本地测试用文件、游客用 localStorage、
   注册用户用 user_rating 表，推荐链路一行都不用改。
   现在建 user 表也没有东西会往里写（认证排在第 6 周）。
"""

import argparse
import contextlib
import json
import sys
from pathlib import Path

import numpy as np


def _fix_console_encoding() -> None:
    """让中文在 Windows 控制台正常显示，不必每次设 PYTHONIOENCODING。

    ⚠️ 两件事都要做，缺一不可：
      · SetConsoleOutputCP(65001) —— 把控制台代码页切到 UTF-8。
        只 reconfigure 而不切代码页的话，Python 吐的是 UTF-8 字节、
        控制台按 cp936 解释，结果是乱码而不是报错，更难查。
      · reconfigure —— Python 的 stdout 默认按 cp936 编码，
        遇到 cp936 里没有的字符（日文假名、繁体字）会直接抛
        UnicodeEncodeError 而不是降级。
    这是本项目唯一需要交互输入的脚本，其余脚本仍走 PYTHONIOENCODING。
    """
    if sys.platform == "win32":
        import ctypes
        with contextlib.suppress(Exception):
            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
            ctypes.windll.kernel32.SetConsoleCP(65001)
    for stream in (sys.stdout, sys.stderr, sys.stdin):
        with contextlib.suppress(Exception):
            stream.reconfigure(encoding="utf-8", errors="replace")


_fix_console_encoding()

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import db
from src import questionnaire as Q
from src.recommend import Rating, build_catalog, score, season_window

ANSWERS = Path("data/interim/my_answers.json")

HELP = """
  1-10  看过，打分            w  没看过但想尝试
  n     没看过也不感兴趣       s  跳过这题
  d     答完了，直接看推荐      q  退出（已答的会保存）
"""


def load() -> dict[str, dict]:
    if ANSWERS.exists():
        return json.loads(ANSWERS.read_text(encoding="utf-8"))
    return {}


def save(ans: dict) -> None:
    ANSWERS.parent.mkdir(parents=True, exist_ok=True)
    ANSWERS.write_text(json.dumps(ans, ensure_ascii=False, indent=1),
                       encoding="utf-8")


META_KEY = "_meta"


def answers_only(ans: dict) -> dict:
    """剔除 _meta 这类非题目键。

    ⚠️ 资历存在同一个 JSON 里，但它不是评分 —— 不过滤的话
       int("_meta") 会直接抛异常。
    """
    return {k: v for k, v in ans.items() if not k.startswith("_")}


def ask_experience(ans: dict) -> str:
    """第一题：观众资历。决定出题的年份窗口，**不影响推荐范围**。"""
    saved = ans.get(META_KEY, {}).get("experience")
    if saved:
        return saved
    keys = list(Q.EXPERIENCE)
    print("先问一句 —— 你看番大概多久了？这决定出什么年代的题目。\n")
    for n, k in enumerate(keys, 1):
        print(f"  {n}. {Q.EXPERIENCE_LABEL[k]}")
    while True:
        try:
            raw = input("\n        > ").strip()
        except (EOFError, KeyboardInterrupt):
            return "veteran"
        if raw.isdigit() and 1 <= int(raw) <= len(keys):
            exp = keys[int(raw) - 1]
            ans.setdefault(META_KEY, {})["experience"] = exp
            save(ans)
            return exp
        print("        输 1-3。")


def to_ratings(ans: dict) -> list[Rating]:
    out = []
    for sid, a in answers_only(ans).items():
        got = Q.to_rating(a["choice"], a.get("score"))
        if got:
            out.append(Rating(int(sid), got[0], got[1], got[2]))
    return out


def show_recs(cat, ans: dict, mode: str,
              year_min: int | None = None, year_max: int | None = None,
              rank_by: str = "blend", alpha: float = 0.5) -> None:
    rs = to_ratings(ans)
    vals = answers_only(ans).values()
    seen = sum(1 for a in vals if a["choice"] == "seen")
    wish = sum(1 for a in vals if a["choice"] == "wish")
    psss = sum(1 for a in vals if a["choice"] == "pass")
    print(f"\n{'=' * 66}\n作答：看过 {seen} / 想尝试 {wish} / 不感兴趣 {psss}"
          f"   （共 {len(rs)} 条有效信号）")
    if not rs:
        print("还没有有效作答。")
        return
    out = score(cat, rs, mode=mode, year_min=year_min, year_max=year_max,
                rank_by=rank_by, blend_alpha=alpha, top_k=15)
    if not out:
        print("没有推荐结果 —— 作答太少，或年份区间里没有可推荐的作品。")
        return
    if year_min or year_max:
        rng = f"{year_min or '不限'} ~ {year_max or '不限'}"
    else:
        lo, hi = season_window()
        rng = {"all": "不限年份",
               "season": f"当季混合 {lo}~{hi}",
               "aired": f"当季已开播（{lo} 起）",
               "upcoming": "当季未开播",
               "classic": "经典回顾（2011 前）"}[mode]
    print(f"\n推荐（{rng}）:")
    for rank, rec in enumerate(out, 1):
        i = cat.index_of(rec.subject_id)
        tags = [cat.vocab[j] for j in np.argsort(-cat.mat[i])[:5]
                if cat.mat[i, j] > 0]
        star = " ★想看过" if str(rec.subject_id) in ans else ""
        sc = (f"{cat.bgm_score[i]:.1f}" if cat.bgm_score[i] > 0 else " - ")
        print(f"  {rank:>2}. 评分{sc} 匹配{rec.match:.3f}  "
              f"{rec.name[:28]:<30}{cat.year[i]}  {'/'.join(tags)}{star}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=30, help="出多少题")
    ap.add_argument("--mode", default="all",
                    choices=["all", "season", "aired", "upcoming", "classic"],
                    help="all=不限 season=当季混合(前后各一季) "
                         "aired=当季已开播 upcoming=当季未开播 classic=2011 年前")
    ap.add_argument("--from", dest="year_min", type=int, help="只推这年之后的")
    ap.add_argument("--to", dest="year_max", type=int, help="只推这年之前的")
    ap.add_argument("--rank", default="blend",
                    choices=["blend", "quality", "match"],
                    help="blend=匹配度与评分混合（默认） quality=纯评分 match=纯匹配度")
    ap.add_argument("--alpha", type=float, default=0.5,
                    help="blend 模式下匹配度的权重，0=只看评分 1=只看匹配度")
    ap.add_argument("--show", action="store_true", help="不答题，直接看推荐")
    ap.add_argument("--experience", choices=["new", "mid", "veteran"],
                    help="观众资历，省略则第一次运行时交互询问")
    ap.add_argument("--reset", action="store_true", help="清空已有答案")
    args = ap.parse_args()

    if args.reset:
        if ANSWERS.exists():
            # 先备份再删。答案是手工一条条答出来的，删掉就没了 ——
            # 实际发生过一次：测试时顺手 --reset，30 条作答全丢。
            bak = ANSWERS.with_suffix(".bak.json")
            bak.write_text(ANSWERS.read_text(encoding="utf-8"), encoding="utf-8")
            ANSWERS.unlink()
            print(f"已清空答案（旧的备份在 {bak}）")
        else:
            print("本来就没有答案")
        return

    ans = load()
    exp = args.experience or (ans.get(META_KEY, {}).get("experience"))
    if not args.show and not exp:
        exp = ask_experience(ans)
    exp = exp or "veteran"
    with db.connect() as conn:
        items = Q.select_items(conn, args.n, experience=exp)
        cat = build_catalog(conn)

    if args.show:
        show_recs(cat, ans, args.mode, args.year_min, args.year_max, args.rank, args.alpha)
        return

    todo = [it for it in items if str(it.subject_id) not in ans]
    print(f"\n资历：{Q.EXPERIENCE_LABEL[exp]}")
    print(f"共 {len(items)} 题，已答 {len(items) - len(todo)}，剩 {len(todo)}")
    print(HELP)

    for k, it in enumerate(todo, 1):
        i = cat.index_of(it.subject_id)
        tags = "/".join(cat.vocab[j] for j in np.argsort(-cat.mat[i])[:5]
                        if cat.mat[i, j] > 0) if i is not None else ""
        folded = "（系列第一部）" if it.replaced_from else ""
        print(f"\n[{k}/{len(todo)}] {it.name}  {it.year}  "
              f"{it.form}  {it.done} 人看过{folded}")
        print(f"          {tags}")
        try:
            raw = input("        > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n中断，已保存。")
            break
        if raw == "q":
            break
        if raw == "d":
            break
        if raw in ("", "s"):
            continue
        if raw == "w":
            ans[str(it.subject_id)] = {"choice": "wish", "name": it.name}
        elif raw == "n":
            ans[str(it.subject_id)] = {"choice": "pass", "name": it.name}
        elif raw.isdigit() and 1 <= int(raw) <= 10:
            ans[str(it.subject_id)] = {"choice": "seen", "score": int(raw),
                                       "name": it.name}
        else:
            print("        没听懂，跳过。" + HELP)
            continue
        save(ans)

    save(ans)
    show_recs(cat, ans, args.mode, args.year_min, args.year_max, args.rank, args.alpha)
    print(f"\n答案存于 {ANSWERS}")
    print("再跑一次可以接着答；--show 只看推荐；--reset 清空。")


if __name__ == "__main__":
    main()
