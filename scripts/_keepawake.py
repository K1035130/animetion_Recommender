"""跑长任务时别让 Windows 睡过去。

🚨 **这不是可选项，是长任务的必需品。** 2026-08-21 实测：角色页抓取跑到
   3 小时 19 分（1,502/4,955）被外部终止，日志里**零异常、fail 0** ——
   不是网络也不是代码，最可能就是系统休眠挂起了进程。
   而 `scripts/translate_corpus.py` 早就为它 8 小时的翻译任务加过同样的东西，
   我写 fetch_char_pages 时漏了。

⚠️ **退出时必须还原**（`ES_CONTINUOUS` 单独调一次），否则机器永远不睡。
   用 `with` 保证异常路径也还原。

📌 **来源**：从 translate_corpus.py 里提取。⚠️ 那边**暂未改用本模块** ——
   它的翻译任务已经跑完、代码已验证，为一个 20 行的工具去动它不划算。
   ⇒ 现在是两份实现，但**新脚本一律用这里的**；哪天 translate_corpus
   要重跑（季度更新会），顺手切过来即可。
"""

from __future__ import annotations

import ctypes
import sys

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """跑的时候别让系统睡。显示器仍可休眠 —— 只挡系统级睡眠。"""

    def __enter__(self):
        self.ok = False
        if sys.platform == "win32":
            try:
                ctypes.windll.kernel32.SetThreadExecutionState(
                    ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
                self.ok = True
            except (OSError, AttributeError) as e:
                print(f"  （防睡眠不可用：{e}）")
        print(f"防睡眠：{'已启用（显示器仍可休眠）' if self.ok else '未启用，请手动保持唤醒'}")
        return self

    def __exit__(self, *exc):
        if self.ok:
            ctypes.windll.kernel32.SetThreadExecutionState(ES_CONTINUOUS)
