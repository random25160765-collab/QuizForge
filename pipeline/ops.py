"""资料处理的**运维与观测**：把"现在什么状态"变成一条命令能问出来的东西。

## 为什么要有这个模块

向量化是这条流水线上唯一"分钟到小时级"的一步，而它以前的可观测性是**人肉**：
`ps` 数进程、`ls -la` 看日志、写一段临时 python 数窗口 —— 于是同一晚上踩了两次：

* 一个进程**退出码 0**，其实每片只写了最后一窗（上一次中断留下的半片）；
* 另一个还在跑，而"每片至少有一行"被当成了"算完了"，报了两次"齐了"。

两次都不是手抖，是**缺设施**：没有分口的原因（哪些算完了、预计还要多久）、
没有跑单（谁在跑、跑到哪、怎么结束的）、没有对账（存了几窗 vs 该有几窗）。
所以这里把四件事固化下来，一件一条命令：

    python -m pipeline.ops                    # 总览：在跑什么、还差多少、还要多久
    python -m pipeline.ops audit [--exact]    # 对账：逐本"存了几窗 / 该有几窗"，缺的列出来
    python -m pipeline.ops runs [--limit 12]  # 跑单账本：谁、何时、多少窗、成没成、卡住没有
    python -m pipeline.ops drive --materials a b --slots 3 --batch 8
                                              # 调度：保持 N 个进程把这批算完，结束时自动复验

## 三条分寸

* **同一把尺子**：算"该有几窗"用的就是 `pipeline/embed.py` 的 `_windows` —— 另写一份
  迟早与真正的实现分家，那比没有对账更坏；
* **不猜**：进程没了、跑单还挂着 `running`，就照实说"中断"，不把它当成功；
* **默认快、`--exact` 准**：默认用切片上缓存的 `windows` 列（秒级），`--exact` 读正文重算
  （权威，并顺手把缓存校准回去）。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from . import config

sys.path.insert(0, str(config.ROOT / "api"))  # 与 `embed.py` 同一处：`app` 包在 `api/` 下

from app.db import get_engine, get_session_factory  # noqa: E402
from app.models import EmbedRun, Material, MaterialSlice, SliceEmbedding  # noqa: E402

from . import config  # noqa: E402
from .embed import _source_lines, _windows  # noqa: E402

#: 跑单里的 pid 是不是还活着（只对本机有意义；跨机留 `None` = 不知道）。
def _pid_alive(pid: int, host: str) -> bool | None:
    if not pid:
        return None
    if host and host != os.uname().nodename:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


#: 一个向量化进程在 `MemAvailable` 里的**增量**（不是峰值 RSS）。
#:
#: 峰值 RSS 是 1.6~1.9G，闸门按 `MemAvailable` 算 —— 而实测 3 个进程时可用内存还有约 2.9G
#: （空闲约 6.5G），也就是一个进程只吃掉约 1.2~1.4G：差在共享页与可回收的缓存上。
#: 按 2G 去卡会把并发压到 2 —— 那不是安全，那是白等（3 个进程这台机器跑得好好的）。
WORKER_MEM_MB = 1400

#: 系统红线：可用内存低于它，**一个都不再放**，并把一个在跑的按住（`SIGSTOP`）。
#: 这不是保守 —— 这台机器（WSL2，15G）被 OOM 干掉过一次，那次连 API 服务一起没了。
SYSTEM_FLOOR_MB = 1000


def _resources() -> dict:
    """机器这一刻的余量。调度与报告共用这一处口径。

    两条实测教训写进数字里：这台机器（16 核 / 15G）上 **3 个** embed 进程合计约 3.9 窗/秒，
    **5 个** 反而掉到 3.5（负载冲到 37，互相抢）；而它被 OOM 干掉过一次。
    所以"有活干就多开"是错的 —— 得看机器吃不吃得下。
    """
    info: dict[str, int] = {}
    try:
        with open("/proc/meminfo", "r", encoding="utf-8") as handle:
            for line in handle:
                key, _, rest = line.partition(":")
                parts = rest.split()
                if parts:
                    info[key] = int(parts[0])
    except OSError:
        pass
    try:
        load1 = os.getloadavg()[0]
    except OSError:
        load1 = 0.0
    return {
        "cores": os.cpu_count() or 4,
        "load1": round(load1, 2),
        "mem_total_mb": info.get("MemTotal", 0) // 1024,
        "mem_avail_mb": info.get("MemAvailable", 0) // 1024,
    }


def _plan_slots(requested: int, res: dict) -> tuple[int, str]:
    """这一刻**该开几个** —— 既不让机器闲着，也不把 WSL 自己挤崩。

    三道闸门取最小，并且**把理由说出来**：调度器不该是黑盒 —— 今晚已经因为"只能靠猜"
    吃过两次亏，所以它每次决策都要能回答"为什么是 3 个而不是 5 个"。

    * 用户给的 `--slots` 现在是**上限**，不是硬指标；
    * **CPU**：核数整除 5（16 核 → 3，与实测甜点一致）；负载已经很高时**不加人** ——
      那时候加进去只会互相抢，总吞吐反而降；
    * **内存**：扣掉留给系统的 `SYSTEM_FLOOR_MB`，剩下还塞得下几个 `WORKER_MEM_MB`。
    """
    cores = int(res["cores"] or 4)
    load1 = float(res["load1"])
    # CPU：核数整除 5（16 核 → 3，与实测甜点一致）。负载只当**收敛的旋钮**，不当开关：
    # 这个负载里有一大半是 onnxruntime 那两个线程池在空转（3 个进程就能到 24），
    # 拿它一刀切会把并发砍成 1，反而让机器闲着。
    by_cpu = max(1, cores // 5)
    if load1 > cores * 1.5:
        by_cpu = min(by_cpu, 2)
    if load1 > cores * 2.5:
        by_cpu = 1
    # 内存：红线之上**至少放一个** —— 不然队列没人推，就成了"既不干活、也不释放"。
    avail = int(res["mem_avail_mb"])
    if avail < SYSTEM_FLOOR_MB:
        by_mem = 0
    else:
        by_mem = max(1, (avail - SYSTEM_FLOOR_MB) // WORKER_MEM_MB)
    chosen = max(0, min(int(requested), by_cpu, by_mem))
    why = "上限 %d · CPU 允许 %d（%d 核，负载 %.2f）· 内存允许 %d（可用 %dMB，留 %dMB 给系统）" % (
        int(requested),
        by_cpu,
        cores,
        float(res["load1"]),
        by_mem,
        int(res["mem_avail_mb"]),
        SYSTEM_FLOOR_MB,
    )
    return chosen, why


# ---------------------------------------------------------------- 对账


def audit(db, *, exact: bool = False, material: str = "", depth: str = "") -> list[dict]:  # noqa: ANN001
    """逐本对账：**存了几窗 / 该有几窗**。

    `exact=False`（默认）用 `material_slices.windows`（向量化时写进去的分母）；
    `exact=True` 读正文重算，并把结果**写回**那一列（缓存自己会校准）。
    """
    stored = dict(
        db.execute(
            select(SliceEmbedding.slice_id, func.count()).group_by(SliceEmbedding.slice_id)
        ).all()
    )
    stmt = select(MaterialSlice, Material).join(Material, Material.id == MaterialSlice.material_id)
    if material:
        stmt = stmt.where(Material.slug == material)
    if depth:
        stmt = stmt.where(Material.depth == depth)
    rows = db.execute(stmt.order_by(Material.slug, MaterialSlice.start_line)).all()

    by_material: dict[str, dict] = {}
    lines_cache: dict[str, tuple[str, ...]] = {}
    for slice_row, mat in rows:
        one = by_material.setdefault(
            mat.slug,
            {
                "material": mat.slug,
                "title": mat.title,
                "depth": mat.depth,
                "source": mat.source_path,
                "slices": 0,
                "windows_planned": 0,
                "windows_stored": 0,
                "slices_short": 0,
                "slices_extra": 0,
                "slices_unknown": 0,
                "file_missing": False,
            },
        )
        one["slices"] += 1
        got = int(stored.get(slice_row.id) or 0)
        one["windows_stored"] += got
        want = int(slice_row.windows or 0)
        if exact:
            try:
                if mat.slug not in lines_cache:
                    lines_cache[mat.slug] = _source_lines(mat.source_path)
                want = len(_windows(slice_row, lines_cache[mat.slug]))
            except OSError:
                one["file_missing"] = True
                want = 0
            if want and want != int(slice_row.windows or 0):
                slice_row.windows = want  # 顺手校准缓存
        one["windows_planned"] += want
        if want == 0:
            # 分母还不知道时，"存了几行"说明不了任何事 —— 别把它算进"多行"，
            # 否则每本老材料都会挂一句吓人的"多行 N 片"（明明没事，只是没校准）。
            one["slices_unknown"] += 1
        elif got < want:
            one["slices_short"] += 1
        elif got > want:
            one["slices_extra"] += 1
    if exact:
        db.commit()

    out = []
    for one in by_material.values():
        missing = max(0, one["windows_planned"] - one["windows_stored"])
        # 顺序要紧：**分母未知** 要排在"半片"前面。`material_slices.windows` 是这一版
        # 才加上的列，老材料的分母是 0 —— 若让它落到"半片"，第一次跑起来会把**每一本**
        # 都报成半片（明明对得上），那种报告比没有报告更坏。
        if one["file_missing"]:
            state = "正文不在本机"
        elif one["windows_planned"] == 0 and one["windows_stored"] == 0:
            state = "没做过"
        elif one["windows_planned"] == 0:
            state = "分母未知（--exact 校准）"
        elif missing == 0 and one["slices_extra"] == 0:
            state = "齐"
        elif one["windows_stored"] == 0:
            state = "没做"
        else:
            state = "半片"
        one["windows_missing"] = missing
        one["state"] = state
        out.append(one)
    out.sort(key=lambda item: (-item["windows_missing"], item["material"]))
    return out


# ---------------------------------------------------------------- 速率与总览


def _rate(db) -> tuple[float, int]:  # noqa: ANN001
    """每个进程大约每秒写多少窗 —— 用**跑完的跑单**量，不是拍的。

    返回 `(每秒窗数, 样本数)`。取中位数：一次被打断的慢跑单不该把 ETA 拖歪。
    """
    rows = db.execute(
        select(EmbedRun.windows_written, EmbedRun.started_at, EmbedRun.ended_at)
        .where(EmbedRun.status == "ok")
        .order_by(EmbedRun.id.desc())
        .limit(8)
    ).all()
    speeds = []
    for written, started, ended in rows:
        if not written or not started or not ended:
            continue
        seconds = (ended - started).total_seconds()
        if seconds > 5:
            speeds.append(written / seconds)
    if not speeds:
        return 2.0, 0  # 还没跑过：给一个保守的默认（实测单进程 0.5~2.7 窗/秒）
    speeds.sort()
    return speeds[len(speeds) // 2], len(speeds)


def _embed_processes() -> list[dict]:
    """正在跑的 `pipeline.embed` 进程 —— **扫进程表，不看账本**。

    两处都要看：账本只记"用这一版代码起的跑"，而现场常有别处起的（手工、上一版代码
    留下的、被中断后重启的）。只看账本会得出"没人在跑"，而 CPU 明明在转 ——
    那正是今晚被问到的那句话（"过了这么久了 cpu 还在转"）。
    """
    out: list[dict] = []
    try:
        text = subprocess.run(
            ["ps", "-eo", "pid,etimes,args"], capture_output=True, text=True, timeout=10
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return out
    for line in text.splitlines()[1:]:
        parts = line.split(None, 2)
        if len(parts) < 3 or "pipeline.embed" not in parts[2]:
            continue
        cmdline = parts[2].split()
        material = ""
        if "--material" in cmdline:
            idx = cmdline.index("--material")
            material = cmdline[idx + 1] if idx + 1 < len(cmdline) else ""
        out.append(
            {"pid": int(parts[0]), "elapsed": int(parts[1]), "material": material or "(全部)"}
        )
    return out


def overview(db, *, slots: int = 0) -> dict:  # noqa: ANN001
    """一条命令看全貌：在跑什么、还差多少、还要多久。"""
    now = datetime.now(UTC)
    running = []
    for row in db.execute(
        select(EmbedRun).where(EmbedRun.status == "running").order_by(EmbedRun.id)
    ).scalars():
        alive = _pid_alive(row.pid, row.host)
        elapsed = (now - row.started_at).total_seconds() if row.started_at else 0.0
        done = int(row.windows_written or 0)
        running.append(
            {
                "material": row.material,
                "pid": row.pid,
                "host": row.host,
                "alive": alive,
                "elapsed": round(elapsed, 1),
                "written": done,
                "planned": int(row.windows_planned or 0),
                "speed": round(done / elapsed, 2) if elapsed > 5 else 0.0,
                "state": "在跑" if alive else "中断（进程没了，跑单还挂着）",
            }
        )
    reports = audit(db)
    todo = [one for one in reports if one["windows_missing"] > 0 or one["state"].startswith("没做")]
    # **分母未知不是"齐"**：`material_slices.windows` 只在新版跑过之后才有值，
    # 老材料全是 0 —— 把它们算成"全部对得上"，就是今晚那个错的翻版（每片有一行 = 算完了）。
    unknown = [one for one in reports if one["state"].startswith("分母未知")]
    missing = sum(one["windows_missing"] for one in todo)
    processes = _embed_processes()
    known = {one["pid"] for one in running}
    for one in processes:
        one["registered"] = one["pid"] in known
    speed, samples = _rate(db)
    # 车道数取**分口里更大的那个**：账本只记这一版代码起的跑，而机器上可能还有旧版/手工
    # 起的进程在干活 —— 拿账本数当车道会把 ETA 说长好几倍（实测 89 分钟 vs 实际 ~30 分钟）。
    lanes = slots or max(1, len(processes) or len([one for one in running if one["alive"]]))
    eta_seconds = missing / max(0.1, speed * lanes) if missing else 0.0
    recent_bad = [
        {"material": row.material, "status": row.status, "note": row.note, "at": str(row.started_at)}
        for row in db.execute(
            select(EmbedRun).where(EmbedRun.status == "failed").order_by(EmbedRun.id.desc()).limit(3)
        ).scalars()
    ]
    return {
        # 库路径从**引擎自己**取：`config.DB_FILE` 是出题流水线那本 state.sqlite3，
        # 与资料库不是同一本（写错过一次，报告里会指着一本"查不到材料"的库）。
        "db": str(get_engine().url),
        "running": running,
        "processes": processes,
        "todo": todo,
        "unknown": unknown,
        "windows_missing": missing,
        "speed_per_process": round(speed, 2),
        "speed_samples": samples,
        "lanes": lanes,
        "eta_minutes": round(eta_seconds / 60, 1),
        "recent_failed": recent_bad,
    }


def _print_overview(data: dict) -> None:  # noqa: ANN001
    print(f"库：{data['db']}")
    # 在跑的**先按 /proc 说**：账本只记这一版代码起的跑，现场常有别的（手工、旧版）。
    # 只说账本会得出"没人在跑"，而 CPU 在转 —— 那种报告会把人带沟里。
    procs = data["processes"]
    if not procs:
        print("正在跑：没有 embed 进程")
    else:
        print("正在跑：%d 个 embed 进程" % len(procs))
        for one in procs:
            tick = "已登记" if one["registered"] else "**不在账本里**（旧版代码或手工起的）"
            print(
                "  · %-24s pid %-6d %6.1f 分钟  %s"
                % (one["material"][:24], one["pid"], one["elapsed"] / 60, tick)
            )
    for one in data["running"]:
        if one["alive"]:
            pct = " %.0f%%" % (100.0 * one["written"] / one["planned"]) if one["planned"] else ""
            print(
                "  · 跑单 %-20s 已写 %5d/%-5d%s  %.2f 窗/秒"
                % (one["material"][:20], one["written"], one["planned"], pct, one["speed"])
            )
        else:
            print(
                "  · 跑单 %-20s **中断**：进程 %d 已经没了，记录还挂着 running —— "
                "那说明它没算完（去 `ops audit` 看缺多少）"
                % (one["material"][:20], one["pid"])
            )
    if not data["todo"]:
        print("待办：没有**已知**缺口的材料。")
    else:
        print(f"待办（{len(data['todo'])} 本，还差 {data['windows_missing']} 窗）：")
        for one in data["todo"][:12]:
            print(
                "  · %-24s %-6s %5d/%-5d 缺 %5d  %s"
                % (
                    one["material"][:24],
                    one["state"],
                    one["windows_stored"],
                    one["windows_planned"],
                    one["windows_missing"],
                    one["title"][:22],
                )
            )
        print(
            "  预计：约 %.0f 分钟（按 %d 个进程 × %.2f 窗/秒%s）"
            % (
                data["eta_minutes"],
                data["lanes"],
                data["speed_per_process"],
                "" if data["speed_samples"] else "，还没跑完过，取默认值",
            )
        )
    if data["unknown"]:
        # 这一条是**这工具最容易出错的地方**，所以明着说：分母未知 ≠ 齐。
        print(
            "另有 %d 本的分母还不知道（`audit --exact` 校准；校准前不能算「齐」）：%s%s"
            % (
                len(data["unknown"]),
                "、".join(one["material"][:18] for one in data["unknown"][:5]),
                "…" if len(data["unknown"]) > 5 else "",
            )
        )
    if data["recent_failed"]:
        print("最近失败：")
        for one in data["recent_failed"]:
            print("  · %-20s %s  %s" % (one["material"][:20], one["at"][:19], one["note"][:60]))


# ---------------------------------------------------------------- 调度


def drive(
    materials: list[str],
    *,
    slots: int = 5,
    batch: int = 8,
    mem_floor_mb: int = SYSTEM_FLOOR_MB,
    log_dir: Path | None = None,
    python: str | None = None,
) -> int:
    """把这几本算完，**按机器余量动态开进程**，结束后自动复验。

    `slots` 是**上限**而不是指标：真正开几个由 `_plan_slots` 每一轮现算（CPU / 负载 /
    可用内存三道闸门都过一遍），于是"机器闲着"与"把 WSL 挤崩"这两头都能避开：

    * 内存紧到给系统留的底线之下，**按住**一个在跑的进程（`SIGSTOP`）而不是杀掉它 ——
      每写完一批就落库，按住不丢活；而放着不管的后果是被 OOM 连锅端（这台机器试过，
      那次连 API 服务一起没了）；
    * 内存回来了再放开（`SIGCONT`），中间留一段迟滞（多出整整一个进程的量才放），
      免得在阈值上反复抖；
    * 每次"该开几个"变了都打印**理由** —— 调度器不该是黑盒（否则出问题时只能靠猜，
      而今晚已经因为"只能靠猜"吃过两次亏）。

    日志落在仓库里（`data/logs/embed/<材料>.log`，带时间戳表头）：以前写 `/tmp`，
    重启就没了，而"重启"正是要查的那种场合。
    """
    root = config.ROOT
    python = python or str(root / "api/.venv/bin/python")
    log_dir = log_dir or (root / "data" / "logs" / "embed")
    log_dir.mkdir(parents=True, exist_ok=True)

    queue = list(materials)
    running: dict[str, subprocess.Popen] = {}

    def start(slug: str) -> None:
        log_path = log_dir / f"{slug}.log"
        handle = log_path.open("a", encoding="utf-8")
        handle.write(
            "\n===== %s 起跑（pid 稍后写入；batch=%d，%s）=====\n"
            % (datetime.now().strftime("%H:%M:%S"), batch, python)
        )
        handle.flush()
        proc = subprocess.Popen(
            [python, "-m", "pipeline.embed", "--material", slug, "--batch", str(batch)],
            cwd=str(root),
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        running[slug] = proc
        print("  %s 起跑 %s（pid %d，日志 %s）" % (datetime.now().strftime("%H:%M:%S"), slug, proc.pid, log_path), flush=True)

    print(
        "调度开始：%d 本待做，上限 %d 个进程，每批 %d 窗（实际开几个按机器余量现算）"
        % (len(queue), slots, batch),
        flush=True,
    )
    last_report = 0.0
    last_plan = -1
    paused: set[str] = set()
    while queue or running:
        for slug, proc in list(running.items()):
            if proc.poll() is not None:
                print(
                    "  %s 结束 %s（退出码 %s）"
                    % (datetime.now().strftime("%H:%M:%S"), slug, proc.returncode),
                    flush=True,
                )
                del running[slug]
                paused.discard(slug)

        res = _resources()
        want, why = _plan_slots(slots, res)
        if want != last_plan:
            print(
                "  %s 该开 %d 个 —— %s" % (datetime.now().strftime("%H:%M:%S"), want, why),
                flush=True,
            )
            last_plan = want

        # 内存压到给系统留的底线之下：**按住**最年轻的那一个，而不是杀掉它 ——
        # 它当前这一批写完就停在系统调用里，一行都不丢；等内存回来再放开。
        if res["mem_avail_mb"] < mem_floor_mb and running and not paused:
            slug = list(running)[-1]
            try:
                os.kill(running[slug].pid, signal.SIGSTOP)
                paused.add(slug)
                print(
                    "  %s 可用内存只剩 %dMB —— 按住 %s（SIGSTOP），缓过来再放开"
                    % (datetime.now().strftime("%H:%M:%S"), res["mem_avail_mb"], slug),
                    flush=True,
                )
            except OSError as exc:
                print("  按住 %s 失败：%s" % (slug, exc), flush=True)
        elif paused and res["mem_avail_mb"] > mem_floor_mb + WORKER_MEM_MB:
            # 迟滞：要多出**整整一个进程**的量才放开，免得在阈值上来回抖。
            for slug in list(paused):
                try:
                    os.kill(running[slug].pid, signal.SIGCONT)
                    print(
                        "  %s 内存回到 %dMB —— 放开 %s"
                        % (datetime.now().strftime("%H:%M:%S"), res["mem_avail_mb"], slug),
                        flush=True,
                    )
                except OSError:
                    pass
                paused.discard(slug)

        while queue and len(running) < want:
            start(queue.pop(0))

        if time.time() - last_report > 60:
            last_report = time.time()
            db = get_session_factory()()
            try:
                data = overview(db, slots=max(1, len(running)))
                print(
                    "  %s 在跑 %d 个（按住 %d）· 还差 %d 窗 · 预计 %.0f 分钟 · 可用内存 %dMB"
                    % (
                        datetime.now().strftime("%H:%M:%S"),
                        len(running) - len(paused),
                        len(paused),
                        data["windows_missing"],
                        data["eta_minutes"],
                        res["mem_avail_mb"],
                    ),
                    flush=True,
                )
            finally:
                db.close()
        time.sleep(5)

    # **结束时复验**：这正是以前缺的那一步 —— 退出码 0 不等于算完。
    print("跑完了，复验：", flush=True)
    db = get_session_factory()()
    try:
        for one in audit(db, exact=True, material=""):
            if one["material"] in materials:
                print(
                    "  · %-24s %5d/%-5d 缺 %5d  %s"
                    % (
                        one["material"][:24],
                        one["windows_stored"],
                        one["windows_planned"],
                        one["windows_missing"],
                        one["state"],
                    ),
                    flush=True,
                )
        data = overview(db, slots=slots)
        print("  合计还差 %d 窗" % data["windows_missing"], flush=True)
    finally:
        db.close()
    return 0


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="pipeline.ops", description="资料处理的运维与观测（总览 / 对账 / 跑单 / 调度）"
    )
    sub = parser.add_subparsers(dest="cmd")

    p_audit = sub.add_parser("audit", help="逐本对账：存了几窗 / 该有几窗")
    p_audit.add_argument("--material", default="", help="只看这一本")
    p_audit.add_argument("--depth", default="", help="只看这一档（检索 / 出题）")
    p_audit.add_argument("--exact", action="store_true", help="读正文重算（权威，稍慢），并校准缓存")
    p_audit.add_argument("--json", action="store_true", help="给程序读")

    p_runs = sub.add_parser("runs", help="跑单账本")
    p_runs.add_argument("--limit", type=int, default=12)
    p_runs.add_argument("--json", action="store_true")

    p_drive = sub.add_parser("drive", help="按机器余量动态开进程，把这批算完并复验")
    p_drive.add_argument("--materials", nargs="+", required=True)
    p_drive.add_argument("--slots", type=int, default=5, help="**上限**（不是指标；实际开几个现算）")
    p_drive.add_argument("--batch", type=int, default=8)
    p_drive.add_argument(
        "--mem-floor", type=int, default=SYSTEM_FLOOR_MB, help="给系统留的可用内存底线（MB）"
    )

    p_plan = sub.add_parser("plan", help="现在这台机器该开几个向量化进程（把决策摆出来看）")
    p_plan.add_argument("--slots", type=int, default=5, help="上限")
    p_plan.add_argument("--json", action="store_true")

    parser.add_argument("--json", action="store_true", help="总览也给程序读")
    parser.add_argument("--slots", type=int, default=0, help="总览里按几个进程估 ETA")
    args = parser.parse_args(argv)

    db = get_session_factory()()
    try:
        if args.cmd == "audit":
            rows = audit(db, exact=args.exact, material=args.material, depth=args.depth)
            if args.json:
                print(json.dumps(rows, ensure_ascii=False, indent=2))
                return 0
            bad = [one for one in rows if one["windows_missing"] or one["state"] != "齐"]
            print("对账（%s）：%d 本，其中对不上的 %d 本" % ("精确" if args.exact else "用缓存", len(rows), len(bad)))
            for one in bad:
                print(
                    "  · %-24s %-6s %5d/%-5d 缺 %5d  半片 %d 片  多行 %d 片  %s"
                    % (
                        one["material"][:24],
                        one["state"],
                        one["windows_stored"],
                        one["windows_planned"],
                        one["windows_missing"],
                        one["slices_short"],
                        one["slices_extra"],
                        one["title"][:20],
                    )
                )
            if not bad:
                print("  全部对得上。")
            return 0
        if args.cmd == "runs":
            rows = db.execute(
                select(EmbedRun).order_by(EmbedRun.id.desc()).limit(max(1, args.limit))
            ).scalars().all()
            out = []
            for row in rows:
                alive = _pid_alive(row.pid, row.host)
                seconds = (
                    (row.ended_at - row.started_at).total_seconds()
                    if row.ended_at and row.started_at
                    else (datetime.now(UTC) - row.started_at).total_seconds()
                )
                out.append(
                    {
                        "id": row.id,
                        "material": row.material,
                        "status": row.status,
                        "alive": alive,
                        "pid": row.pid,
                        "batch": row.batch,
                        "planned": row.windows_planned,
                        "written": row.windows_written,
                        "minutes": round(seconds / 60, 1),
                        "speed": round(row.windows_written / seconds, 2) if seconds > 5 else 0.0,
                        "started": str(row.started_at),
                        "note": row.note,
                    }
                )
            if args.json:
                print(json.dumps(out, ensure_ascii=False, indent=2))
                return 0
            print("%-6s %-22s %-9s %6s %9s %8s %s" % ("id", "材料", "状态", "分钟", "已写/计划", "窗/秒", "备注"))
            for one in out:
                flag = one["status"]
                if one["status"] == "running":
                    flag = "在跑" if one["alive"] else "中断"
                print(
                    "%-6d %-22s %-9s %6.1f %5d/%-5d %8.2f %s"
                    % (
                        one["id"],
                        one["material"][:22],
                        flag,
                        one["minutes"],
                        one["written"],
                        one["planned"],
                        one["speed"],
                        one["note"][:40],
                    )
                )
            return 0
        if args.cmd == "plan":
            res = _resources()
            want, why = _plan_slots(args.slots, res)
            if args.json:
                print(
                    json.dumps(
                        dict(res, slots=want, ceiling=args.slots, why=why), ensure_ascii=False, indent=2
                    )
                )
                return 0
            print(
                "机器：%d 核 · 负载 %.2f · 内存可用 %d / 共 %d MB"
                % (res["cores"], res["load1"], res["mem_avail_mb"], res["mem_total_mb"])
            )
            print("该开：%d 个向量化进程（%s）" % (want, why))
            return 0
        if args.cmd == "drive":
            return drive(
                list(args.materials), slots=args.slots, batch=args.batch, mem_floor_mb=args.mem_floor
            )
        data = overview(db, slots=args.slots)
        if args.json:
            print(json.dumps(data, ensure_ascii=False, indent=2))
        else:
            _print_overview(data)
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
