# 展览图录 · 逐图署名复核系统

供图馆复核展览图录逐图署名的 Flask 应用：上传来源台账 CSV，按 **图像 SHA-256 + 馆号**
在 SQLite 中增补/更正准用状态与署名；编目员勾选图片后，系统搜索**使用馆数最少**的
署名组合并出表；确认时强制复核现行台账；已确认表支持存档查询并标注后续台账变化；
已确认表可整表发放纸质图录**短码**（SHA-256 最短可用前缀，6～12 位小写），
供图馆按馆号 + 短码查回原完整摘要。

## 业务规则实现

1. **台账增补/更正**：CSV 按 `(image_sha256, library_no)` 唯一键 upsert（`app/ledger.py`、
   `app/storage.py`）。支持中英文表头与 `approved/revoked`、`准用/撤回` 状态别名；
   文件内重复键、缺列、非法状态、非 UTF-8 等均带行号报错，不入库。
2. **选图上限**：至多 100 张图（前端勾选限制 + 后端 `solver.MAX_IMAGES` 双重校验）。
3. **求解规则**（`app/solver.py`）：
   - 每张图仅能选一家 `approved` 馆；存在无准用来源的图时，列出这些图片，**本次不出表**；
   - 目标为整册使用馆数最少；馆数并列时，取**馆号升序序列字典序最小**的组合
     （按馆数 1..n 枚举 `combinations`，首个可行解即字典序最小）；
   - 一张图在入选馆集合中有多家准用时，取**最小馆号**及其署名；
   - 涉及的准用馆总数超过 16 家直接拒绝求解（枚举安全边界）。
4. **搜索期间可继续补正台账**：草稿以一次性 token 存 SQLite（24 小时自动清理），
   草稿页实时对照现行台账，发现漂移即禁用确认按钮并展示逐图差异。
5. **确认强制复核**（`app/__init__.py` 的 `/confirm`）：用现行台账重新求解，
   只有与草稿完全同一份表（图序、馆号、署名逐图一致）才保存；否则进入重算页，
   列出差异与按现行台账重算的结果，**本次不保存**。
6. **已确认表查询**（`app/recheck.py`）：存档内容永不改写，保留确认时的原馆号、原署名；
   仅逐图标注 `撤回准用`（原馆号已无有效准用记录）与 `署名有变`（同馆同图署名已改，
   备注列显示现行署名）。
7. **纸质图录短码**（`app/codes.py`、`app/storage.py` 的 `short_codes` 表）：
   - 已确认表详情页可“整表发码”，短码按**图序**显示并附排版用清单，供图馆查回原图；
   - 新码取图像完整 SHA-256 摘要的**最短可用小写前缀，限 6～12 位**；
   - 同馆同图已发过码则**沿用同馆原码**；同馆一码只对应一个完整摘要（双唯一索引兜底）；
   - 发码生效时该前缀在同馆台账中须唯一对应本摘要，**已撤回来源同样参与查重**，
     摘要比较**不区分大小写**（落库统一小写）；
   - 查重只按 6 位锚定的**相关前缀**走索引探测，不扫描全馆台账；
   - 遇到非 64 位十六进制摘要、6～12 位前缀全冲突或写入失败，说明原因、**整表不发新码**，
     已发原码仍可在“短码查回”页查询；
   - “短码查回”页按**馆号 + 已发短码**只查短码注册表返回原完整摘要；台账后来补入
     同前缀的图片不影响查回结果。

## 目录结构

```
app/
  __init__.py      Flask 路由与页面装配
  storage.py       SQLite 连接、schema、upsert、草稿/确认表/短码注册表存取
  ledger.py        台账 CSV 解析、校验与导入
  solver.py        最少馆组合求解（字典序 tie-break、每图最小馆号）
  recheck.py       确认前漂移比对、确认后存档复核
  codes.py         整表发码、最短前缀分配、同馆原码沿用、短码查回
  templates/       9 个 Jinja2 页面（中文界面，含短码查回页）
wsgi.py            gunicorn 入口
tests/test_app.py      20 个单元/集成测试（unittest）
tests/test_codes.py    17 个短码发码/查回单元与 Web 集成测试
tests/e2e_demo.sh       真实 HTTP 端到端演示脚本（署名复核主流程）
tests/e2e_codes_demo.sh 真实 HTTP 端到端演示脚本（短码发码/查回）
sample-data/       演示台账生成脚本与三份 CSV（含短码前缀冲突演示）
Dockerfile, docker-compose.yml, requirements.txt
```

## Docker Compose 启动

```bash
docker compose up --build
# 浏览器访问 http://localhost:8000
```

SQLite 数据库存放在命名卷 `credit-data`（容器内 `/data/credit.db`），重建容器不丢数据。
生产环境请通过环境变量覆盖 `FLASK_SECRET_KEY`。

## 本地（无 Docker）运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python wsgi.py        # 或 gunicorn -b 0.0.0.0:8000 wsgi:app
```

## 演示流程

```bash
python3 sample-data/generate_samples.py     # 生成 ledger_initial.csv / ledger_correction.csv
```

1. 首页上传 `sample-data/ledger_initial.csv`（6 张图、12 条来源，含撤回记录）；
2. “选图出表”勾选前 5 张图（第 6 张全部撤回），得到 3 馆解 `[L01, L03, L05]`；
3. 若同时勾选全部撤回的图，页面列出缺准用图片且不出表；
4. 上传 `ledger_correction.csv` 补正台账，草稿页显示漂移、确认被拒绝并给出重算表；
5. 放弃草稿重新搜索并确认；确认后再改台账，在“已确认表查询”中可见
   `撤回准用` / `署名有变` 行级标注，原署名保留。

## 自测

```bash
# 单元 + 集成（37 项：主流程 20 + 短码 17）
PYTHONPATH=. python3 -m unittest -v tests.test_app tests.test_codes

# 端到端 HTTP 演示（需先在 $PORT 启动服务）
PORT=8000 BASE=http://127.0.0.1:8000 bash tests/e2e_demo.sh
# 短码发码/查回演示（使用 sample-data/ledger_codes_demo.csv，建议用空库启动）
BASE=http://127.0.0.1:8000 bash tests/e2e_codes_demo.sh
```

## 短码演示流程

```bash
python3 sample-data/generate_code_samples.py   # 生成 ledger_codes_demo.csv
```

1. 首页上传 `sample-data/ledger_codes_demo.csv`（含同前缀两图、撤回记录与非十六进制脏摘要）；
2. 勾选图一/图二/图三出表并确认，在已确认表页点“整表发码”：
   图一/图二共享前 6 位前缀，各取 7 位码；图三独占前缀取 6 位码，按图序列入排版清单；
3. 再次发码全部沿用同馆原码（新发 0 个）；
4. “短码查回”页输入馆号 `L01` 与图一短码（支持大写输入），查回 64 位完整摘要；
5. 台账后补同前缀图片后再次查回，结果仍是注册表中的原摘要；
6. 对含脏摘要（`zzzz…`）的表发码，页面说明原因且整表不发新码，之前的原码仍可查。

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `CREDIT_DB_PATH` | `./data/credit.db` | SQLite 文件路径（容器内为 `/data/credit.db`） |
| `FLASK_SECRET_KEY` | 开发占位值 | 会话/flash 签名密钥，生产必须覆盖 |
