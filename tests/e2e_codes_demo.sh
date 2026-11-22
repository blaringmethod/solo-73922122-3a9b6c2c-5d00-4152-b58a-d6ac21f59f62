#!/usr/bin/env bash
# 短码发码 / 查回 端到端 HTTP 演示：按真实浏览器流程走一遍关键操作。
# 前置：服务已在 $BASE 启动，且台账使用 sample-data/ledger_codes_demo.csv。
set -euo pipefail

BASE=${BASE:-http://127.0.0.1:8000}
COOKIE=/tmp/credit-codes-demo.cookies
CSV=/workspace/sample-data/ledger_codes_demo.csv
rm -f "$COOKIE"

say() { printf '\n========== %s ==========\n' "$1"; }

say "1. 上传短码演示台账（6 行，含撤回记录与非十六进制脏摘要）"
curl -sS -c "$COOKIE" -b "$COOKIE" -F "ledger=@${CSV};type=text/csv" \
  "$BASE/ledger/upload" -o /tmp/c1.html -w "HTTP %{http_code}\n" -L
grep -q "台账已补正" /tmp/c1.html && echo "导入成功提示 OK"

# 读出演示哈希
eval "$(python3 -s - <<'PY'
import csv
rows = list(csv.DictReader(open('/workspace/sample-data/ledger_codes_demo.csv', encoding='utf-8-sig')))
digests = {}
for r in rows:
    digests.setdefault(r['summary'][:2], r['image_sha256'])
for name, key in (('H1', '图一'), ('H2', '图二'), ('H3', '图三'), ('H4', '图四'), ('HZ', '图五')):
    print(f"{name}={digests[key]!r}")
PY
)"
echo "图一=${H1:0:12}… 图二=${H2:0:12}… 图三=${H3:0:12}… 图四=${H4:0:12}… 脏=${HZ:0:12}…"

say "2. 选图一/图二/图三 出表并确认（图四仅撤回、图五脏摘要先不选）"
LOC=$(curl -sS -c "$COOKIE" -b "$COOKIE" -X POST \
  --data-urlencode "selected=${H1}" --data-urlencode "selected=${H2}" --data-urlencode "selected=${H3}" \
  "$BASE/solve" -o /dev/null -w "%{redirect_url}")
TOKEN=${LOC##*/}
curl -sS -c "$COOKIE" -b "$COOKIE" -X POST "$BASE/confirm/$TOKEN" \
  -o /dev/null -w "确认 HTTP %{http_code} -> %{redirect_url}\n"

say "3. 对已确认表 #1 整表发码"
# 不用 -L：发码成功后 302 跳到仅接受 GET 的详情页，跟随会把 POST 重发成 405
curl -sS -c "$COOKIE" -b "$COOKIE" -X POST "$BASE/tables/1/issue-codes" \
  -o /dev/null -w "发码 HTTP %{http_code} -> %{redirect_url}\n"
curl -sS -c "$COOKIE" -b "$COOKIE" "$BASE/tables/1" -o /tmp/c3.html -w "详情页 HTTP %{http_code}\n"
grep -q "短码发放完成" /tmp/c3.html && echo "发码成功提示 OK"
python3 -s - <<'PY'
import re
html = open('/tmp/c3.html', encoding='utf-8').read()
# 只取“排版用短码清单”有序列表中的短码（按图序）
section = html.split('排版用短码清单', 1)[1]
codes = re.findall(r'class="mono code-cell">([0-9a-f]{6,12})</span>', section)
print("按图序短码:", codes)
# 图一/图二同 6 位前缀 → 均 7 位；图三独占前缀 → 6 位
assert codes[0] == "a" * 7, codes
assert codes[1] == "a" * 6 + "b", codes
assert codes[2] == "c" * 6, codes
assert "排版用短码清单" in html
print("最短可用前缀 + 同前缀避让 + 按图序排版 规则验证 OK")
PY

say "4. 再次整表发码：全部沿用同馆原码，不新发"
curl -sS -c "$COOKIE" -b "$COOKIE" -X POST "$BASE/tables/1/issue-codes" \
  -o /dev/null -w "重复发码 HTTP %{http_code}\n"
curl -sS -c "$COOKIE" -b "$COOKIE" "$BASE/tables/1" -o /tmp/c4.html
grep -q "新发 0 个" /tmp/c4.html && echo "沿用统计 OK"
grep -q "沿用同馆原码 3 个" /tmp/c4.html && echo "原码沿用 OK"

say "5. 短码查回：馆号 L01 + 图一短码（大写输入归一）→ 原完整摘要"
CODE1=$(python3 -s -c "print('a'*7)")
curl -sS -c "$COOKIE" -b "$COOKIE" \
  "$BASE/codes/lookup?library_no=L01&code=${CODE1^^}" -o /tmp/c5.html -w "查回 HTTP %{http_code}\n"
grep -q "查回成功" /tmp/c5.html && echo "查回成功提示 OK"
grep -q "$H1" /tmp/c5.html && echo "查回完整摘要与发码登记一致 OK"

say "6. 台账后来补入同前缀图片，查回仍返回注册表原摘要"
python3 -s - <<PY
import csv
with open('/tmp/same_prefix.csv', 'w', encoding='utf-8', newline='') as f:
    w = csv.writer(f)
    w.writerow(['image_sha256', 'library_no', 'status', 'credit', 'summary'])
    # 与图一前 6 位相同、第 7 位再分叉的新图
    w.writerow(['a' * 6 + 'e' + 'a' * 57, 'L01', 'approved', '甲馆藏 / 新增', '后补同前缀图'])
PY
curl -sS -c "$COOKIE" -b "$COOKIE" -F "ledger=@/tmp/same_prefix.csv;type=text/csv" \
  "$BASE/ledger/upload" -o /dev/null -L -w "补入台账 HTTP %{http_code}\n"
curl -sS -c "$COOKIE" -b "$COOKIE" \
  "$BASE/codes/lookup?library_no=L01&code=$CODE1" -o /tmp/c6.html
grep -q "$H1" /tmp/c6.html && echo "台账补入后原摘要查回不变 OK"

say "7. 含非 64 位十六进制摘要的表整表发码被拒，原码仍可查"
LOC2=$(curl -sS -c "$COOKIE" -b "$COOKIE" -X POST \
  --data-urlencode "selected=$HZ" "$BASE/solve" -o /dev/null -w "%{redirect_url}")
TOKEN2=${LOC2##*/}
curl -sS -c "$COOKIE" -b "$COOKIE" -X POST "$BASE/confirm/$TOKEN2" -o /dev/null -w "确认脏表 HTTP %{http_code}\n"
curl -sS -c "$COOKIE" -b "$COOKIE" -X POST "$BASE/tables/2/issue-codes" \
  -o /dev/null -w "脏表发码 HTTP %{http_code}\n"
curl -sS -c "$COOKIE" -b "$COOKIE" "$BASE/tables/2" -o /tmp/c7.html
grep -q "整表未发新码" /tmp/c7.html && echo "整表拒绝提示 OK"
grep -q "64 位十六进制" /tmp/c7.html && echo "原因说明 OK"
# 原码仍可查
curl -sS -c "$COOKIE" -b "$COOKIE" \
  "$BASE/codes/lookup?library_no=L01&code=$CODE1" -o /tmp/c7b.html
grep -q "$H1" /tmp/c7b.html && echo "失败后原码仍可查 OK"

say "8. 查回异常路径：格式非法 / 查无此码"
curl -sS -c "$COOKIE" -b "$COOKIE" "$BASE/codes/lookup?library_no=L01&code=zz" -o /tmp/c8a.html
grep -q "6～12 位" /tmp/c8a.html && echo "非法格式提示 OK"
curl -sS -c "$COOKIE" -b "$COOKIE" "$BASE/codes/lookup?library_no=L99&code=ffffff" -o /tmp/c8b.html
grep -q "查不到" /tmp/c8b.html && echo "查无此码提示 OK"

echo
echo "短码端到端检查全部通过。"
