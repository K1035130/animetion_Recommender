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
    ("  Kevin@X.COM ", "kevin@x.com"),
    ("a@b.co", "a@b.co"),
    ("MiXeD@Case.Org", "mixed@case.org"),
])
def test_normalize_email(raw, expect):
    """不归一化的话 UNIQUE 约束形同虚设 —— 它拦的是字节相同，不是身份相同。"""
    assert auth.normalize_email(raw) == expect


def test_normalize_email_keeps_plus_and_dots():
    """⚠️ 只归一化大小写与空白。删点/去 + 号是 Gmail 特有的规则，
    套到别家域名上会把两个不同的人合成一个账号 —— 宁可一个人两个号。"""
    assert auth.normalize_email("a.b+tag@example.com") == "a.b+tag@example.com"


@pytest.mark.parametrize("email,ok", [
    ("a@b.co", True), ("no-at-sign", False), ("a b@c.co", False),
    ("a@b", False), ("", False),
])
def test_valid_email(email, ok):
    assert auth.valid_email(email) is ok


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
    email = f"t-{uuid.uuid4().hex[:12]}@pytest.local"
    password = "test-password-123"
    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO app_user (email, password_hash) VALUES (%s,%s) RETURNING user_id",
            (email, auth.hash_password(password)))
        uid = cur.fetchone()[0]
    conn.commit()
    yield {"user_id": uid, "email": email, "password": password, "conn": conn}
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
    email = f"flow-{uuid.uuid4().hex[:12]}@pytest.local"
    with TestClient(app) as c:
        r = c.post(f"{API}/auth/register",
                   json={"email": email, "password": "a-good-password"})
        assert r.status_code == 200
        assert r.json()["email"] == email
        assert c.get(f"{API}/auth/me").json()["email"] == email

        assert c.post(f"{API}/auth/logout").status_code == 200
        assert c.get(f"{API}/auth/me").json() is None

        r = c.post(f"{API}/auth/login",
                   json={"email": email, "password": "a-good-password"})
        assert r.status_code == 200
        assert c.get(f"{API}/auth/me").json()["email"] == email

    conn = db.connect()
    with conn.cursor() as cur:
        cur.execute("DELETE FROM app_user WHERE email = %s", (email,))
    conn.commit()
    conn.close()


@pytest.mark.real_auth
def test_login_email_case_insensitive(client, account):
    """注册时归一化了，登录时也必须归一化 —— 否则用户用大写邮箱就登不进去。"""
    r = client.post(f"{API}/auth/login", json={
        "email": account["email"].upper(), "password": account["password"]})
    assert r.status_code == 200
    client.post(f"{API}/auth/logout")


@pytest.mark.real_auth
def test_wrong_password_and_unknown_email_are_indistinguishable(client, account):
    """🚨 两者必须报**同一句话**：分开报等于提供了一个账号枚举接口。"""
    bad_pw = client.post(f"{API}/auth/login", json={
        "email": account["email"], "password": "definitely-wrong"})
    no_user = client.post(f"{API}/auth/login", json={
        "email": f"nobody-{uuid.uuid4().hex[:8]}@pytest.local",
        "password": "definitely-wrong"})
    assert bad_pw.status_code == no_user.status_code == 401
    assert bad_pw.json()["detail"] == no_user.json()["detail"]


@pytest.mark.real_auth
def test_duplicate_email_rejected(client, account):
    r = client.post(f"{API}/auth/register", json={
        "email": account["email"].upper(), "password": "another-password"})
    assert r.status_code == 409


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
        "email": account["email"], "password": account["password"],
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
