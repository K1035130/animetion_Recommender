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
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# ⚠️ 复用 build_embeddings 的节流器，**不要复制一份**。两个脚本打的是同一家
#    API，限额常数一旦分叉就会用不同速率去撞同一个上限 —— 而分叉不报错，
#    只会偶发限流。（与 build_plot_chunks 复用 resolve_texts 同一条理由。）
from build_embeddings import TokenBudget, make_bar

from src import db, translate, translate_cache
from src.langclean import is_japanese, kana_ratio, strip_jp_tail

DUMP = Path(__file__).resolve().parent.parent / "data" / "raw" / "dump"
CHARACTERS = DUMP / "character.jsonlines"
SUBJECT_CHARACTERS = DUMP / "subject-characters.jsonlines"

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001

LOCK = Path(__file__).resolve().parent.parent / "data" / "interim" / "translate.lock"


class SingleInstance:
    """同一时刻只允许一个本脚本在跑。

    ⚠️ **这道锁是被真事故逼出来的（2026-08-17）**：连续三条链的
       `--scope char` 进程同时活着，因为 Windows 上杀掉外层 shell
       **不会连带杀掉 python 子进程**。后果不是崩溃，而是：

         · 三份各自算出的待办清单互相重叠 → **20% 的条目被翻了 2~3 遍**
         · 观测到的"速率"是三个进程之和 → 把 ETA 估算带偏
         · 更糟：先启动的那两个是加质量闸**之前**的版本，
           它们一直在往同一个缓存里写未经校验的译文（实测 116 条）

       ⚠️ 全程没有任何报错 —— 三个进程各自的日志都完全正常。

    锁文件存 PID；发现残留锁时校验该 PID 是否真的活着（崩溃后不会卡死）。
    """

    def __enter__(self):
        if LOCK.exists():
            try:
                # ⚠️ 去掉 BOM 再解析。锁文件正常由本脚本写（无 BOM），但用
                #    PowerShell 手工写过一次就会带上 ﻿ —— int() 抛异常 →
                #    被当成残留锁放行 → **锁静默失效**。实测踩过。
                old = int(LOCK.read_text(encoding="utf-8-sig").strip())
            except (ValueError, OSError):
                old = -1
            if old > 0 and _pid_alive(old):
                raise SystemExit(
                    f"✗ 已有翻译进程在跑（PID {old}）。\n"
                    f"  同时跑多个会重复翻译并互相干扰 —— 先停掉它，或删除 {LOCK}")
            print(f"（清理残留锁：PID {old} 已不存在）")
        LOCK.parent.mkdir(parents=True, exist_ok=True)
        LOCK.write_text(str(os.getpid()))
        return self

    def __exit__(self, *exc):
        try:
            LOCK.unlink(missing_ok=True)
        except OSError:
            pass


def _pid_alive(pid: int) -> bool:
    """⚠️ **只判 OpenProcess 成不成功是不够的。**
       进程退出后，只要还有人握着它的句柄，内核对象就不销毁，
       OpenProcess 照样返回有效句柄 —— 于是死进程被判成活的，
       **残留锁永远清不掉、脚本再也起不来**（实测踩过）。
       必须再问一次退出码：STILL_ACTIVE(259) 才算真活着。
    """
    if sys.platform == "win32":
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        code = ctypes.c_ulong()
        ok = k.GetExitCodeProcess(h, ctypes.byref(code))
        k.CloseHandle(h)
        return bool(ok) and code.value == 259
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


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

    ⚠️ **翻译单位必须与 loader 的查找单位逐字一致**，否则译文永远查不到 ——
       而那不报错，只是日文原样留在库里，几小时的翻译白做。
       两个 scope 因此走**不同的来源**：

         profile  库里的 anime_profile.summary
                  （已由 backfill 剥离过，与 loader 的 strip_jp_tail(dump) 等价）
         char     **dump 的整条 character.summary**，不是 plot_chunk.text
                  ⚠️ 后者是**切块之后**的片段，而 build_char_chunks 是
                     「剥离 → 换译文 → 切块」，查的是整条。
                     首版取了 plot_chunk.text，实测交集只有 19,799/42,211 ——
                     一半以上的译文 loader 永远用不上。

    ⚠️ 先过 is_japanese()（阈值 0.20 不是 0.03 —— 0.03 会把「中文里引用了
       日文原名」的条目误判成日文，见 langclean）。
    ⚠️ 去重不是优化：同一段简介可能被多个条目共用（实测 128 部共用 summary），
       不去重就是重复占用墙钟时间。
    """
    seen: set[str] = set()
    out: list[str] = []

    def add(raw: str) -> None:
        t = strip_jp_tail((raw or "").strip())
        if t and t not in seen and is_japanese(t):
            seen.add(t)
            out.append(t)

    if scope == "profile":
        with db.connect() as conn, conn.cursor() as cur:
            cur.execute("""SELECT summary FROM anime_profile
                            WHERE summary IS NOT NULL AND length(summary) > 20""")
            for (t,) in cur.fetchall():
                add(t)
        return out

    # char：只要落在我们 11,453 部之内的角色，与 build_char_chunks 同一口径
    with db.connect() as conn, conn.cursor() as cur:
        cur.execute("SELECT subject_id FROM anime_profile")
        known = {r[0] for r in cur.fetchall()}
    in_scope: set[int] = set()
    with SUBJECT_CHARACTERS.open(encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if d["subject_id"] in known:
                in_scope.add(d["character_id"])
    with CHARACTERS.open(encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if int(d["id"]) in in_scope:
                add(d.get("summary") or "")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--scope", choices=("profile", "char"), required=True)
    ap.add_argument("--limit", type=int, help="只跑前 N 条（试跑）")
    # ⚠️ 并发不再是单个数字 —— 每个模型的服务端容量差一个数量级
    #    （MT 实测上限约 2–4，Qwen3-8B 能吃 16），统一取值必然一头浪费一头挨打。
    #    每模型的值在 translate.CONCURRENCY 里。
    ap.add_argument("--models", default="all",
                    help="逗号分隔的模型名，或 all（默认，= translate.ACCEPTED）")
    args = ap.parse_args()

    models = (list(translate.ACCEPTED) if args.models == "all"
              else [m.strip() for m in args.models.split(",") if m.strip()])
    unknown = [m for m in models if m not in translate.CONCURRENCY]
    if unknown:
        print(f"⚠️ 未知模型（没有并发配置）：{unknown}", file=sys.stderr)
        return 2

    texts = fetch(args.scope)
    if args.limit:
        texts = texts[:args.limit]
    total_chars = sum(map(len, texts))
    print(f"待译 {len(texts):,} 条 · {total_chars:,} 字符 · "
          f"均 {total_chars // max(len(texts), 1)} 字")

    cache = translate_cache.connect()
    # ⚠️ 跨模型查：任何一个受信模型翻过就算翻过。只查首选模型的话，
    #    协作模型翻好的那几万条会被判成"没翻"→ 重复劳动。
    hit = translate_cache.get_many_any(cache, texts, translate.PROMPT_VERSION,
                                       translate.ACCEPTED)
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
        key = translate.api_key()
        print("热身 …")
        for m in models:
            print(f"  {m:42s} {translate.warm_up(key, m):6.1f}s")

        # ⚠️ **一个共享队列，不预先切分工作。** 各模型速度差 5 倍以上
        #    （MT 29 字符/s vs Qwen3-8B 94），静态均分会让快的干完空等慢的。
        #    共享队列天然按实际速度分配 —— 快的自己会多拿几批。
        work: queue.Queue = queue.Queue()
        for b in batches:
            work.put(b)
        results: queue.Queue = queue.Queue()

        # ⚠️ **每模型一份 TokenBudget，不共用。** 限额是按模型算的，
        #    共用一份等于让四个模型挤同一条 80,000 TPM 的道，白白自缚。
        budgets = {m: TokenBudget(translate.TOKENS_PER_MIN) for m in models}
        dead: set[str] = set()          # 额度耗尽/鉴权失败的模型
        stop = threading.Event()
        bar = make_bar(len(todo), "翻译", "条")
        done = missed = 0
        per_model: dict[str, int] = {m: 0 for m in models}
        t0 = time.perf_counter()

        def worker(model: str) -> None:
            while not stop.is_set() and model not in dead:
                try:
                    batch = work.get_nowait()
                except queue.Empty:
                    return
                try:
                    budgets[model].take(
                        int(sum(map(len, batch)) * translate.TOKENS_PER_CHAR))
                    if stop.is_set() or model in dead:
                        work.put(batch)      # 没做就还回去，别丢
                        return
                    results.put(("ok", model, batch,
                                 translate.translate_batch(batch, key, model)))
                except translate.QuotaExhausted as e:
                    # ⚠️ 只停这一个模型，不停整轮 —— 四个模型里挂一个，
                    #    剩下三个照样干活，这正是多模型的价值之一。
                    dead.add(model)
                    work.put(batch)          # 退回队列让别的模型接手
                    results.put(("dead", model, batch, e))
                    return
                except translate.TranslateError as e:
                    results.put(("err", model, batch, e))

        threads = []
        for m in models:
            for _ in range(translate.CONCURRENCY[m]):
                t = threading.Thread(target=worker, args=(m,), daemon=True)
                t.start()
                threads.append(t)
        print(f"工人 {len(threads)} 个："
              + " · ".join(f"{m.split('/')[-1]}×{translate.CONCURRENCY[m]}"
                           for m in models))

        try:
            # ⚠️ **SQLite 写入只在主线程。** 工作线程只发请求 —— sqlite3 连接
            #    默认 check_same_thread=True，跨线程直接抛异常；
            #    关掉也会撞 `database is locked`。串行化是构造上的，不靠纪律。
            while any(t.is_alive() for t in threads) or not results.empty():
                try:
                    kind, model, batch, payload = results.get(timeout=0.5)
                except queue.Empty:
                    continue
                if kind == "dead":
                    bar.write(f"⚠️ 停用 {model}：{str(payload)[:90]}")
                    continue                 # 该批已退回队列，不计进度
                if kind == "err":
                    bar.write(f"⚠️ 一批失败（{model.split('/')[-1]}），跳过："
                              f"{str(payload)[:90]}")
                    missed += len(batch)
                    bar.update(len(batch))
                    continue
                # ⚠️ 对不齐的**不补位**，记成 missed 留给下一轮重跑。
                #    补位会张冠李戴，而那不报错（见 translate._parse）。
                missed += len(batch) - len(payload)
                # ⚠️ 按**实际产出它的模型**写缓存 —— 键里含模型名，
                #    统一写成首选模型会让下次查找算出错的键。
                translate_cache.put_many(cache, list(payload.items()), model,
                                         translate.PROMPT_VERSION)
                done += len(payload)
                per_model[model] += len(payload)
                bar.update(len(batch))
                bar.set_postfix_str(f"成功 {done:,} 缺 {missed:,}", refresh=False)
        except KeyboardInterrupt:
            stop.set()
            bar.close()
            print(f"\n已中断。成功 {done:,} 条已落缓存，重跑从断点继续。",
                  file=sys.stderr)
            return 130
        bar.close()
        if len(dead) == len(models):
            print("\n✗ 所有模型都停用了（额度/鉴权）。已翻的在缓存里，重跑可续传。",
                  file=sys.stderr)
            return 1

    dt = time.perf_counter() - t0
    print(f"\n完成 {done:,} 条 · 未对齐 {missed:,} 条 · 耗时 {dt / 60:.1f} 分钟")
    for m in models:
        tag = " (已停用)" if m in dead else ""
        print(f"    {m:42s} {per_model[m]:>7,} 条{tag}")
    if missed:
        print(f"⚠️ {missed:,} 条没对齐 —— **再跑一次本脚本**即可只重试这些")

    # 抽查：译文里不该还剩大量假名
    sample = translate_cache.get_many_any(cache, texts[:200],
                                          translate.PROMPT_VERSION,
                                          translate.ACCEPTED)
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
    with SingleInstance():
        raise SystemExit(main())
