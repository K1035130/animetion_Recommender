"""密码哈希与 JWT 签发/校验。**纯函数，不碰数据库、不联网。**

⚠️ 放在 `src/` 而不是 `server/`：与 `src/embed.py` 同一条理由 ——
   `.vercelignore` 排掉了 `scripts/` 但不排 `src/`，而这里的函数
   请求路径要用。测试也能直接 import，不用起 FastAPI。

## 三条纪律

🚨 **`AUTH_SECRET` 缺失必须报错，绝不能退回默认值。**
   有默认值的话，忘配环境变量的部署会**正常启动、正常签发 token**，
   而那些 token 用的是一个公开在源码里的密钥 —— 任何人都能伪造登录态。
   这是最坏的一类故障：不报错、看起来完全正常。
   与 `build_embeddings.py` 在花钱之前先检查 `SILICONFLOW_API_KEY`
   是同一条纪律（在造成损失之前失败）。

⚠️ **用户名一律经 `normalize_username()` 之后再进库/查库。**
   不归一化的话 `Kevin` 与 `kevin` 是两个账号，而用户认为它们是同一个 ——
   数据库的 UNIQUE 约束在这种情况下形同虚设（它拦的是字节相同，
   不是身份相同）。
   📌 与 `textproc.norm_name` 定义 alias 的键是同构的问题：
      **凡是「用来判断两个东西是不是同一个」的字符串，都必须先归一化。**
   📌 所以库里是两列：`username`（原样展示）+ `username_norm`（判重与查询）。
      归一化只在这里有一个定义处 —— **不要改用 Postgres 的 lower()
      函数索引**，那会变成两个定义处，而 `casefold()` 与 `lower()`
      在边缘 case 上并不等价。

⚠️ **token 里只放 user_id，不放邮箱/权限/昵称。**
   JWT 是**签名**的不是加密的，任何人都能 base64 解开看内容。
   更要紧的是：放进去的字段会随 token 一起被冻结 30 天，
   改了库里的值而 token 里还是旧的，就是一个静默不一致。
   ⇒ 除了「你是谁」，其余一律现查。
"""

import datetime
import os
import re
import unicodedata

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import (
    InvalidHashError,
    VerificationError,
    VerifyMismatchError,
)
from dotenv import load_dotenv

# ⚠️ argon2id（PasswordHasher 的默认类型），不是 bcrypt/sha256 —— 设计文档 §4 已定。
#    参数用库的默认值：argon2-cffi 的默认已对齐 OWASP 建议，
#    自己调参数除非有实测依据，否则更可能调差。
#    📌 参数变了也不用重灌：编码串里带着参数，旧串照样能验，
#       `needs_rehash()` 会在下次登录成功时告诉你该升级哪一条。
_hasher = PasswordHasher()

# 🚨 **时间侧信道的挡箭牌。** 登录时若邮箱不存在就直接返回，那条路径会比
#    「邮箱存在但密码错」快几十毫秒（少了一次 argon2 校验，而 argon2 是
#    **故意**设计得慢的）。攻击者掐表就能枚举出哪些邮箱注册过本站 ——
#    **响应文案一致但响应时间不一致，等于没防**。
#    ⇒ 邮箱不存在时也拿这个假串跑一次 verify，两条路径开销对齐。
# ⚠️ 这不是真密码，是对一个随机字符串的哈希，永远不可能被匹配上。
DUMMY_HASH = _hasher.hash("not-a-real-password-timing-guard")

ALGORITHM = "HS256"
TOKEN_TTL = datetime.timedelta(days=30)

# 密码长度下限。⚠️ 只做长度不做「必须含大写+数字+符号」那套 ——
# 复杂度规则被实证是负收益（逼出 Passw0rd! 这类可预测的变体），
# NIST SP 800-63B 现在也建议只查长度 + 黑名单。
PASSWORD_MIN = 8
PASSWORD_MAX = 128

USERNAME_MIN = 2
USERNAME_MAX = 20

# `\w` 在 Python 的默认（Unicode）模式下涵盖各语言字母、数字和下划线，
# 所以「小明」「kevin_2024」「あきら」都合法 —— 这是个中文站，
# 限成纯 ASCII 没道理。另外放行连字符。
# ⚠️ **不放行空格与标点**：用户名要在 URL、日志、@提及里出现，
#    带空格的标识符每一处都得额外转义，而收益为零。
_USERNAME_RE = re.compile(r"^[\w-]+$", re.UNICODE)

# 保留名：避免有人注册成 admin 之后在界面上冒充官方身份。
# ⚠️ 判据是**归一化之后**的形式，否则 `Admin`、`ＡＤＭＩＮ`（全角）
#    都能绕过 —— NFKC 会把全角折成半角，正是为此。
RESERVED_USERNAMES = frozenset({
    "admin", "administrator", "root", "system", "official", "staff",
    "support", "help", "moderator", "mod", "null", "undefined", "anonymous",
    "me", "self", "api", "guest",
})


class AuthError(Exception):
    """凭据无效 / token 不可用。⚠️ 调用方对外的报错必须模糊化，见下。"""


def secret() -> str:
    """JWT 签名密钥。**没配就抛异常，不返回默认值。**

    ⚠️ 服务端启动时应当主动调一次（见 server/main.py 的惰性初始化），
       让「忘配密钥」在部署时就炸，而不是等第一个用户注册时才炸。
    """
    load_dotenv()
    s = os.environ.get("AUTH_SECRET")
    if not s:
        raise RuntimeError(
            "缺少环境变量 AUTH_SECRET（见 .env.example）。"
            "生成一个：python -c \"import secrets; print(secrets.token_urlsafe(48))\""
        )
    # 太短的密钥等于没有。32 字符是 HS256 的合理下限。
    if len(s) < 32:
        raise RuntimeError(f"AUTH_SECRET 太短（{len(s)} 字符），至少 32 位")
    return s


def normalize_username(username: str) -> str:
    """判重与登录查询用的形式。**入库与查库都必须走这里。**

    三步，每一步都对应一类「看起来一样但字节不同」：

        strip()      首尾空白 —— 复制粘贴时最常带进来的东西
        NFKC         全角 `Ｋｅｖｉｎ` → `Kevin`、组合字符 `é` 的两种写法
                     统一成一种。⚠️ 不做这步的话，注册一个全角同名账号
                     就能在界面上冒充别人，而唯一约束完全拦不住
        casefold()   比 lower() 更彻底的大小写折叠（德语 ß → ss）；
                     选它而不是 lower() 正是因为要覆盖这些边缘 case

    ⚠️ 返回值只用来**比较和存 `username_norm`**，展示一律用原始的
       `username` 列 —— 用户写 `Kevin` 就该看到 `Kevin`，不是 `kevin`。
    """
    return unicodedata.normalize("NFKC", username.strip()).casefold()


def valid_username(username: str) -> tuple[bool, str]:
    """校验用户名，返回 (是否合法, 不合法时的原因)。

    ⚠️ **长度按归一化后算**：NFKC 会改变长度（全角折半角、`①` → `1`），
       按原始串算的话有人能用全角凑出一个超长的名字。
    """
    norm = normalize_username(username)
    if not norm:
        return False, "用户名不能为空"
    if len(norm) < USERNAME_MIN:
        return False, f"用户名至少 {USERNAME_MIN} 个字符"
    if len(norm) > USERNAME_MAX:
        return False, f"用户名最多 {USERNAME_MAX} 个字符"
    if not _USERNAME_RE.match(norm):
        return False, "用户名只能包含字母、数字、下划线和连字符"
    if norm in RESERVED_USERNAMES:
        return False, "该用户名已被保留，请换一个"
    return True, ""


def hash_password(password: str) -> str:
    """明文 → argon2id 编码串（含算法、参数、salt，直接整串入库）。"""
    if not PASSWORD_MIN <= len(password) <= PASSWORD_MAX:
        raise AuthError(f"密码长度必须在 {PASSWORD_MIN}–{PASSWORD_MAX} 之间")
    return _hasher.hash(password)


def verify_password(password_hash: str, password: str) -> bool:
    """校验密码。异常一律吃掉返回 False —— 调用方只需要真/假。

    ⚠️ **不要把 VerifyMismatchError 和 InvalidHashError 区分着往外报。**
       前者是「密码错」，后者是「库里那条哈希串坏了」，但对外都必须是
       同一句「邮箱或密码不正确」：分开报等于告诉攻击者哪个邮箱存在
       （账号枚举）。
    """
    try:
        return _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError, VerificationError):
        return False


def needs_rehash(password_hash: str) -> bool:
    """这条哈希是不是用旧参数生成的（该在下次登录成功时重算）。

    ⚠️ 只能在**刚校验成功**时调用 —— 那是唯一一个我们手上有明文的时刻。
    """
    try:
        return _hasher.check_needs_rehash(password_hash)
    except InvalidHashError:
        return True


def make_token(user_id: int, *, now: datetime.datetime | None = None) -> str:
    """签发 access token。

    ⚠️ 只放 user_id（`sub`）。不放邮箱、不放权限 —— 见模块注释第三条。
    📌 **没有 refresh token**（Kevin 2026-08-24 定）：作品集项目上
       token rotation 是过度设计，30 天到期重新登录即可。
    """
    now = now or datetime.datetime.now(datetime.UTC)
    payload = {
        # ⚠️ sub 必须是字符串 —— RFC 7519 如此要求，pyjwt 新版本会对
        #    非字符串的 sub 报错。取出来时记得转回 int。
        "sub": str(user_id),
        "iat": now,
        "exp": now + TOKEN_TTL,
    }
    return jwt.encode(payload, secret(), algorithm=ALGORITHM)


def read_token(token: str) -> int:
    """校验并取出 user_id。任何问题（过期/篡改/格式错）一律抛 AuthError。

    ⚠️ **必须指定 algorithms**。不指定的话 pyjwt 会接受 token 自己
       header 里声明的算法 —— 攻击者把 alg 改成 "none" 就能绕过签名。
       这是 JWT 最经典的一个坑，pyjwt 现在默认拦住了，但显式写出来
       才不会因为将来换库/换版本而回退。
    """
    try:
        payload = jwt.decode(
            token,
            secret(),
            algorithms=[ALGORITHM],          # ⚠️ 见上，别删
            options={"require": ["exp", "sub"]},
        )
        return int(payload["sub"])
    except (jwt.InvalidTokenError, ValueError, KeyError) as e:
        raise AuthError("登录状态无效或已过期") from e
