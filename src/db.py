"""Neon Postgres 连接。

⚠️ 灌库/建表/迁移一律走 `DATABASE_URL_DIRECT`（直连）。
   pooler 那条是给线上 FastAPI 用的 —— PgBouncer 的 transaction 模式
   与 psycopg3 的 prepared statement 冲突，批量写会报错。
"""

import os

import psycopg
from dotenv import load_dotenv
from pgvector.psycopg import register_vector


def prepare(conn: psycopg.Connection) -> psycopg.Connection:
    """注册 pgvector 类型适配器。

    ⚠️ **每条新连接都必须过这里。** 不注册的话 `tag_vec` 读回来是**字符串**
       而不是向量，写入时 numpy 数组也没法直接当参数 —— 而且不报类型错，
       是在后面某处解析失败，很难定位。连接池要走 configure= 回调（见 server/main.py）。
    """
    register_vector(conn)
    return conn


def connect(*, autocommit: bool = False) -> psycopg.Connection:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL_DIRECT")
    if not dsn:
        raise RuntimeError("缺少环境变量 DATABASE_URL_DIRECT（见 .env.example）")
    return prepare(psycopg.connect(dsn, autocommit=autocommit))


def pool_dsn() -> str:
    """线上 API 用的 DSN。优先 pooler，没配则退回直连。

    退回是有意的：本地开发通常只配了 DATABASE_URL_DIRECT，
    不该为了跑一次 uvicorn 就逼着填第二个连接串。
    ⚠️ 但线上必须配 pooler —— Render 实例重启/扩容时直连会耗尽
       Neon 免费层的连接数上限。
    """
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_URL_DIRECT")
    if not dsn:
        raise RuntimeError("缺少 DATABASE_URL / DATABASE_URL_DIRECT（见 .env.example）")
    return dsn
