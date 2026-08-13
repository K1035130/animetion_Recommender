"""系列关系：续作 → 第一部 的映射。

问卷选题和推荐结果都要用它，所以单独成模块，不要在两处各写一份加载逻辑。
产物由 scripts/build_series_map.py 生成，不入 git（data/interim/* 被忽略）。

判据与 relation_type 码表见 scripts/build_series_map.py 的模块注释。
"""

import json
from pathlib import Path

# ⚠️ 必须相对 __file__ 而不是 CWD。原先写的是 Path("data/interim/...")，
#    而 recommend.score() 走的是 load(required=False) —— 工作目录不对时
#    不报错，只是续作折叠**静默关闭**（实测切到 C:/ 后返回 0 条映射），
#    表现是「推荐列表里突然全是第二季第三季」，日志里一个字都没有。
#    与 src/textproc.py 的 VOCAB_PATH 保持同一写法。
#
# 📌 线上（recommend_sql）已经不读这个文件了 —— 系列关系走
#    anime_profile.series_root 列。这里只服务于第 5 周评测用的内存路径。
SERIES_MAP = (Path(__file__).resolve().parent.parent
              / "data" / "interim" / "series_root.json")


def load(required: bool = True) -> dict[int, int]:
    """续作 subject_id → 系列根 subject_id。不在映射里的作品本身就是根。"""
    if not SERIES_MAP.exists():
        if required:
            raise FileNotFoundError(
                f"缺少 {SERIES_MAP}，先跑 scripts/build_series_map.py")
        return {}
    return {int(k): int(v) for k, v in
            json.loads(SERIES_MAP.read_text(encoding="utf-8")).items()}
