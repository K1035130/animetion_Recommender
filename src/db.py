"""Neon Postgres 连接。

⚠️ 灌库/建表/迁移一律走 `DATABASE_URL_DIRECT`（直连）。
   pooler 那条是给线上 FastAPI 用的 —— PgBouncer 的 transaction 模式
   与 psycopg3 的 prepared statement 冲突，批量写会报错。
"""

import os

import psycopg
from dotenv import load_dotenv


def connect(*, autocommit: bool = False) -> psycopg.Connection:
    load_dotenv()
    dsn = os.environ.get("DATABASE_URL_DIRECT")
    if not dsn:
        raise RuntimeError("缺少环境变量 DATABASE_URL_DIRECT（见 .env.example）")
    return psycopg.connect(dsn, autocommit=autocommit)
