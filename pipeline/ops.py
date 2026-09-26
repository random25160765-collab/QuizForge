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
from app.local_embed import active_provider, cuda_lib_path  # noqa: E402
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
    # CPU 车道：核数整除 8（16 核 → 2）。
    #
    # 原先取整除 5（3 条），吞吐确实更高，但**这台机器上还有 IDE 与浏览器**：实测 3 条
    # 车道时负载 24~27，IDE 明显发卡（用户："CPU 负载太高的话 ide 很吃力"）。少一条换来
    # 的手感比那点吞吐值 —— 而且那部分正该由 GPU 车道去补，它不抢 CPU。
    # 负载只当**收敛的旋钮**，不当开关：这个数字里有一大半是 onnxruntime 线程池在空转
    # （3 条就能到 24），拿它一刀切会把并发砍成 1、机器反而闲着。
    by_cpu = max(0, cores // 8)
    if load1 > cores * 1.5:
        by_cpu = min(by_cpu, 1)
    if load1 > cores * 2.5:
        by_cpu = 0
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


#: 显存底线：GPU 车道开工前要求至少这么多空闲显存（MB）。模型 fp16 约 1.2G、
#: 加上激活与 ORT 的池子，1.5G 是实测不炸的下限。
GPU_MEM_FLOOR_MB = 1500


def _lanes(
    requested: int, res: dict, gpu: dict, *, onnx: str = ""
) -> tuple[list[tuple[str, dict]], str]:
    """这一刻该开哪些**车道**：若干 CPU 车道 + 最多一条 GPU 车道。

    为什么两边一起上：GPU 哪怕只比 CPU 快一点点（实测 int8 的图在 CUDA 上 717ms/条，
    CPU 822ms/条），它占的是**另一块芯片** —— 与 CPU 车道不抢核，等于白多一份算力。
    两条前提：显存有余量、且没人在用这张卡（利用率低）；否则那一个进程会把两边都拖慢。

    `onnx` 指定模型变体时会**发到每一条车道** —— 不能让 CPU 跑 int8、GPU 跑 fp16：
    两种向量不可混用，而 `embed` 只按名字判新旧，于是它们会互相把对方判成过期、
    来回重算（那是最坏的一种"活着但在打架"）。
    """
    cpu_lanes, cpu_why = _plan_slots(requested, res)
    lanes: list[tuple[str, dict]] = []
    for _ in range(cpu_lanes):
        lanes.append(("cpu", dict(QF_EMBED_ONNX=onnx) if onnx else {}))
    why = cpu_why
    if gpu.get("ok"):
        if gpu["mem_free_mb"] < GPU_MEM_FLOOR_MB:
            why += " · GPU 不开：显存只剩 %.0fMB（要留 %dMB）" % (
                gpu["mem_free_mb"],
                GPU_MEM_FLOOR_MB,
            )
        elif gpu["util"] > 80:
            why += " · GPU 不开：卡已被占（利用率 %.0f%%）" % gpu["util"]
        else:
            env = {"QF_ORT_PROVIDER": "CUDAExecutionProvider"}
            if onnx:
                env["QF_EMBED_ONNX"] = onnx
            lanes.append(("gpu", env))
            why += " · GPU 加一条（显存余 %.0fMB，利用率 %.0f%%）" % (
                gpu["mem_free_mb"],
                gpu["util"],
            )
    else:
        why += " · GPU 不可用（%s）" % str(gpu.get("why") or "未知")
    return lanes, why


def _gpu() -> dict:
    """这张卡现在什么样（没有卡、或没装驱动时返回 `{"ok": False}`）。

    为什么运维层必须看它：GPU 车道是**独占一块芯片**的 —— 卡被别的活占着（浏览器、
    别的推理）时再压一个进程进去，两边都会变慢，而那种慢从 CPU 侧看不出来。
    """
    try:
        out = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return {"ok": False, "why": "没有 nvidia-smi（无卡或没装驱动）"}
    if not out:
        return {"ok": False, "why": "nvidia-smi 没给出信息"}
    first = out.splitlines()[0].split(",")
    if len(first) < 6:
        return {"ok": False, "why": "nvidia-smi 的输出看不懂：" + out[:60]}

    def num(text: str) -> float:
        try:
            return float(text.strip())
        except ValueError:
            return 0.0

    used, total = num(first[2]), num(first[3])
    return {
        "ok": True,
        "name": first[0].strip(),
        "util": num(first[1]),
        "mem_used_mb": used,
        "mem_total_mb": total,
        "mem_free_mb": max(0.0, total - used),
        "temp_c": num(first[4]),
        "power_w": num(first[5]),
    }


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
                # 跑在哪块芯片上（开跑时写进账本的，见 `embed._run`）—— 进度屏靠它区分
                # gpu / cpu 车道，不然两条车道在屏幕上长得一模一样。
                "device": str(row.device or ""),
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
    # 速率**优先用正在跑的那些**（它们是真的在推进），只有没活口时才退回历史均值。
    # 实测差 8 倍：历史均值 4.56 窗/秒，而当时那条 GPU 车道实际 37.8 窗/秒 ——
    # ETA 说 81 分钟，其实 10 分钟。**ETA 说错比不说更糟**，所以宁可不用它。
    live = sum(float(one["speed"]) for one in running if one["alive"])
    speed, samples = _rate(db)
    # 车道数取**两处里更大的那个**：账本只记这一版代码起的跑，而机器上可能还有旧版/手工
    # 起的进程在干活 —— 拿账本数当车道会把 ETA 说长好几倍（实测 89 分钟 vs 实际 ~30 分钟）。
    lanes = slots or max(1, len(processes) or len([one for one in running if one["alive"]]))
    if live > 0:
        speed = live / max(1, lanes)
        eta_seconds = missing / max(0.1, live) if missing else 0.0
    else:
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
        "gpu": _gpu(),
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
    gpu = data.get("gpu") or {}
    print(
        "卡：%s"
        % (
            "%s · 利用率 %.0f%% · 显存 %.0f/%.0fMB（余 %.0f）· %0.f度 · %.0fW"
            % (
                gpu.get("name"),
                gpu.get("util", 0),
                gpu.get("mem_used_mb", 0),
                gpu.get("mem_total_mb", 0),
                gpu.get("mem_free_mb", 0),
                gpu.get("temp_c", 0),
                gpu.get("power_w", 0),
            )
            if gpu.get("ok")
            else "没有可用 GPU（%s）" % gpu.get("why")
        )
    )
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
    onnx: str = "",
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
    lane_of: dict[str, str] = {}

    def start(slug: str, lane: str, lane_env: dict) -> None:
        """起一个进程做这本材料。`lane` 进日志与账本，`lane_env` 是这条车道的专属环境。"""
        key = "%s:%s" % (lane, slug)
        log_path = log_dir / f"{slug}.log"
        handle = log_path.open("a", encoding="utf-8")
        handle.write(
            "\n===== %s 起跑（车道 %s；batch=%d，%s）=====\n"
            % (datetime.now().strftime("%H:%M:%S"), lane, batch, python)
        )
        handle.flush()
        env = dict(os.environ)
        # **把 pip 装的 CUDA / cuDNN 库放进子进程的搜索路径**：`onnxruntime-gpu` 是动态链
        # `libcudart.so.12` / `libcudnn.so.9` 的，而它们在 `site-packages/nvidia/*/lib`
        # —— 默认不在搜索路径上，找不到就**静默**退回 CPU（不报错，只是慢十倍）。
        # `LD_LIBRARY_PATH` 必须在**进程启动前**就位（glibc 只在启动时读它），
        # 所以这里给子进程带环境，而不是在 python 里改 os.environ。
        libs = cuda_lib_path()
        if libs:
            old = env.get("LD_LIBRARY_PATH") or ""
            env["LD_LIBRARY_PATH"] = libs + (":" + old if old else "")
        env.update(lane_env or {})
        proc = subprocess.Popen(
            [python, "-m", "pipeline.embed", "--material", slug, "--batch", str(batch)],
            cwd=str(root),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
        )
        running[key] = proc
        lane_of[key] = lane
        print(
            "  %s 起跑 %s ［%s 车道，pid %d，日志 %s］"
            % (datetime.now().strftime("%H:%M:%S"), slug, lane, proc.pid, log_path),
            flush=True,
        )

    print(
        "调度开始：%d 本待做，上限 %d 条 CPU 车道，每批 %d 窗"
        "（开几条、要不要 GPU 车道，按机器余量现算）" % (len(queue), slots, batch),
        flush=True,
    )
    last_report = 0.0
    last_plan = ""
    paused: set[str] = set()
    while queue or running:
        for key, proc in list(running.items()):
            if proc.poll() is not None:
                print(
                    "  %s 结束 %s（退出码 %s）"
                    % (datetime.now().strftime("%H:%M:%S"), key, proc.returncode),
                    flush=True,
                )
                del running[key]
                lane_of.pop(key, None)
                paused.discard(key)

        res = _resources()
        gpu = _gpu()
        lanes, why = _lanes(slots, res, gpu, onnx=onnx)
        signature = ",".join(label for label, _env in lanes)
        if signature != last_plan:
            print(
                "  %s 车道：%s —— %s"
                % (datetime.now().strftime("%H:%M:%S"), signature or "（无）", why),
                flush=True,
            )
            last_plan = signature

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

        # 按车道派活：每条车道同时只做一本，材料做完再接下一本 ——
        # 同一本材料**绝不给两条车道**（两条进程对同一批切片先删后插会互相踩）。
        wanted: dict[str, int] = {}
        for label, _env in lanes:
            wanted[label] = wanted.get(label, 0) + 1
        busy: dict[str, int] = {}
        for label in lane_of.values():
            busy[label] = busy.get(label, 0) + 1
        for label, lane_env in lanes:
            if not queue:
                break
            if busy.get(label, 0) >= wanted.get(label, 0):
                continue
            busy[label] = busy.get(label, 0) + 1
            start(queue.pop(0), label, lane_env)

        if time.time() - last_report > 60:
            last_report = time.time()
            db = get_session_factory()()
            try:
                data = overview(db, slots=max(1, len(running)))
                lanes_now: dict[str, int] = {}
                for label in lane_of.values():
                    lanes_now[label] = lanes_now.get(label, 0) + 1
                gpu_text = (
                    "GPU %.0f%% · 显存 %.0f/%.0fMB" % (gpu["util"], gpu["mem_used_mb"], gpu["mem_total_mb"])
                    if gpu.get("ok")
                    else "无 GPU"
                )
                print(
                    "  %s 车道 %s · 还差 %d 窗 · 预计 %.0f 分钟 · 可用内存 %dMB · %s"
                    % (
                        datetime.now().strftime("%H:%M:%S"),
                        ",".join("%s×%d" % (k, v) for k, v in sorted(lanes_now.items())) or "（无）",
                        data["windows_missing"],
                        data["eta_minutes"],
                        res["mem_avail_mb"],
                        gpu_text,
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


def bench(*, texts: int = 24, batch: int = 8) -> int:
    """量**这套配置**每条窗口多少毫秒，以及实际用的是哪个 provider。

    为什么把它做成命令而不是"临场写一段"：GPU 这条路上最典型的事故是**以为在跑 GPU、
    其实静默退回 CPU** —— 它不报错，只慢十倍。判据只有量：

        python -m pipeline.ops bench                                      # 当前配置
        QF_ORT_PROVIDER=CPUExecutionProvider python -m pipeline.ops bench # 拿 CPU 对照

    样本取自真实材料（默认 `book.md`），按约 1200 字切 —— 与 `embed.py` 的窗口同量级。
    """
    import time as _time  # noqa: PLC0415

    from app import local_embed  # noqa: PLC0415
    from .embed import WINDOW_CHARS  # noqa: PLC0415

    # `LD_LIBRARY_PATH` 只在**进程启动时**被动态链接器读一次 —— python 起来之后再设没用。
    # 所以这里够不着 CUDA 库时，就把自己**重新起一遍**（带环境、带一个标记防止递归）。
    # 这样"量 GPU 到底快了多少"是一句 `ops bench` 的事，不必让人先记住一条环境变量。
    libs = cuda_lib_path()
    if libs and not os.environ.get("QF_BENCH_REEXEC"):
        child = dict(os.environ)
        old = child.get("LD_LIBRARY_PATH") or ""
        child["LD_LIBRARY_PATH"] = libs + (":" + old if old else "")
        child["QF_BENCH_REEXEC"] = "1"
        print("（带上 CUDA 库路径重起一次：%s）" % libs)
        return subprocess.call(
            [sys.executable, "-m", "pipeline.ops", "bench", "--texts", str(texts), "--batch", str(batch)],
            cwd=str(config.ROOT),
            env=child,
        )

    res = _resources()
    sample = config.ROOT / "data" / "library" / ".text" / "book.md"
    if not sample.is_file():  # 换一本存在的
        others = sorted((sample.parent).glob("*.md"))
        if not others:
            print("没有可用的样本材料（data/library/.text/*.md 都是空的）")
            return 1
        sample = others[0]
    lines = sample.read_text(encoding="utf-8", errors="replace").splitlines()
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for line in lines:
        current.append(line)
        size += len(line) + 1
        if size >= WINDOW_CHARS:
            chunks.append("\n".join(current))
            current, size = [], 0
        if len(chunks) >= texts:
            break
    if not chunks:
        print("样本太短，切不出窗口")
        return 1

    print(
        "机器：%d 核 · 负载 %.2f · 内存可用 %dMB · 样本 %s（%d 段，平均 %d 字）"
        % (res["cores"], res["load1"], res["mem_avail_mb"], sample.name, len(chunks),
           sum(map(len, chunks)) // len(chunks))
    )
    ok, detail = local_embed.ensure(log=lambda message: print("   ", message))
    if not ok:
        print("  " + detail)
        return 1
    import onnxruntime  # noqa: PLC0415

    print("  onnxruntime %s · 可用 provider %s" % (onnxruntime.__version__, onnxruntime.get_available_providers()))
    started = _time.perf_counter()
    done = 0
    for offset in range(0, len(chunks), batch):
        got = local_embed.embed(chunks[offset : offset + batch], kind="passage")
        done += len(got)
    elapsed = _time.perf_counter() - started
    print(
        "  实际用的 provider：**%s** · %d 条 / %.1f 秒 = **%.0f ms 每条**"
        % (local_embed.active_provider() or "(还没建会话)", done, elapsed, elapsed / max(1, done) * 1000)
    )
    return 0


def _bar(done: int, total: int, width: int = 28) -> str:
    """进度条。`total` 未知时给一排问号 —— 不猜、也不假装是 0%。"""
    if total <= 0:
        return "[" + "?" * width + "]      ？"
    filled = max(0, min(width, int(round(width * min(1.0, done / total)))))
    return "[%s%s] %5.1f%%" % ("█" * filled, "░" * (width - filled), 100.0 * done / total)


def _frame(data: dict) -> list[str]:
    """这一帧的内容（每行一个字符串）。

    把"算什么"与"怎么画"分开：终端里要**原地重画**（光标上移、覆盖上一帧），
    输出被接走时要**一行一帧** —— 两种画法共用同一份内容，不然迟早各写一遍、慢慢分家。
    """
    import shutil as _shutil  # noqa: PLC0415

    cols = _shutil.get_terminal_size((100, 30)).columns
    width = max(12, min(40, cols - 46))
    gpu = data.get("gpu") or {}
    out: list[str] = [
        "quizforge · 资料向量化进度                          %s"
        % datetime.now().strftime("%H:%M:%S"),
        "",
    ]
    known_done = sum(one["windows_stored"] for one in data["todo"])
    known_plan = sum(one["windows_planned"] for one in data["todo"])
    if known_plan:
        out.append(
            "整体  %s   %d/%d 窗   还差 %d"
            % (_bar(known_done, known_plan, width), known_done, known_plan, data["windows_missing"])
        )
    else:
        out.append("整体  %s   没有已知缺口" % _bar(0, 0, width))
    out.append("")
    # 只画**还活着**的跑单：一屏里塞十几条"中断"的旧记录，真正的进度就被淹了
    #（实测第一版就是这样）。中断的那些收成一行 —— 仍然要说，但不该占屏。
    runs = [one for one in data["running"] if one.get("alive") is not False]
    stale = [one for one in data["running"] if one.get("alive") is False]
    procs = data["processes"]
    if not procs and not runs:
        out.append("正在跑：没有 embed 进程")
    else:
        out.append("正在跑（%d 个进程）" % len(procs))
        for one in runs:
            done, plan = int(one["written"]), int(one["planned"])
            lane = "gpu" if "CUDA" in (one.get("device") or "") else "cpu"
            out.append(
                "  %-3s %-22s %8.1f 窗/秒  %s  %5d/%-6d"
                % (lane, one["material"][:22], one["speed"], _bar(done, plan, width), done, plan)
            )
        for one in procs:
            if one["pid"] not in {r["pid"] for r in runs}:
                out.append("  --  %-22s （旧版/手工起的进程，没在账本里）" % one["material"][:22])
        if stale:
            out.append(
                "  ·  另有 %d 条跑单挂着 running、进程早已不在（中断）：%s…"
                % (len(stale), "、".join(one["material"][:14] for one in stale[:3]))
            )
    out.append("")
    if gpu.get("ok"):
        out.append(
            "卡：%s · %.0f%% · 显存 %.0f/%.0fMB · %.0fW · %.0f度"
            % (
                gpu.get("name"),
                gpu.get("util", 0),
                gpu.get("mem_used_mb", 0),
                gpu.get("mem_total_mb", 0),
                gpu.get("power_w", 0),
                gpu.get("temp_c", 0),
            )
        )
    else:
        out.append("卡：没有可用 GPU（%s）" % gpu.get("why"))
    out.append(
        "机器：负载 %.2f · 内存可用 %dMB · 速率样本 %d"
        % (data.get("load1", 0.0), data.get("mem_avail_mb", 0), data["speed_samples"])
    )
    if data["windows_missing"]:
        out.append(
            "预计：还差约 %.0f 分钟（按 %d 条车道 × %.2f 窗/秒）"
            % (data["eta_minutes"], data["lanes"], data["speed_per_process"])
        )
    else:
        out.append("预计：没有缺口了")
    if data["unknown"]:
        out.append("另有 %d 本分母未知（`ops audit --exact` 校准）" % len(data["unknown"]))
    out.append("")
    out.append("（Ctrl-C 退出；后台的活不受影响）")
    return out


def _one_line(data: dict) -> str:
    """非终端时的紧凑一行 —— 日志里要的是"一行一帧"，不是几百行整屏。"""
    known_done = sum(one["windows_stored"] for one in data["todo"])
    known_plan = sum(one["windows_planned"] for one in data["todo"])
    parts = [
        "%5.1f%%" % (100.0 * known_done / known_plan if known_plan else 0.0),
        "%d/%d 窗" % (known_done, known_plan),
    ]
    run = next((one for one in data["running"] if one.get("alive") is not False), None)
    if run is not None:
        lane = "gpu" if "CUDA" in (run.get("device") or "") else "cpu"
        parts.append("%s %s %.1f 窗/秒" % (lane, str(run["material"])[:20], run["speed"]))
    if data["windows_missing"]:
        parts.append("还差 %d 窗 · 约 %.0f 分钟" % (data["windows_missing"], data["eta_minutes"]))
    gpu = data.get("gpu") or {}
    if gpu.get("ok"):
        parts.append("GPU %.0f%% %.0fW" % (gpu.get("util", 0), gpu.get("power_w", 0)))
    return "%s  %s" % (datetime.now().strftime("%H:%M:%S"), " · ".join(parts))


def _draw(lines: list[str], *, prev: int) -> int:
    """**在原先的基础上重画**：光标上移 `prev` 行，逐行覆盖、清到行尾。

    为什么不用 `\\033[2J` 清整屏：那会把上面的历史一起抹掉 —— 想往上翻看刚才的进度就没了。
    帧高会变（车道增减、材料名长短不同），所以必须记住上一帧占了几行。
    """
    if prev:
        sys.stdout.write("\033[%dA" % prev)
    sys.stdout.write("".join(line + "\033[K\n" for line in lines))
    sys.stdout.flush()
    return len(lines)


def watch(*, interval: float = 3.0, once: bool = False, slots: int = 0, mode: str = "auto") -> int:
    """**前台**看进度：**在原先那一屏上刷新**，没有活了自己退出。

    为什么要有它、而不只是让 `ops` 输出一次：向量化是分钟到小时级的事，盯着它的人
    要的是"到哪儿了、还要多久" —— 一次性输出只会让人反复敲命令，而每敲一次就多一段
    随手写的脚本（那正是这个模块存在的理由）。`make watch` 就是它的快捷键。

    `mode`：`auto`（终端里原地重画、输出被接走时一行一帧）、`ansi`、`plain`。
    """
    if mode == "auto":
        mode = "ansi" if sys.stdout.isatty() else "plain"
    drawn = 0
    try:
        while True:
            db = get_session_factory()()
            try:
                data = overview(db, slots=slots)
                res = _resources()
                data["load1"] = res["load1"]
                data["mem_avail_mb"] = res["mem_avail_mb"]
            finally:
                db.close()
            if mode == "ansi":
                drawn = _draw(_frame(data), prev=drawn)
            else:
                print(_one_line(data), flush=True)
            if once or not (data["running"] or data["processes"]):
                break
            time.sleep(max(0.5, interval))
    except KeyboardInterrupt:
        print("\n（退出了；后台的活不受影响）")
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
    p_drive.add_argument("--materials", nargs="*", default=[], help="要算的材料（slug）")
    p_drive.add_argument(
        "--all",
        action="store_true",
        help="库里所有有切片的材料 —— 换了模型/精度要整库重建就用它",
    )
    p_drive.add_argument("--slots", type=int, default=5, help="**上限**（不是指标；实际开几个现算）")
    p_drive.add_argument("--batch", type=int, default=8)
    p_drive.add_argument(
        "--onnx",
        default="",
        help="用哪个 ONNX 变体（例如 model_fp16.onnx）—— 会发给**所有**车道（混精度=互相判过期）",
    )
    p_drive.add_argument(
        "--mem-floor", type=int, default=SYSTEM_FLOOR_MB, help="给系统留的可用内存底线（MB）"
    )

    p_plan = sub.add_parser("plan", help="现在这台机器该开几个向量化进程（把决策摆出来看）")
    p_plan.add_argument("--slots", type=int, default=5, help="上限")
    p_plan.add_argument("--json", action="store_true")

    p_bench = sub.add_parser("bench", help="量一遍：每条窗口多少毫秒、实际用的哪个 provider")
    p_bench.add_argument("--texts", type=int, default=24, help="取多少段真实正文当样本")
    p_bench.add_argument("--batch", type=int, default=8, help="每批多少条")

    p_watch = sub.add_parser("watch", help="前台看进度（刷新一屏 + 进度条，没活了自己退出）")
    p_watch.add_argument("--interval", type=float, default=3.0, help="几秒刷一次（默认 3）")
    p_watch.add_argument("--once", action="store_true", help="只画一帧就退出")
    p_watch.add_argument("--slots", type=int, default=0, help="按几条车道估 ETA（0 = 按实际在跑的）")

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

            # **库里有几种"模型标签"** —— 那一列（`slice_embeddings.model`）是
            # "这些向量是不是同一套"的**唯一凭据**，而检索**不按它过滤**（`semantic`
            # 只有 join、没有 where）：一旦库里混着两种模型，它们会被一起算分，
            # 余弦值毫无意义、却不报错。所以这里每次都报，多于一种还单独警告。
            from app.local_embed import model_label  # noqa: PLC0415

            want = model_label()
            groups = db.execute(
                select(SliceEmbedding.model, func.count()).group_by(SliceEmbedding.model)
            ).all()
            print("向量分布（当前模型：%s）：" % want)
            for model, count in sorted(groups, key=lambda one: -int(one[1])):
                current = str(model) == want
                print(
                    "  · %-46s %7d 行%s"
                    % (str(model)[:46], int(count), "（当前）" if current else "  ← **不是当前模型**")
                )
            if len(groups) > 1 or (groups and str(groups[0][0]) != want):
                print(
                    "  ⚠ 库里有不属于当前模型的向量。跑一次 `pipeline.embed` 会按片重算它们"
                    "（`_pending` 判的是 stale_model），或者整库重建：`ops drive --all`。"
                )
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
                        "device": str(row.device or ""),
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
            print(
                "%-5s %-20s %-6s %-26s %6s %11s %8s %s"
                % ("id", "材料", "状态", "跑在哪块芯片", "分钟", "已写/计划", "窗/秒", "备注")
            )
            for one in out:
                flag = one["status"]
                if one["status"] == "running":
                    flag = "在跑" if one["alive"] else "中断"
                print(
                    "%-5d %-20s %-6s %-26s %6.1f %5d/%-5d %8.2f %s"
                    % (
                        one["id"],
                        one["material"][:20],
                        flag,
                        (one["device"] or "（还没写）")[:26],
                        one["minutes"],
                        one["written"],
                        one["planned"],
                        one["speed"],
                        one["note"][:34],
                    )
                )
            return 0
        if args.cmd == "watch":
            return watch(interval=args.interval, once=args.once, slots=args.slots)
        if args.cmd == "bench":
            return bench(texts=args.texts, batch=args.batch)
        if args.cmd == "plan":
            res = _resources()
            gpu = _gpu()
            lanes, why = _lanes(args.slots, res, gpu)
            if args.json:
                print(
                    json.dumps(
                        dict(
                            res,
                            gpu=gpu,
                            ceiling=args.slots,
                            cpu_lanes=_plan_slots(args.slots, res)[0],
                            lanes=[label for label, _env in lanes],
                            why=why,
                        ),
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            print(
                "机器：%d 核 · 负载 %.2f · 内存可用 %d / 共 %d MB"
                % (res["cores"], res["load1"], res["mem_avail_mb"], res["mem_total_mb"])
            )
            print(
                "卡：%s"
                % (
                    "%s · 利用率 %.0f%% · 显存余 %.0fMB · %.0f度 · %.0fW"
                    % (
                        gpu.get("name"),
                        gpu.get("util", 0),
                        gpu.get("mem_free_mb", 0),
                        gpu.get("temp_c", 0),
                        gpu.get("power_w", 0),
                    )
                    if gpu.get("ok")
                    else "没有可用 GPU（%s）" % gpu.get("why")
                )
            )
            print("车道：%s" % (",".join(label for label, _env in lanes) or "（无）"))
            print("理由：%s" % why)
            return 0
        if args.cmd == "drive":
            slugs = [str(one) for one in (args.materials or [])]
            if args.all:
                # 整库重建：**所有有切片的材料**（换模型或换精度之后就该这么来一次，
                # 否则库里会同时留着两种不可混用的向量）。
                slugs = [
                    str(one)
                    for one in db.execute(
                        select(Material.slug)
                        .where(Material.id.in_(select(MaterialSlice.material_id)))
                        .order_by(Material.slug)
                    )
                    .scalars()
                    .all()
                ]
                print("整库：%d 份材料" % len(slugs), flush=True)
            if not slugs:
                print("没给材料、也没给 --all，不知道要算什么。", flush=True)
                return 2
            return drive(
                slugs,
                slots=args.slots,
                batch=args.batch,
                mem_floor_mb=args.mem_floor,
                onnx=str(args.onnx or ""),
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
