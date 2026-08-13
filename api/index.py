"""Vercel serverless 入口。

⚠️ **`api/` 目录下的每个 .py 都会被 Vercel 当成一个独立的 serverless function。**
   所以这里**只能有这一个文件** —— 应用代码在 server/ 包里。
   把 server/schemas.py 之类放进 api/ 会导致构建失败（那个文件没有 handler）。

本地开发不走这个文件，直接：
    uv run uvicorn server.main:app --reload

---
vercel.json 的三项配置在这里说明 —— **那个文件不能写注释**，
schema 校验会拒掉任何多余的键（`"//"` 之类），报
`Invalid request: should NOT have additional property "//"`。

`rewrites: /(.*) -> /api/index`
    所有路径都打到这一个 function 上，由 FastAPI 自己路由。

`regions: ["iad1"]`
    ⚠️ 必须与 Neon 的 us-east-2 对齐。真正影响性能的是 API↔DB 的往返延迟 ——
    /recommend 一次要打三次库，跨区能轻易多出 150–300 ms。
    iad1（华盛顿）是离 us-east-2 最近的可选区。Hobby 计划只能指定一个区域。

`includeFiles: "{server,src}/**/*.py"`
    保险。Python builder 默认会带上项目目录，但 server/ 与 src/ 是被本文件
    **间接** import 的。漏掉就是 ModuleNotFoundError。
    ⚠️ data/interim/tag_vocab.json 是运行时依赖（jieba 词典 + keep_tags），
       靠 .vercelignore 的取反规则放行，不在这个 glob 里。
    ⚠️ series_root.json 不需要 —— 它不入 git，系列关系走
       anime_profile.series_root 列。
"""

from server.main import app

# Vercel 的 Python runtime 认这个名字导出的 ASGI 应用
__all__ = ["app"]
