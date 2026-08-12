"""系列关系：续作 → 第一部 的映射。

问卷选题和推荐结果都要用它，所以单独成模块，不要在两处各写一份加载逻辑。
产物由 scripts/build_series_map.py 生成，不入 git（data/interim/* 被忽略）。

判据与 relation_type 码表见 scripts/build_series_map.py 的模块注释。
"""

import json
from pathlib import Path

SERIES_MAP = Path("data/interim/series_root.json")


def load(required: bool = True) -> dict[int, int]:
    """续作 subject_id → 系列根 subject_id。不在映射里的作品本身就是根。"""
    if not SERIES_MAP.exists():
        if required:
            raise FileNotFoundError(
                f"缺少 {SERIES_MAP}，先跑 scripts/build_series_map.py")
        return {}
    return {int(k): int(v) for k, v in
            json.loads(SERIES_MAP.read_text(encoding="utf-8")).items()}
