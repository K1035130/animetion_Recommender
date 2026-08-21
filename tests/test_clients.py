"""HTTP 客户端收尾（src/clients.py）。

🚨 这组测试锁的是一个**真实发生过的漂移**：`llm.close_client()` 与
   `rerank.close_client()` 有定义却从来没有调用方，四个 eval 脚本都只关了
   Neon 连接，httpx 连接池一直挂着（2026-08-20 自检发现）。
"""

import re
from pathlib import Path

from src import clients, embed, llm, rerank, translate

SRC = Path(__file__).resolve().parent.parent / "src"


def _live() -> dict[str, bool]:
    return {m.__name__: m._client is not None for m in clients._MODULES}


def test_close_all_closes_every_live_client():
    embed._get_client()
    rerank._get_client()
    assert any(_live().values()), "前置：至少要有一个 client 活着"
    clients.close_all()
    assert not any(_live().values())


def test_close_all_is_idempotent():
    """脚本会在 finally 里调它，而 finally 可能在已经关过之后再执行一次。"""
    clients.close_all()
    clients.close_all()
    assert not any(_live().values())


def test_close_all_never_raises_and_keeps_going():
    """🚨 收尾抛异常会**盖住 finally 之前的真异常**，把"任务为什么失败"
    换成"收尾为什么失败"——是最难查的一类。所以逐个吞掉并继续。"""
    class Boom:
        __name__ = "boom"

        @staticmethod
        def close_client():
            raise RuntimeError("模拟关闭失败")

    embed._get_client()
    orig = clients._MODULES
    clients._MODULES = (Boom, embed)
    try:
        clients.close_all()                      # 不该抛
        assert embed._client is None, "前一个抛异常不能让后一个被跳过"
    finally:
        clients._MODULES = orig


def test_every_module_with_a_client_is_registered():
    """🚨 **防漂移：这条才是本次 bug 的根因。**

    `close_client` 分散定义在各模块里，而"该关谁"没有唯一定义处 ——
    新增一个带 client 的模块时，得指望作者去翻所有脚本补一行。
    ⇒ 这条测试把「新增模块要进 close_all()」变成一个会亮红灯的约束，
      与 CLAUDE.md 那条「新增影响输出的配置时要问：它进指纹了吗」同构。
    """
    defines = {p.stem for p in SRC.glob("*.py")
               if re.search(r"^def close_client\b", p.read_text(encoding="utf-8"),
                            re.MULTILINE)}
    registered = {m.__name__.rsplit(".", 1)[-1] for m in clients._MODULES}
    missing = defines - registered
    assert not missing, (
        f"这些模块定义了 close_client 却没进 src/clients.py 的 _MODULES：{missing}")


def test_registered_modules_all_have_close_client():
    """反向：_MODULES 里的每一项都必须真有 close_client（防拼错/改名）。"""
    for m in (embed, llm, rerank, translate):
        assert callable(getattr(m, "close_client", None)), m.__name__
