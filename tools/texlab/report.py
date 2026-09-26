#!/usr/bin/env python3
"""把 `run_cases.js` 的结果排版成人看的样子（`make cases` 用它收尾）。

输入是 runner 返回的那份 JSON（每行：名字 / 期望 / 实际 / 用时 / 是否通过 / 失败时的说明）。
退出码：全过 0，有任何一条不过 1 —— 这样它在 CI/`make` 里能被当检查用。
"""

import json
import sys


def main():
    try:
        rows = json.load(sys.stdin)
    except Exception as err:  # 读不到结果 => 也算失败，别让它悄悄绿
        print("拿不到套件结果：%s" % err)
        return 1

    bad = 0
    print("图表引擎回归套件：%d 条" % len(rows))
    for r in rows:
        flag = "✓" if r["pass"] else "✗"
        if not r["pass"]:
            bad += 1
        line = "  %s %-30s 期望 %-4s 实际 %-4s %6dms" % (
            flag,
            r["name"],
            r["expect"],
            r["got"],
            r["ms"],
        )
        print(line)
        if not r["pass"] and r["why"]:
            print("      → %s" % r["why"].replace("\n", " ")[:170])
    print("通过 %d / %d" % (len(rows) - bad, len(rows)))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
