"""测试夹具。

## 为什么要旁路 /ask 的鉴权与配额

2026-08-24 起 `/api/ask` 与 `/api/find` 需要登录且计入 24 小时配额
（Kevin 定，见 server/main.py::_require_user）。但 test_router.py /
test_ask_intent.py / test_api.py 里那二十几条用例测的是**路由选对了没有**
—— 给它们套上真实注册登录会把两个无关的关注点焊死在一起：

  · 路由逻辑的用例会因为账号系统的改动而红，误导排查方向
  · 每条用例都要建用户、发 cookie，还会真的往 app_user / ask_log 写行
  · 配额只有 10 条，跑到第 11 条用例就会开始 429 —— **测试之间互相污染**，
    而且是按执行顺序偶发，最难查的那种

⇒ 默认旁路，让路由用例继续只测路由。真正要测鉴权/配额本身的用例
   打上 `@pytest.mark.real_auth`，本夹具就不插手。

⚠️ **旁路的是「是谁」和「扣没扣」，不是端点逻辑本身。** `_ask_impl` 一行
   没被 mock，路由分支照常真跑 —— 否则这些用例就成了自说自话。
"""

import pytest

from server import main as server_main
from src import quota

# 旁路模式下假装登录成的那个用户。⚠️ 这个 id 不需要真实存在于 app_user：
# 因为 quota.reserve 也一并被旁路了，不会有任何语句去 JOIN 它。
FAKE_USER_ID = 999_001
FAKE_ASK_ID = 1


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_auth: 不旁路鉴权与配额（测账号系统本身的用例用）",
    )


@pytest.fixture(autouse=True)
def bypass_ask_auth(request, monkeypatch):
    """默认让 /ask、/find 以「已登录且配额充足」的状态运行。

    ⚠️ autouse：不这样的话得给二十几条既有用例逐个加参数，而漏掉一条
       就是一个 401，且报错信息（assert 401 == 200）完全看不出根因。
    """
    if request.node.get_closest_marker("real_auth"):
        return

    monkeypatch.setattr(server_main, "_require_user", lambda _req: FAKE_USER_ID)
    monkeypatch.setattr(quota, "reserve",
                        lambda conn, uid, q, route=None: FAKE_ASK_ID)
    monkeypatch.setattr(quota, "set_route", lambda conn, ask_id, route: None)
    monkeypatch.setattr(quota, "refund", lambda conn, ask_id: None)
    monkeypatch.setattr(
        quota, "status",
        lambda conn, uid: {"used": 0, "limit": quota.DAILY_LIMIT,
                           "remaining": quota.DAILY_LIMIT, "reset_at": None},
    )
