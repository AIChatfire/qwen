#!/usr/bin/env python3
"""生效 env（`.env`）⇄ 模板（`.env.example`）的同步核验 —— **`.env` 的这道门禁只能用脚本**。

为什么不写成测试：`.env` 不入库（gitignored），CI 上根本没有它 ⇒ 测试只能写成"条件跳过"，
而**跳过等于假绿灯**（skill `env-template-sync` 明令）。所以模板侧的门禁在
`tests/test_env_contract.py`，生效文件侧的核对在这里，发版/换机前手工跑。

它回答三件事（都**不打印任何取值**，只报键名）：
  1. `.env` 有没有**模板没登记**的自创键；
  2. `.env` 里 `# DIFF KEY 理由` 的「刻意差异清单」是否**与实况一致**（漏写 / 过期都算红）；
  3. 两份文件有没有「空值 + 行内注释」（解析器会把注释整段当成值）。

用法：`python scripts/env_sync_check.py [--env .env] [--template .env.example]`
退出码：0 = 一致；1 = 有漂移（报告已打印）。
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def parse(path: Path) -> tuple[dict[str, str], dict[str, str], set[str]]:
    """→ (显式键值, 注释态键值, DIFF 清单键)。行内 ` # 注释` 剥掉；不返回给外部打印。"""
    active: dict[str, str] = {}
    commented: dict[str, str] = {}
    diffs: set[str] = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        m = re.match(r"^#\s*DIFF\s+([A-Z0-9_]+)\s+\S", s)
        if m:
            diffs.add(m.group(1))
            continue
        m = re.match(r"^#\s*([A-Z0-9_]+)=(.*)$", s)
        if m:
            commented[m.group(1)] = re.split(r"\s+#", m.group(2), maxsplit=1)[0].strip()
            continue
        if s and not s.startswith("#") and "=" in s:
            k, v = s.split("=", 1)
            active[k.strip()] = re.split(r"\s+#", v, maxsplit=1)[0].strip()
    return active, commented, diffs


def norm(value: str) -> str:
    low = value.strip().lower()
    if low in ("", "0", "false", "no", "off"):
        return "false" if low else ""
    if low in ("1", "true", "yes", "on"):
        return "true"
    try:
        return str(float(low))
    except ValueError:
        return value.strip()


def main() -> int:
    ap = argparse.ArgumentParser(description="生效 env ⇄ 模板 同步核验")
    ap.add_argument("--env", default=str(REPO / ".env"))
    ap.add_argument("--template", default=str(REPO / ".env.example"))
    args = ap.parse_args()

    env_p, tpl_p = Path(args.env), Path(args.template)
    if not env_p.exists():
        print(f"❌ 找不到生效文件 {env_p}（这份核验必须在有 .env 的机器上跑）")
        return 2

    e_act, e_com, e_diff = parse(env_p)
    t_act, t_com, _ = parse(tpl_p)
    t_keys = set(t_act) | set(t_com)
    problems: list[str] = []

    # ① 自创键
    invented = (set(e_act) | set(e_com)) - t_keys
    if invented:
        problems.append(f"· .env 有模板没登记的键（自创或已改名）：{sorted(invented)}")

    # ② 刻意差异清单 vs 实况（漏写 / 过期都算）
    both = (set(e_act) & set(t_act))
    measured = {k for k in both if norm(e_act[k]) != norm(t_act[k])}
    if measured - e_diff:
        problems.append(f"· 两文件取值不同、但清单没写：{sorted(measured - e_diff)}")
    if e_diff - measured:
        problems.append(f"· 清单写了「有意不同」、实际已一致（清单过期）：{sorted(e_diff - measured)}")
    undocumented_missing = {k for k in t_keys - set(e_act) if k in t_act and k not in e_com}
    if undocumented_missing:
        problems.append(
            f"· 模板显式、.env 既没显式也没注释登记（读者无法判断是故意用默认还是漏了）：{sorted(undocumented_missing)}")

    # ③ 解析陷阱
    for p in (env_p, tpl_p):
        bad = [ln for ln in p.read_text(encoding="utf-8").splitlines()
               if re.match(r"^\s*[A-Z0-9_]+\s*=\s*#", ln)]
        if bad:
            problems.append(f"· {p.name} 有「空值 + 行内注释」（注释会被当成值）：{bad}")

    print("=== 生效 env ⇄ 模板 核验 ===")
    print(f"  {env_p.name}: 显式 {len(e_act)} 键 | 注释态 {len(e_com)} 键 | DIFF 清单 {len(e_diff)} 项")
    print(f"  {tpl_p.name}: 显式 {len(t_act)} 键 | 注释态 {len(t_com)} 键")
    print(f"  刻意差异（实况）：{sorted(measured)}")
    if problems:
        print("\n🔴 发现漂移：")
        print("\n".join(problems))
        return 1
    print("\n✅ 一致：无自创键、差异清单与实况相符、无解析陷阱")
    return 0


if __name__ == "__main__":
    sys.exit(main())
