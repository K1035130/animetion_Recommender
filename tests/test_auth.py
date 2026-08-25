"""账号系统：密码/JWT 纯函数 + 端点鉴权 + 24 小时配额。

⚠️ **这个文件里的端点用例必须打 `@pytest.mark.real_auth`**，
   否则 tests/conftest.py 的 autouse 夹具会把鉴权与配额旁路掉 ——
   那正是本文件要测的东西，被旁路了就成了自说自话。

⚠️ 端点用例会**真的往 app_user / user_rating / ask_log 写行**，所以每个
   用例自己建号自己删（ON DELETE CASCADE 会带走评分与用量）。
   邮箱一律用 `@pytest.local` 结尾，与真实账号不可能撞。
"""

import datetime
import uuid

import pytest
from fastapi.testclient import TestClient

from server.main import API, app
from src import auth, db, quota

# ============================================================
# 纯函数：密码与 JWT（不碰库，不需要 real_auth）
# ============================================================

def test_secret_missing_raises(monkeypatch):
    """🚨 密钥缺失必须硬失败。有默认值的话，忘配环境变量的部署会正常启动、
    正常签发 token，而那些 token 用的是公开在源码里的密钥 —— 不报错、
    看起来完全正常，是最坏的一类故障。"""
    monkeypatch.delenv("AUTH_SECRET", raising=False)
    monkeypatch.setattr(auth, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="AUTH_SECRET"):
        auth.secret()


def test_secret_too_short_raises(monkeypatch):
    monkeypatch.setenv("AUTH_SECRET", "short")
    monkeypatch.setattr(auth, "load_dotenv", lambda *a, **k: None)
    with pytest.raises(RuntimeError, match="太短"):
        auth.secret()


def test_password_roundtrip():
    h = auth.hash_password("correct-horse-battery")
    assert h != "correct-horse-battery"          # 别存明文
    assert h.startswith("$argon2id$")            # 不是 bcrypt / sha256
    assert auth.verify_password(h, "correct-horse-battery")
    assert not auth.verify_password(h, "wrong-password")


def test_password_hash_is_salted():
    """同一个密码两次哈希必须不同 —— 相同就说明没加盐，彩虹表直接打穿。"""
    assert auth.hash_password("same-password") != auth.hash_password("same-password")


def test_verify_survives_garbage_hash():
    """库里那条哈希坏了也只能返回 False，不能抛异常把登录接口炸成 500。"""
    assert not auth.verify_password("not-a-valid-hash", "x")
    assert not auth.verify_password("", "x")


@pytest.mark.parametrize("pw", ["short", "x" * 129])
def test_password_length_enforced(pw):
    with pytest.raises(auth.AuthError):
        auth.hash_password(pw)


def test_token_roundtrip():
    assert auth.read_token(auth.make_token(12345)) == 12345


def test_token_tampered_rejected():
    t = auth.make_token(1)
    head, payload, sig = t.split(".")
    with pytest.raises(auth.AuthError):
        auth.read_token(f"{head}.{payload}.{sig[:-2]}xx")


def test_token_expired_rejected():
    past = datetime.datetime.now(datetime.UTC) - auth.TOKEN_TTL - datetime.timedelta(hours=1)
    with pytest.raises(auth.AuthError):
        auth.read_token(auth.make_token(1, now=past))


def test_token_alg_none_rejected():
    """🚨 JWT 最经典的坑：把 header 的 alg 改成 none 就绕过签名。
    read_token 显式传 algorithms=[HS256] 挡住它 —— 这条测试是为了防止
    将来有人为了「兼容」把那个参数去掉。"""
    import base64
    import json

    def b64(d):
        return base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()

    forged = (f"{b64({'alg': 'none', 'typ': 'JWT'})}."
              f"{b64({'sub': '1', 'exp': 9999999999})}.")
    with pytest.raises(auth.AuthError):
        auth.read_token(forged)


@pytest.mark.parametrize("raw,expect", [
    ("  Kevin ", "kevin"),                  # 首尾空白 + 大小写
    ("kevin", "kevin"),
    ("MiXeDcAsE", "mixedcase"),
    ("小明", "小明"),                        # CJK 不受大小写折叠影响
])
def test_normalize_username(raw, expect):
    """不归一化的话 UNIQUE 约束形同虚设 —— 它拦的是字节相同，不是身份相同。"""
    assert auth.normalize_username(raw) == expect


def test_normalize_username_folds_fullwidth():
    """🚨 全角必须折成半角。不折的话注册一个全角同名账号就能在界面上
    冒充别人，而唯一约束完全看不出问题 —— 静默的身份混淆。"""
    assert auth.normalize_username("Ｋｅｖｉｎ") == auth.normalize_username("kevin")


def test_normalize_username_folds_unicode_composition():
    """`é` 有组合与预组合两种写法，字节不同但看起来一模一样。
    NFKC 把它们统一 —— 否则同样是一个可冒充的缺口。"""
    assert auth.normalize_username("café") == auth.normalize_username("café")


@pytest.mark.parametrize("name,ok", [
    ("kevin", True), ("小明", True), ("a_b-c", True), ("用户2024", True),
    ("k", False),                       # 太短
    ("x" * 21, False),                  # 太长
    ("has space", False),               # 空格：会让 URL/日志/@提及处处要转义
    ("bad!char", False),
    ("", False),
])
def test_valid_username(name, ok):
    assert auth.valid_username(name)[0] is ok


@pytest.mark.parametrize("name", ["admin", "Admin", "ＡＤＭＩＮ", "root", "api"])
def test_reserved_usernames_rejected(name):
    """⚠️ 判据是**归一化之后**的形式，所以大小写和全角变体都挡得住。"""
    ok, why = auth.valid_username(name)
    assert not ok and "保留" in why


def test_username_length_counted_after_normalization():
    """⚠️ NFKC 会改变长度。按原始串算的话，有人能用全角凑出超长名字。"""
    assert auth.valid_username("Ｋ" * 21)[0] is False


# ============================================================
# 端点：鉴权与配额（真打库，必须 real_auth）
# ============================================================

@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture
def account():
    """建一个临时账号，用完连级联数据一起删。"""
    username = f"pytest-{uuid.uuid4().hex[:10]}"
    password = "test-password-123"
    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_user (username, username_norm, password_hash) "
            "VALUES (%s,%s,%s) RETURNING user_id",
            (username, auth.normalize_username(username), auth.hash_password(password)))
        uid = cur.fetchone()[0]
    conn.commit()
    yield {"user_id": uid, "username": username, "password": password, "conn": conn}
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app_user WHERE user_id = %s", (uid,))
    conn.commit()
    conn.close()


@pytest.mark.real_auth
def test_guest_blocked_from_ask(client):
    """🚨 游客不能用问答（2026-08-24 Kevin 定）。前端也有一道门，
    但**真正的门必须在服务端** —— 前端拦不住直接打 API 的人。"""
    r = client.post(f"{API}/ask", json={"question": "安兹是谁"})
    assert r.status_code == 401


@pytest.mark.real_auth
def test_guest_blocked_from_find(client):
    """/find 也要拦：不然它就是绕过配额的后门，同一件事换个 URL 就免费了。"""
    assert client.get(f"{API}/find", params={"q": "热血番"}).status_code == 401


@pytest.mark.real_auth
@pytest.mark.parametrize("path,params", [
    ("/season", {}),
    ("/questionnaire", {"n": 2}),
    ("/search", {"q": "孤独摇滚"}),
])
def test_zero_model_endpoints_stay_open_to_guests(client, path, params):
    """⚠️ 判据是「这个端点会不会花钱」，不是「它属不属于问答」。
    voice/season/search/questionnaire 是纯 SQL，对游客照常开放 ——
    设计文档「登录不是使用门槛」只有问答那一半被修订了。"""
    assert client.get(API + path, params=params).status_code == 200


@pytest.mark.real_auth
def test_me_returns_null_for_guest(client):
    """⚠️ 未登录是**正常情况**不是错误。做成 401 的话前端每次冷启动都会
    在控制台留一条红色报错，久而久之没人再看控制台了。"""
    r = client.get(f"{API}/auth/me")
    assert r.status_code == 200 and r.json() is None


@pytest.mark.real_auth
def test_register_login_logout_flow(account):
    username = f"Flow-{uuid.uuid4().hex[:10]}"      # 故意带大写，验证原样保留
    with TestClient(app) as c:
        r = c.post(f"{API}/auth/register",
                   json={"username": username, "password": "a-good-password"})
        assert r.status_code == 200
        # ⚠️ 展示的是原样大小写，不是归一化后的小写。
        assert r.json()["username"] == username
        assert c.get(f"{API}/auth/me").json()["username"] == username

        assert c.post(f"{API}/auth/logout").status_code == 200
        assert c.get(f"{API}/auth/me").json() is None

        r = c.post(f"{API}/auth/login",
                   json={"username": username, "password": "a-good-password"})
        assert r.status_code == 200
        assert c.get(f"{API}/auth/me").json()["username"] == username

    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app_user WHERE username_norm = %s",
                    (auth.normalize_username(username),))
    conn.commit()
    conn.close()


@pytest.mark.real_auth
def test_login_username_case_insensitive(client, account):
    """注册时归一化了，登录时也必须归一化 —— 否则用户换个大小写就登不进去。"""
    r = client.post(f"{API}/auth/login", json={
        "username": account["username"].upper(), "password": account["password"]})
    assert r.status_code == 200
    client.post(f"{API}/auth/logout")


@pytest.mark.real_auth
def test_wrong_password_and_unknown_user_are_indistinguishable(client, account):
    """🚨 两者必须报**同一句话**：分开报等于提供了一个账号枚举接口。"""
    bad_pw = client.post(f"{API}/auth/login", json={
        "username": account["username"], "password": "definitely-wrong"})
    no_user = client.post(f"{API}/auth/login", json={
        "username": f"nobody-{uuid.uuid4().hex[:8]}",
        "password": "definitely-wrong"})
    assert bad_pw.status_code == no_user.status_code == 401
    assert bad_pw.json()["detail"] == no_user.json()["detail"]


@pytest.mark.real_auth
def test_duplicate_username_rejected(client, account):
    """⚠️ 大小写不同也算重复 —— 判重走 username_norm。"""
    r = client.post(f"{API}/auth/register", json={
        "username": account["username"].upper(), "password": "another-password"})
    assert r.status_code == 409


@pytest.mark.real_auth
def test_register_rejects_invalid_username(client):
    for bad in ("k", "has space", "admin"):
        r = client.post(f"{API}/auth/register",
                        json={"username": bad, "password": "a-good-password"})
        assert r.status_code == 422, bad


# ── 配额 ─────────────────────────────────────────────────────

@pytest.mark.real_auth
def test_quota_blocks_after_limit(account):
    conn = account["conn"]
    uid = account["user_id"]
    assert quota.status(conn, uid)["remaining"] == quota.DAILY_LIMIT
    for i in range(quota.DAILY_LIMIT):
        quota.reserve(conn, uid, f"问题{i}")
    conn.commit()
    assert quota.status(conn, uid)["remaining"] == 0
    with pytest.raises(quota.QuotaExceeded):
        quota.reserve(conn, uid, "第 11 条")


@pytest.mark.real_auth
def test_quota_refund_restores_one(account):
    """⚠️ 扣费顺序是「先扣后退」，所以 5xx 路径必须退还 —— 不退的话
    LLM 挂一次用户就白丢一条配额。"""
    conn, uid = account["conn"], account["user_id"]
    ask_id = quota.reserve(conn, uid, "会失败的问题")
    conn.commit()
    assert quota.status(conn, uid)["remaining"] == quota.DAILY_LIMIT - 1
    quota.refund(conn, ask_id)
    conn.commit()
    assert quota.status(conn, uid)["remaining"] == quota.DAILY_LIMIT


@pytest.mark.real_auth
def test_quota_reset_at_only_when_exhausted(account):
    """没用满时 reset_at 恒为 null —— 那时「最早一条何时过期」对用户
    毫无信息量，给了反而让人以为要等。"""
    conn, uid = account["conn"], account["user_id"]
    quota.reserve(conn, uid, "一条")
    conn.commit()
    assert quota.status(conn, uid)["reset_at"] is None
    for i in range(quota.DAILY_LIMIT - 1):
        quota.reserve(conn, uid, f"再来{i}")
    conn.commit()
    assert quota.status(conn, uid)["reset_at"] is not None


@pytest.mark.real_auth
def test_ask_endpoint_returns_429_when_exhausted(client, account):
    """端点层面：配额用尽时 /ask 必须 429 且**不调模型**。"""
    conn, uid = account["conn"], account["user_id"]
    for i in range(quota.DAILY_LIMIT):
        quota.reserve(conn, uid, f"占位{i}")
    conn.commit()

    client.cookies.set("anime_rec_session", auth.make_token(uid))
    try:
        r = client.post(f"{API}/ask", json={"question": "安兹是谁"})
        assert r.status_code == 429
        assert "已用满" in r.json()["detail"]
    finally:
        client.cookies.clear()


# ── 评分持久化 ────────────────────────────────────────────────

@pytest.mark.real_auth
def test_ratings_crud_and_skip_deletes(client, account):
    """⚠️ choice='skip' 要**删行**：「没看过」用缺失表示（设计文档 §4）。
    用户把「看过」改回「跳过」时那条旧评分必须消失，留着就是错误的偏好信号。"""
    client.cookies.set("anime_rec_session", auth.make_token(account["user_id"]))
    try:
        r = client.put(f"{API}/ratings", json={"items": [
            {"subject_id": 328609, "choice": "seen", "score": 9.0},
            {"subject_id": 10380, "choice": "wish"},
        ], "source": "questionnaire"})
        assert r.status_code == 200 and r.json()["written"] == 2

        got = {a["subject_id"]: a for a in client.get(f"{API}/ratings").json()["items"]}
        assert got[328609]["score"] == 9.0
        assert got[10380]["choice"] == "wish"

        r = client.put(f"{API}/ratings",
                       json={"items": [{"subject_id": 10380, "choice": "skip"}]})
        assert r.json()["deleted"] == 1
        ids = {a["subject_id"] for a in client.get(f"{API}/ratings").json()["items"]}
        assert 10380 not in ids and 328609 in ids

        assert client.delete(f"{API}/ratings").json()["deleted"] == 1
        assert client.get(f"{API}/ratings").json()["items"] == []
    finally:
        client.cookies.clear()


@pytest.mark.real_auth
def test_guest_merge_does_not_overwrite_cloud(client, account):
    """🚨 合并规则是**云端为准、本地只补空缺**（DO NOTHING，不是覆盖）。
    账号是跨设备的事实来源，让 localStorage 覆盖云端等于用旧数据洗掉新数据，
    且用户完全看不出发生了什么。"""
    uid = account["user_id"]
    client.cookies.set("anime_rec_session", auth.make_token(uid))
    client.put(f"{API}/ratings",
               json={"items": [{"subject_id": 328609, "choice": "seen", "score": 9.0}]})
    client.cookies.clear()

    r = client.post(f"{API}/auth/login", json={
        "username": account["username"], "password": account["password"],
        "guest_ratings": [
            {"subject_id": 328609, "choice": "seen", "score": 1.0},   # 冲突：不该覆盖
            {"subject_id": 1851, "choice": "pass"},                   # 空缺：应写入
        ]})
    assert r.status_code == 200
    got = {a["subject_id"]: a for a in client.get(f"{API}/ratings").json()["items"]}
    assert got[328609]["score"] == 9.0        # 云端的 9.0 保住了
    assert got[1851]["choice"] == "pass"      # 本地独有的补进来了
    client.cookies.clear()


@pytest.mark.real_auth
def test_ratings_require_login(client):
    assert client.get(f"{API}/ratings").status_code == 401
    assert client.put(f"{API}/ratings", json={"items": []}).status_code == 401
    assert client.delete(f"{API}/ratings").status_code == 401


# ============================================================
# 个人页（2026-08-24）：评分明细 + 改用户名 / 改密码
# ============================================================

@pytest.mark.real_auth
def test_ratings_detail_carries_display_fields(client, account):
    """明细端点要带作品名 —— 没有它个人页只能显示一串 subject_id。

    ⚠️ 同时验证它与 `/ratings` **行数一致**：两个端点读的是同一批行，
       只是形状不同。不一致就说明 JOIN 丢了行（外键本该保证不会）。
    """
    client.cookies.set("anime_rec_session", auth.make_token(account["user_id"]))
    try:
        client.put(f"{API}/ratings", json={"items": [
            {"subject_id": 328609, "choice": "seen", "score": 9.0},
            {"subject_id": 10380, "choice": "wish"},
        ], "source": "questionnaire"})

        r = client.get(f"{API}/ratings/detail")
        assert r.status_code == 200
        body = r.json()
        assert body["total"] == 2 == len(body["items"])
        assert len(client.get(f"{API}/ratings").json()["items"]) == body["total"]

        by_id = {it["subject_id"]: it for it in body["items"]}
        assert by_id[328609]["choice"] == "seen" and by_id[328609]["score"] == 9.0
        assert by_id[328609]["name"]                      # 作品名非空
        assert by_id[328609]["source"] == "questionnaire"
        # wish 不该带分数（库里的 CHECK 也保证了这一点）
        assert by_id[10380]["score"] is None
    finally:
        client.delete(f"{API}/ratings")
        client.cookies.clear()


@pytest.mark.real_auth
def test_ratings_detail_requires_login(client):
    assert client.get(f"{API}/ratings/detail").status_code == 401


@pytest.mark.real_auth
def test_ratings_detail_most_recent_first(client, account):
    """列表按最近修改倒序 —— 用户刚改过的那部应该在最上面。"""
    client.cookies.set("anime_rec_session", auth.make_token(account["user_id"]))
    try:
        client.put(f"{API}/ratings",
                   json={"items": [{"subject_id": 328609, "choice": "wish"}]})
        client.put(f"{API}/ratings",
                   json={"items": [{"subject_id": 10380, "choice": "wish"}]})
        # 再改一次最早那条，它应该重新回到最前
        client.put(f"{API}/ratings",
                   json={"items": [{"subject_id": 328609, "choice": "pass"}]})

        ids = [it["subject_id"] for it in client.get(f"{API}/ratings/detail").json()["items"]]
        assert ids[0] == 328609
    finally:
        client.delete(f"{API}/ratings")
        client.cookies.clear()


# ── 改用户名 ──────────────────────────────────────────────────

@pytest.mark.real_auth
def test_change_username_success(client, account):
    """改完之后：/auth/me 是新名，**旧名登不上、新名能登上**。

    ⚠️ 只断言 /auth/me 是不够的 —— 那只证明展示列改了。登录走的是
       `username_norm`，两列必须一起更新，漏掉后者就是「显示已改名、
       但只能用旧名登录」这种自相矛盾的状态。
    """
    new_name = f"renamed-{uuid.uuid4().hex[:8]}"
    client.cookies.set("anime_rec_session", auth.make_token(account["user_id"]))
    try:
        r = client.put(f"{API}/auth/username",
                       json={"username": new_name, "password": account["password"]})
        assert r.status_code == 200
        assert r.json()["username"] == new_name
        assert client.get(f"{API}/auth/me").json()["username"] == new_name
    finally:
        client.cookies.clear()

    old = client.post(f"{API}/auth/login", json={
        "username": account["username"], "password": account["password"]})
    assert old.status_code == 401                      # 旧名已经不存在了
    new = client.post(f"{API}/auth/login", json={
        "username": new_name, "password": account["password"]})
    assert new.status_code == 200
    client.cookies.clear()


@pytest.mark.real_auth
def test_change_username_wrong_password_has_no_effect(client, account):
    """🚨 密码不对不仅要 401，**还必须没有任何副作用**。

    校验与 UPDATE 写反顺序的话（先改再验），名字已经改掉了才返回 401 ——
    而调用方看到 401 会以为什么都没发生。
    """
    client.cookies.set("anime_rec_session", auth.make_token(account["user_id"]))
    try:
        r = client.put(f"{API}/auth/username",
                       json={"username": f"hacked-{uuid.uuid4().hex[:6]}",
                             "password": "definitely-wrong"})
        assert r.status_code == 401
        assert client.get(f"{API}/auth/me").json()["username"] == account["username"]
    finally:
        client.cookies.clear()


@pytest.mark.real_auth
def test_change_username_rejects_taken_name(client, account):
    """判重走 username_norm，所以**大小写不同也算重复**。"""
    other = f"pytest-{uuid.uuid4().hex[:10]}"
    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_user (username, username_norm, password_hash) "
            "VALUES (%s,%s,%s)",
            (other, auth.normalize_username(other), auth.hash_password("x" * 12)))
    conn.commit()

    client.cookies.set("anime_rec_session", auth.make_token(account["user_id"]))
    try:
        r = client.put(f"{API}/auth/username",
                       json={"username": other.upper(),
                             "password": account["password"]})
        assert r.status_code == 409
        assert client.get(f"{API}/auth/me").json()["username"] == account["username"]
    finally:
        client.cookies.clear()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM app_user WHERE username_norm = %s",
                        (auth.normalize_username(other),))
        conn.commit()
        conn.close()


@pytest.mark.real_auth
@pytest.mark.parametrize("bad", ["k", "has space", "admin", "ＡＤＭＩＮ"])
def test_change_username_rejects_invalid(client, account, bad):
    """⚠️ 全角 `ＡＤＭＩＮ` 也要挡住：判据是**归一化之后**的形式，
    NFKC 会把它折成 admin。不折的话保留名清单一绕就过。"""
    client.cookies.set("anime_rec_session", auth.make_token(account["user_id"]))
    try:
        r = client.put(f"{API}/auth/username",
                       json={"username": bad, "password": account["password"]})
        assert r.status_code == 422
    finally:
        client.cookies.clear()


@pytest.mark.real_auth
def test_change_username_case_only_is_allowed(client, account):
    """📌 只改大小写要能成功：norm 没变，UPDATE 的是自己那一行，
    不该被唯一约束拦成 409（那是「用户名被自己占用了」的荒谬状态）。"""
    client.cookies.set("anime_rec_session", auth.make_token(account["user_id"]))
    try:
        upper = account["username"].upper()
        r = client.put(f"{API}/auth/username",
                       json={"username": upper, "password": account["password"]})
        assert r.status_code == 200
        assert r.json()["username"] == upper
    finally:
        client.cookies.clear()


@pytest.mark.real_auth
def test_change_username_requires_login(client):
    assert client.put(f"{API}/auth/username",
                      json={"username": "whoever", "password": "x"}).status_code == 401


# ── 改密码 ────────────────────────────────────────────────────

@pytest.mark.real_auth
def test_change_password_success(client, account):
    """改完：旧密码登不上、新密码能登上，且**当前会话仍然有效**
    （成功后重新种了一次 cookie）。"""
    new_pw = "a-brand-new-password"
    client.cookies.set("anime_rec_session", auth.make_token(account["user_id"]))
    try:
        r = client.put(f"{API}/auth/password", json={
            "current_password": account["password"], "new_password": new_pw})
        assert r.status_code == 200
        # 会话没被踢掉：改完还能继续用
        assert client.get(f"{API}/auth/me").json()["user_id"] == account["user_id"]
    finally:
        client.cookies.clear()

    assert client.post(f"{API}/auth/login", json={
        "username": account["username"],
        "password": account["password"]}).status_code == 401
    assert client.post(f"{API}/auth/login", json={
        "username": account["username"], "password": new_pw}).status_code == 200
    client.cookies.clear()


@pytest.mark.real_auth
def test_change_password_wrong_current_has_no_effect(client, account):
    """🚨 同 test_change_username_wrong_password_has_no_effect：
    401 之后旧密码必须**依然可用**，否则就是「报错了但其实改了」。"""
    client.cookies.set("anime_rec_session", auth.make_token(account["user_id"]))
    try:
        r = client.put(f"{API}/auth/password", json={
            "current_password": "not-the-password", "new_password": "x" * 12})
        assert r.status_code == 401
    finally:
        client.cookies.clear()

    assert client.post(f"{API}/auth/login", json={
        "username": account["username"],
        "password": account["password"]}).status_code == 200
    client.cookies.clear()


@pytest.mark.real_auth
def test_change_password_rejects_short_new_password(client, account):
    """长度下限由 pydantic 的 min_length=8 挡在进 argon2 之前。"""
    client.cookies.set("anime_rec_session", auth.make_token(account["user_id"]))
    try:
        r = client.put(f"{API}/auth/password", json={
            "current_password": account["password"], "new_password": "short"})
        assert r.status_code == 422
    finally:
        client.cookies.clear()


@pytest.mark.real_auth
def test_change_password_requires_login(client):
    assert client.put(f"{API}/auth/password", json={
        "current_password": "x", "new_password": "y" * 12}).status_code == 401
