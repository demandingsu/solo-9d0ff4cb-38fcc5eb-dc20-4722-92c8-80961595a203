# 培训供数与更正 API

培训报名表和测评表按课题分批交付。源数据由培训方提供，每行带**表名**、**源行号**和内部判断的**归并学员号**；交付给课题组时改用**课题专用匿名行号**和**学员号**。同一学员在不同表中拿到同一个学员号；同一行在课题内的行号一旦分配就不变。发现两人被误并为一人时，把要拆出的行单独指定，拆出的行统一换新学员号，其余行保留原号。按成功登记的交付记录给课题组出更正单，凭原匿名行号标明新号，更正单可原样重新获取。

## 运行

```bash
docker compose up --build
```

- 交互文档：<http://localhost:8000/docs>
- OpenAPI: <http://localhost:8000/openapi.json>
- 数据卷 `app-data` 挂到容器 `/data`，SQLite 文件为 `/data/app.db`。

本地不装 Docker 时：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
APP_DB_PATH=./data/app.db .venv/bin/python -m uvicorn app.main:app --port 8000
```

## 在 /docs 里演示

1. `POST /demo/seed` — 生成一套演示数据（可重复调用，不会重复写入）。
2. `GET /projects/{project_id}/rows` — 看课题行号、学员号、拆分标记。
3. `GET /projects/{project_id}/deliveries` — 看每次交付的匿名行号与学员号。
4. `GET /projects/{project_id}/corrections` — 看全课题更正单；
   `?split_id=1` 只看某次拆分，`?delivery_id=3` 只看某次交付涉及的更正行。

演示数据覆盖：跨表共享学员号；S002 在拆分前、拆分后都有交付；S002 中未拆出的行保留原号；S003 的拆分行从未交付，因此更正单为空。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/source-rows` | 登记/更正源明细（表名、行号、归并学员号） |
| GET | `/source-rows` | 查看源明细 |
| POST | `/projects` | 创建课题组 |
| POST | `/projects/{id}/deliveries` | 按课题交付指定行，返回匿名行号与学员号 |
| GET | `/projects/{id}/deliveries[/{delivery_id}]` | 重取交付记录 |
| GET | `/projects/{id}/rows` | 课题行号、学员号、拆分/交付状态 |
| POST | `/projects/{id}/splits` | 拆分误并学员 |
| GET | `/projects/{id}/splits[/{split_id}]` | 重取拆分记录 |
| GET | `/projects/{id}/corrections` | 更正单（可按 `split_id` / `delivery_id` 过滤） |
| POST | `/demo/seed` | 演示数据 |
| GET | `/health` | 健康检查 |

### 交付

```json
POST /projects/{id}/deliveries
{
  "request_id": "batch-2026-05-01-1",
  "items": [
    {"table_name": "registration", "row_no": 2},
    {"table_name": "assessment",   "row_no": 11}
  ]
}
```

`merged_student_no` 可省略；若填写则必须与源登记一致，否则返回 409。行不存在返回 404，同批重复行返回 422，整批不会写入。`request_id` 用于幂等：同 id 同内容返回原记录，同 id 不同内容返回 409。

### 拆分

```json
POST /projects/{id}/splits
{
  "merged_student_no": "S002",
  "rows": [
    {"table_name": "assessment", "row_no": 11},
    {"original_row_no": 4}
  ]
}
```

行可以用源坐标 `table_name + row_no` 指定，也可以用更正单上的原匿名行号 `original_row_no` 指定（两种不能混用在同一项上）。拆分行的归并学员号可由服务推断；跨多个归并号时必须显式指定。所有被拆行必须当前属于同一学员号，否则返回 409 `row_not_in_student` / `rows_span_multiple_students`。

### 更正单

每行包含：`table_name`、`original_row_no`（原匿名行号）、`old_student_no`、`new_student_no`、`delivery_id`、`split_id`。更正行的编号与内容在拆分成功时确定，读取时只按已登记交付记录展开，不在读取时重算归属，因此同一 `split_id`/`delivery_id` 的更正单多次获取结果完全一致（含 `digest_sha256`）。同一更正行无论按课题、按 `split_id` 还是按 `delivery_id` 查询，`correction_id` 都相同。

### 拆分成本

拆分对同一课题的写锁占用只随**本次拆出的行数**增长，不随这些行历史交付次数增长：

- 交付时在同事务内把 `(课题, 表, 行, 学员号)` 的交付计数累加到 `delivery_rollups`；
- 拆分时每拆出一行只读一次该计数、写一条 `correction_ranges`（一行对应一个连续更正编号区间）；
- 更正单读取时用 `correction_items` 视图把区间按交付顺序展开成逐条更正行。

回执中的 `correction_count` 在提交时直接由计数得出，不需要回扫交付历史。

## 实现要点

- **编号**：`project_counters` 为每个课题维护行号、学员号两个自增序列。`project_rows` 以 `(project_id, table_name, row_no)` 为主键并唯一约束 `project_row_no`；`project_student_numbers` 对每个归并学员号唯一，拆分组以 `kind='split'` 另占一个号。
- **交付即登记**：每次交付把每行写入 `delivery_items`（含当时的学员号），并在同事务内累加 `delivery_rollups`。这是更正单的唯一事实来源。
- **拆分与更正同事务**：拆分先完成全部校验，再在同一 `BEGIN IMMEDIATE` 事务内分配新号、改行归属、按行写一条 `correction_ranges` 并从全局分配器预留一段更正编号。任一步失败整体回滚，原归属、原更正单和编号分配都不变。
- **交错交付**：更正区间只覆盖"用旧号成功交付过"的记录，所以拆分前的交付一定在单上；拆分后的交付读到当前学员号，用新号，不产生更正行。
- **更正编号**：`correction_id_state` 是全局分配器，升级时初始化为旧 `legacy_correction_items` 最大编号 + 1；新更正行编号不会与旧卷已发出的编号重复。
- **旧卷升级**：`init_db` 幂等迁移 v1 → v2：把旧 `correction_items` 表改名为 `legacy_correction_items` 并原样保留，回填 `delivery_rollups`，把旧拆分标记为 `correction_format='per_line'`。旧更正行的编号、顺序和单据摘要因此可原样查回；新拆分从分配器取新号继续追加。
- **并发**：所有写操作 `BEGIN IMMEDIATE`，SQLite WAL 模式，连接 30s busy timeout。并发交付不会重号，并发拆分同一组只有一次成功。

## 实测

```bash
python -m pytest -q     # 28 passed
```

覆盖内容：

- 跨表同号、行号稳定、不同课题编号隔离；
- 未知行、重复行返回错误且无副作用；
- 拆分后未拆出的行保留原号；
- 交错交付（交付→交付→拆分→交付）的更正单内容与顺序；
- 未交付即拆分的更正单为空；
- 拆分失败（行已不属于该组）后归属、更正单编号和分配器都不变；
- 用匿名行号寻址拆分；
- `request_id` 幂等与冲突；
- 更正单按 `split_id`、`delivery_id` 过滤，且同一更正行在三种查询范围内编号一致；
- 同一行多次交付各生成一条更正行；
- 同一行 12 次交付后拆分：只写 1 条更正区间、0 条逐条更正行，回执 `correction_count=12`，更正单 12 行；拆分后交付不再新增；
- 拆分写入量不随历史交付次数变化（20 次交付后拆分仍只写一条区间）；
- v1 数据卷升级后：旧更正单（课题/拆分/交付三种范围）逐字节一致，旧更正编号保留，新拆分编号接续；
- 8 线程并发交付编号唯一；6 线程并发拆分同一组仅一次成功；失败事务回滚计数器；并发重复交付后拆分每笔交付各一条更正行且编号唯一。

### 拆分写入量实测

同一行连续交付 N 次后拆分，SQLite 虚拟机指令数与实际变更行数（`total_changes`）：

| 交付次数 | 指令数（旧） | 变更行数（旧） | 指令数（新） | 变更行数（新） |
| --- | --- | --- | --- | --- |
| 1 | 593 | 7 | 648 | 8 |
| 10 | 1664 | 16 | 648 | 8 |
| 100 | 12374 | 106 | 648 | 8 |
| 400 | 48074 | 406 | 648 | 8 |
| 1000 | — | — | 649 | 8 |

旧实现随交付次数线性增长；新实现拆分开销恒定（每行一次计数读 + 一条区间写）。

## 余项

- 未做身份认证与按课题的数据权限；生产部署需要加一层鉴权。
- 不支持删除/撤销拆分或更正行，只有追加式记录。
- 拆分只允许从"归并组"拆出；已拆出的组若要再拆，需要新的接口语义和更正链设计，当前返回 409。
- 更正编号全局唯一且写入时固定；未被任何交付使用的行不预留编号，因此编号不保证连续，只保证唯一、稳定、可原样重取。
- 更正单读取仍需按行展开交付记录；超大更正单可增加按区间分页。
- 分页为 offset 方式，超大课题可改用键集分页。
- 演示数据是固定剧本，仅用于 `/docs` 试用。
- 本次环境无 Docker，容器启动未实测；`Dockerfile`/`docker-compose.yml` 未改动，本地以 `uvicorn` 启动验证。
