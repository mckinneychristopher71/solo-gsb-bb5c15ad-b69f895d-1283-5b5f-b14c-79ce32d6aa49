# 许可证依赖冲突审计台

基于 **FastAPI + SQLite** 的依赖许可证合规审计工具。上传包含包名、版本、许可证及依赖边的
manifest，系统会：

1. **规范化节点** —— 统一包名/版本写法、把许可证别名（`Apache 2.0`、`GPLv3`、`MIT License`…）
   映射为规范 SPDX 标识，合并重复节点并归一依赖边；
2. **计算传递约束** —— 按内置许可证兼容矩阵（宽松型 / 弱 copyleft / 强 copyleft / 专有），
   对依赖图做传递可达性分析，记录每个节点受到的来自所有祖先的许可证约束；
3. **标出最短冲突路径** —— 对每一对不兼容的“祖先许可证 → 后代许可证”，用 BFS 给出
   **最短依赖路径**（冲突路径上每个节点都标注许可证），硬冲突与“需人工确认”告警分级展示；
4. **按包筛选** —— 包名子串过滤，凡最短路径经过该包的冲突都会保留；
5. **导出 JSON** —— 完整审计结果（含筛选结果）可一键下载；审计记录持久化在 SQLite；
6. **两次审计对比** —— 从历史中选择两个 `audit_id`，按 **包名+版本** 配对节点、
   按「祖先节点 → 被依赖节点」配对冲突/告警，展示新增、消失及状态变化（严重程度/
   许可证/最短路径），每条都附上双方各自的最短依赖路径；对比为纯只读操作，
   记录不存在时返回明确错误（404），**不会写入新审计**。
7. **跨审计复核标记** —— 对冲突/告警的 *祖先节点 + 被依赖节点 + 双方规范化许可证*
   计算指纹，在网页上填写复核备注与失效时间即可标记「已复核」，也可随时撤销；
   记录持久化在 SQLite。读取 **任意历史审计** 时，仅把指纹一致、未过期且未撤销的
   最新标记附到对应条目，区分 **待复核 / 已复核**；许可证变更（指纹改变）、标记过期
   或被撤销后，条目自动回到「待复核」。原始冲突、严重程度与两次审计对比完全不受影响。

## 快速开始（Docker Compose，一键启动，推荐）

```bash
docker compose up --build          # 前台启动（加 -d 可后台运行）
```

服务监听容器内 **8000** 端口，Compose 已映射到宿主机 **8000**：<http://localhost:8000/>。
容器启动时会自动初始化数据库（SQLite 文件位于卷 `audit-data`，容器内路径 `/data/audit.db`）。

启动后访问：

| 地址 | 说明 |
| --- | --- |
| <http://localhost:8000/> | 网页界面（已自动载入示例 manifest；第 2 节可选择两次历史审计进行对比） |
| <http://localhost:8000/docs> | Swagger 风格交互式 JSON API 文档 |
| <http://localhost:8000/redoc> | ReDoc API 文档 |
| <http://localhost:8000/api/matrix> | 许可证兼容矩阵（机器可读） |
| <http://localhost:8000/healthz> | 健康检查 |

停止并保留数据：`docker compose down`；连同数据卷一起清除：`docker compose down -v`。
8000 端口被占用时，可修改 `docker-compose.yml` 中 `"8000:8000"` 的宿主机一侧
（例如 `"18000:8000"`），随后访问 <http://localhost:18000/>。

## 本地直接运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

数据库默认创建在 `./data/audit.db`，可用环境变量覆盖：

- `DATA_DIR` —— 数据库目录（默认 `./data`）；
- `DATABASE_URL` —— 直接指定 SQLite 文件路径（如 `/var/lib/audit/audit.db`）。

## manifest 格式

```json
{
  "name": "my-app",
  "packages": [
    {
      "name": "web-app",
      "version": "1.0.0",
      "license": "AGPL-3.0",
      "dependencies": [
        "billing-core@1.2.0",
        { "name": "ui-kit", "version": "^0.9.0" },
        { "name": "font-pack", "version": "2.0.0", "license": "MIT" }
      ]
    },
    { "name": "billing-core", "version": "1.2.0", "license": "GPL-2.0" }
  ],
  "dependencies": {
    "billing-core@1.2.0": ["font-pack@2.0.0"]
  },
  "edges": [
    { "source": "web-app@1.0.0", "target": "billing-core@1.2.0" }
  ]
}
```

约定：

- `packages` 声明节点；`dependencies`（包内或顶层，数组或 `名字: 版本` 映射均可）与
  `edges` 声明依赖边，方向为 **使用方 → 被依赖方**，传递约束沿边传播；
- 只给包名不给版本时，若 `packages` 中该名字唯一则自动解析，多个版本需用 `name@version` 消歧；
- 无法解析到已声明节点的依赖会进入 `unresolved` 列表并产生告警，不会凭空造节点；
- 未在矩阵中识别的许可证（如 `OFL-1.1`）保留原文并标记为 unknown，
  相关边按“需人工确认”告警处理，而非误判为兼容；
- 依赖环会被安全处理（BFS 节点只访问一次），不影响最短路径计算。

## JSON API 速览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/audit` | 提交 JSON：`{"manifest": {...}, "name": "可选名称"}` |
| POST | `/api/audit/upload` | 上传 `.json` manifest 文件（multipart） |
| GET | `/api/audits?limit=20` | 历史审计列表 |
| GET | `/api/audits/{id}` | 获取审计结果；`?package=xxx` 按包筛选 |
| GET | `/api/audits/{id}?download=true&package=xxx` | 导出（筛选后的）JSON 文件 |
| GET | `/api/audits/compare/{base_id}/{target_id}` | **两次审计对比**（只读，不写入） |
| DELETE | `/api/audits/{id}` | 删除审计记录 |
| POST | `/api/reviews` | **创建跨审计复核标记**（按冲突指纹生效） |
| GET | `/api/reviews` | 复核标记记录列表（含已过期/已撤销） |
| POST | `/api/reviews/{id}/revoke` | 撤销复核标记（记录保留） |
| GET | `/api/matrix` | 许可证兼容矩阵 |
| GET | `/api/sample` | 内置示例 manifest |

示例：

```bash
# 上传文件审计
curl -F "file=@static/examples/sample-manifest.json" http://localhost:8000/api/audit/upload

# 按包筛选并导出
curl -OJ "http://localhost:8000/api/audits/1?package=pdf-export&download=true"
```

### 两次审计对比接口

```bash
curl http://localhost:8000/api/audits/compare/1/2
```

路径方向为 **基准 → 目标**：`added` 表示仅目标审计（#2）存在，`removed` 表示仅基准审计（#1）存在。
返回内容：

- `summary.nodes` —— 节点（按 `name@version` 配对）的新增 / 消失 / 配对成功 / 同节点许可证变化数量；
- `summary.findings` —— 冲突/告警的新增、消失、变化数量（并分别统计其中硬冲突数）；
- `added` / `removed` —— 每条携带存在侧的完整冲突对象，含 `path` 最短依赖路径；
  缺失侧固定为 `null`；
- `changed` —— 双方都存在的配对项，同时给出 `base.path` 与 `target.path`，
  `fields_changed` 标明变化项：`severity`（硬冲突⇄告警）、`status`（兼容判定）、
  `ancestor_license` / `node_license`（许可证原文或规范标识变化）、`path`（最短路径变化）；
- `nodes` —— 节点配对明细，含双方许可证、硬冲突数及变化方向。

对比是**只读**操作，不会创建任何审计记录；任一 `audit_id` 不存在时返回 `404`
并指出缺失的编号，传入相同编号返回 `400`：

```bash
$ curl -i http://localhost:8000/api/audits/compare/1/999
HTTP/1.1 404 Not Found
{"detail":"审计记录不存在：#999。对比为只读操作，不会创建新审计。"}

$ curl -i http://localhost:8000/api/audits/compare/3/3
HTTP/1.1 400 Bad Request
{"detail":"请选择两条不同的审计记录进行对比：3 与 3 相同。"}
```

网页端在首页第 2 节「历史审计 · 两次对比」选择两条记录后点击对比即可：
结果按新增 / 消失 / 变化分组，变化项会用 `硬冲突 → 告警` 这类徽标展示严重程度翻转，
并同时渲染双方的最短依赖路径（红色=基准，绿色=目标），折叠区可查看全部节点配对明细。

### 跨审计复核标记接口

复核标记绑定的是 **冲突指纹**（祖先节点 + 被依赖节点 + 双方规范化许可证的 SHA-256），
而不是某一次审计，因此同一份 manifest 反复审计时标记始终跟随；`Apache 2.0` 与
`Apache-2.0` 这类写法差异也会归一到同一指纹。

```bash
# 1) 先审计，拿到冲突条目 id（如 C001）
AUDIT_ID=$(curl -s -F "file=@static/examples/sample-manifest.json" \
  http://localhost:8000/api/audit/upload | python3 -c "import sys,json;print(json.load(sys.stdin)['audit_id'])")

# 2) 创建复核标记：备注 + 失效时间（expires_at 可省略，表示永久有效）
curl -X POST http://localhost:8000/api/reviews \
  -H "Content-Type: application/json" \
  -d "{\"audit_id\": $AUDIT_ID, \"conflict_id\": \"C001\",
       \"note\": \"法务已确认组合分发\", \"expires_at\": \"2026-12-31T23:59:59Z\"}"

# 3) 读取任意历史审计：指纹一致且未过期/未撤销的最新标记会附到对应条目
curl http://localhost:8000/api/audits/$AUDIT_ID
#   conflicts[0].review = {"state": "reviewed", "mark_id": 1, "note": "...", ...}
#   未命中标记的条目则为 {"state": "pending"}；summary.review 给出待复核/已复核统计

# 4) 查看全部复核记录（effective_state: active / expired / revoked）
curl http://localhost:8000/api/reviews

# 5) 撤销标记（记录保留，条目立即回到待复核）
curl -X POST http://localhost:8000/api/reviews/1/revoke
```

行为约定：

- 标记只在 **读取层** 附着，数据库中的审计结果保持原始形态，
  两次审计对比（`/api/audits/compare/...`）不受复核标记影响；
- 许可证发生变化 → 指纹改变 → 旧标记不再附着，条目自动回到「待复核」；
- 标记过期（到达 `expires_at`）或被撤销 → 同样自动回到「待复核」，无需任何手动清理；
- 同一指纹可存在多条记录，**最新一条有效（未撤销且未过期）标记** 生效；
- `expires_at` 使用 ISO 8601（可带 `Z` 或时区偏移，不带时区按 UTC 处理），格式非法返回 `422`；
  `audit_id` / `conflict_id` 不存在返回 `404`，重复撤销返回 `404`。

网页端在审计结果的每张冲突卡片上即可操作：待复核时填写备注、选择失效时间
（留空为永久有效）后点击「标记已复核」；已复核时展示备注与失效时间，可一键撤销。
「复核记录」卡片汇总全部标记（有效 / 已过期 / 已撤销）并支持撤销。

## 审计结果结构（节选）

```json
{
  "audit_id": 1,
  "manifest_name": "billing-platform-demo",
  "status": "conflict",
  "summary": { "packages": 13, "edges": 15, "conflicts": 4, "license_warnings": 3, "unresolved": 1,
               "review": { "total": 7, "reviewed": 2, "pending": 5 } },
  "nodes": [
    { "id": "pdf-export@4.0.0", "name": "pdf-export", "version": "4.0.0",
      "license_display": "Apache 2.0", "license": "Apache-2.0",
      "license_category": "permissive",
      "constraints": [ { "from": "billing-core@1.2.0", "license": "GPL-2.0", "distance": 1, "status": "conflict" } ] }
  ],
  "conflicts": [
    {
      "id": "C001", "kind": "license-conflict", "severity": "error",
      "ancestor": "billing-core@1.2.0", "node": "pdf-export@4.0.0",
      "ancestor_license": "GPL-2.0", "node_license": "Apache 2.0",
      "distance": 1,
      "path": ["billing-core@1.2.0", "pdf-export@4.0.0"],
      "reason": "GPL-2.0 与 Apache-2.0 专利条款冲突，不能组合进同一作品。",
      "review": { "state": "reviewed", "mark_id": 1,
                  "note": "法务已确认组合分发", "expires_at": "2026-12-31T23:59:59+00:00",
                  "marked_at": "2026-09-23T08:30:00+00:00" }
    }
  ],
  "unresolved": [ { "source": "web-app@2.1.0", "reference": "cache-redis", "reason": "依赖包未在 packages 中声明" } ],
  "warnings": ["未解析依赖：web-app@2.1.0 -> cache-redis（依赖包未在 packages 中声明）"]
}
```

## 兼容矩阵说明（方向：父作品许可证 → 依赖许可证）

| 父 \ 依赖 | MIT / BSD / ISC / CC0 | Apache-2.0 | MPL-2.0 | LGPL-2.1 / 3.0 | GPL-2.0 | GPL-3.0 | AGPL-3.0 | Proprietary |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| MIT 等宽松型 | ✅ | ✅ | ✅ | ✅* | ✅* | ✅* | ✅* | ⚠️ 授权 |
| MPL-2.0 | ✅ | ✅ | ✅ | 2.1 ❌ / 3.0 ✅ | ❌ | ✅ | ❌ | ❌ |
| LGPL-2.1 | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ | ❌ | ❌ 静态合并 |
| LGPL-3.0 | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ❌ 静态合并 |
| GPL-2.0 | ✅ | ❌ 专利条款 | ❌ | 2.1 ✅ / 3.0 ❌ | ✅ | ❌ | ❌ | ❌ |
| GPL-3.0 | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ |
| AGPL-3.0 | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ | ✅ | ❌ |
| Proprietary | ✅ | ✅ | ⚠️ 需动态链接 | ⚠️ 需动态链接 | ❌ | ❌ | ❌ | ⚠️ 商业授权 |

`*` 弱/强 copyleft 依赖虽可被宽松作品使用，但其义务会继续向分发链下游传递，本工具会在
`constraints` 中如实记录。完整且带逐条理由的矩阵以 `GET /api/matrix` 返回为准。

> 矩阵依据 SPDX / FSF 对常见许可证组合的通行说明整理，仅用于工程侧自动筛查，
> 不构成法律意见；标记 ⚠️ 与 unknown 的条目请由法务人工确认。

## 项目结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由（JSON API + 网页，含两次审计对比与复核标记接口）
│   ├── analyzer.py    # manifest 解析、节点规范化、传递约束、最短路径
│   ├── compare.py     # 两次审计对比：节点/冲突配对与变化检测（纯只读）
│   ├── review.py      # 跨审计复核标记：冲突指纹、失效时间解析、标记附着
│   ├── licenses.py    # 许可证别名规范化与兼容矩阵
│   └── database.py    # SQLite 初始化与审计记录/复核标记持久化
├── static/
│   ├── index.html     # 单文件网页（无外部 CDN 依赖）
│   └── examples/sample-manifest.json
├── tests/             # 轻量自测：python -m tests.test_analyzer / test_compare / test_review
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
