"""把库里的日文语料译成中文 —— 只写缓存，不写库。

⚠️ **职责边界**：本脚本只产出 `data/interim/translate_cache/`。
   把译文灌进库是独立的一步（apply_translations.py），理由与
   fetch_moegirl / parse_moegirl 分家一样：**翻译很贵（十几小时），
   而灌库策略可能要改几次** —— 分开就不用为了改灌库重翻一遍。

跑法：
    uv run --group etl python scripts/translate_corpus.py --scope profile   # 作品简介
    uv run --group etl python scripts/translate_corpus.py --scope char      # 角色简介
    uv run --group etl python scripts/translate_corpus.py --scope profile --limit 50

⚠️ **可中断可续跑**：每批提交缓存，重跑自动跳过已翻的。
⚠️ **自动防止系统睡眠**（Windows）：睡眠会挂起进程、断开连接。
   显示器仍允许休眠 —— 只申请 ES_SYSTEM_REQUIRED。
"""

from __future__ import annotations

import argparse
import ctypes
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from build_embeddings import make_bar

from src import db, translate, translate_cache
from src.langclean import is_japanese, kana_ratio

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001


class KeepAwake:
    """跑的时候别让系统睡。⚠️ 退出时必须还原，否则机器永远不睡。"""

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


def fetch(scope: str) -> list[str]:
    """取出需要翻译的**去重**源文本。

    ⚠️ 先过 is_japanese()，它内部的阈值是 0.20 而不是 0.03 ——
       0.03 会把「中文里引用了日文原名」的条目误判成日文（见 langclean）。
    ⚠️ 去重不是优化：同一段简介可能被多个条目共用（实测 128 部共用 summary），
       不去重就是重复付费 + 重复占用墙钟时间。
    """
    with db.connect() as conn, conn.cursor() as cur:
        if scope == "profile":
            cur.execute("""SELECT summary FROM anime_profile
                            WHERE summary IS NOT NULL AND length(summary) > 20""")
        else:
            cur.execute("""SELECT text FROM plot_chunk
                            WHERE source = 'bangumi_char'""")
        seen, out = set(), []
        for (t,) in cur.fetchall():
            if t not in seen and is_japanese(t):
                seen.add(t)
                out.append(t)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", choices=("profile", "char"), required=True)
    ap.add_argument("--limit", type=int, help="只跑前 N 条（试跑）")
    args = ap.parse_args()

    texts = fetch(args.scope)
    if args.limit:
        texts = texts[:args.limit]
    total_chars = sum(map(len, texts))
    print(f"待译 {len(texts):,} 条 · {total_chars:,} 字符 · "
          f"均 {total_chars // max(len(texts), 1)} 字")

    cache = translate_cache.connect()
    hit = translate_cache.get_many(cache, texts, translate.MODEL,
                                   translate.PROMPT_VERSION)
    todo = [t for t in texts if t not in hit]
    n_cached, mb = translate_cache.stats(cache)
    print(f"缓存命中 {len(hit):,} / {len(texts):,}，待请求 {len(todo):,}"
          f"（缓存现有 {n_cached:,} 条 / {mb:.1f} MB）")
    if not todo:
        print("全部已在缓存中，无需请求。")
        return 0

    batches = translate.make_batches(todo)
    print(f"{len(batches):,} 批（按 {translate.BATCH_CHARS} 字符预算切，"
          f"均 {len(todo) / max(len(batches), 1):.1f} 条/批）")

    with KeepAwake():
        print("热身 …", end=" ", flush=True)
        print(f"{translate.warm_up():.1f}s")

        key = translate.api_key()
        bar = make_bar(len(todo), "翻译", "条")
        done = missed = 0
        t0 = time.perf_counter()
        try:
            for bi, batch in enumerate(batches):
                try:
                    got = translate.translate_batch(batch, key)
                except translate.QuotaExhausted as e:
                    bar.close()
                    print(f"\n✗ {e}\n  已翻的都在缓存里，补额度后重跑即可续传",
                          file=sys.stderr)
                    return 1
                except translate.TranslateError as e:
                    bar.write(f"⚠️ 批 {bi} 失败，跳过：{e}")
                    missed += len(batch)
                    bar.update(len(batch))
                    continue

                # ⚠️ 对不齐的条目**不补位**，记成 missed 留给下一轮重跑。
                #    补位会张冠李戴，而那不报错（见 translate._parse）。
                missed += len(batch) - len(got)
                translate_cache.put_many(cache, list(got.items()),
                                         translate.MODEL, translate.PROMPT_VERSION)
                done += len(got)
                bar.update(len(batch))
                bar.set_postfix_str(f"成功 {done:,} 缺 {missed:,}", refresh=False)
        except KeyboardInterrupt:
            bar.close()
            print(f"\n已中断。成功 {done:,} 条已落缓存，重跑从断点继续。",
                  file=sys.stderr)
            return 130
        bar.close()

    dt = time.perf_counter() - t0
    print(f"\n完成 {done:,} 条 · 未对齐 {missed:,} 条 · 耗时 {dt / 60:.1f} 分钟")
    if missed:
        print(f"⚠️ {missed:,} 条没对齐 —— **再跑一次本脚本**即可只重试这些")

    # 抽查：译文里不该还剩大量假名
    sample = translate_cache.get_many(cache, texts[:200], translate.MODEL,
                                      translate.PROMPT_VERSION)
    if sample:
        joined = "".join(sample.values())
        bad = sum(1 for v in sample.values() if kana_ratio(v) > 0.05)
        print(f"抽查 {len(sample)} 条：整体残留假名率 {kana_ratio(joined):.4f} · "
              f"单条 >0.05 的 {bad} 条")
    n_cached, mb = translate_cache.stats(cache)
    print(f"缓存现有 {n_cached:,} 条 / {mb:.1f} MB")
    translate.close_client()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
