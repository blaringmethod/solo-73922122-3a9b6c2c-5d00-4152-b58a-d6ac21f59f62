"""生成短码发码/查回专用演示台账（确定性假哈希，前缀冲突场景可控）。

运行：python3 sample-data/generate_code_samples.py
输出：sample-data/ledger_codes_demo.csv

场景覆盖：
- 图一/图二同馆（L01）且前 6 位前缀相同 → 两张图都须让到 7 位码；
- 图三在 L01 独占前缀 → 取最短 6 位码；
- 图四仅在 L02 有一条已撤回记录、无准用 → 可由“含非法摘要的表”之外的流程演示撤回参与查重；
- 图五为非十六进制脏摘要 → 整表发码被拒、原码仍可查（通过专门构造的确认表演示）。
"""
from __future__ import annotations

import csv
import os

HERE = os.path.dirname(os.path.abspath(__file__))
HEADER = ["image_sha256", "library_no", "status", "credit", "summary"]

# 64 位十六进制伪摘要（手工构造，非真实 SHA-256，仅用于演示前缀规则）
IMG_1 = "a" * 64                                   # aaaaaa… 与图二共享前 6 位
IMG_2 = "a" * 6 + "b" + "a" * 57                   # aaaaaab… 第 7 位分叉
IMG_3 = "c" * 64                                   # cccccc… L01 内独占前缀
IMG_4 = "d" * 64                                   # dddddd… L02 仅撤回
IMG_5_DIRTY = "z" * 64                            # 非十六进制脏数据

ROWS = [
    [IMG_1, "L01", "approved", "甲馆藏 / 王晋 摄", "图一：青铜鼎全形"],
    [IMG_2, "L01", "approved", "甲馆藏 / 李远 摄", "图二：壁画供养人"],
    [IMG_3, "L01", "approved", "甲馆藏 / 陈岚 摄", "图三：写经卷首"],
    [IMG_3, "L02", "approved", "乙馆藏 / 周禾 摄", "图三：写经卷首（另一馆准用）"],
    [IMG_4, "L02", "revoked",  "乙馆藏（已撤回）", "图四：漆耳杯（撤回，仍参与查重）"],
    [IMG_5_DIRTY, "L01", "approved", "甲馆藏 / 脏数据", "图五：摘要非十六进制（发码应拒绝）"],
]


def main() -> None:
    path = os.path.join(HERE, "ledger_codes_demo.csv")
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(HEADER)
        writer.writerows(ROWS)
    print("wrote", path, len(ROWS), "rows")
    for tag, digest in (("图一", IMG_1), ("图二", IMG_2), ("图三", IMG_3),
                        ("图四", IMG_4), ("图五脏数据", IMG_5_DIRTY)):
        print(f"  {tag}: {digest}")


if __name__ == "__main__":
    main()
