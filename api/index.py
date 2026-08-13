"""Vercel serverless 入口。

⚠️ **`api/` 目录下的每个 .py 都会被 Vercel 当成一个独立的 serverless function。**
   所以这里**只能有这一个文件** —— 应用代码在 server/ 包里。
   把 server/schemas.py 之类放进 api/ 会导致构建失败（那个文件没有 handler）。
   vercel.json 用 rewrites 把所有路径都打到这个入口上。

本地开发不走这个文件，直接：
    uv run uvicorn server.main:app --reload
"""

from server.main import app

# Vercel 的 Python runtime 认这个名字导出的 ASGI 应用
__all__ = ["app"]
