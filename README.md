# 站点读流量路由演练

栖原站点运维组（11人）在交接班前，用节点位点与会话时序复核读写路由，明确何时读副本、何时回主库、何时必须拒绝读取。

## 场景与模块

原先值班员在配置表和聊天记录中核对副本延迟，以“延迟很低”推断写后读安全。痛点：低延迟不等于已应用会话写位点、故障时回退策略不清、决策无逐请求证据。上游输入为人工确认的节点状态和请求轨迹，本站承接路由规则演练与证据留档，下游为站点值班与变更复核人员。

三个核心实体：复制节点 Node（主副角色/在线状态/日志谱系/位点/延迟），会话请求集 RequestSet（带时序的读写、复制应用、故障恢复事件），路由试验 Trial（冻结输入、策略参数与结果）。5业务页+登录页，另有管理员审计。

**整套拓扑变更**是节点台账的唯一写路径：`TopologyVersion` 保存不可变的完整节点集，`TopologyHead` 单行指向当前生效版本。逐节点保存也会生成新版本，切换永远发生在单个事务里，因此不存在“无主”或“双主”的可见中间态。

**这是受控复制路由模型，不连接真实主从集群。** MySQL真实保存节点、请求、任务及证据；Python真实逐事件计算策略。日志位点采用命名谱系中的单调整数，未实现完整GTID集合/真实复制协议。

## 快速启动

```sh
cp .env.example .env
docker compose up -d --build
docker compose exec -T backend python -m scripts.seed_demo
```

前端 http://localhost:18247 ，后端 http://localhost:19247/docs ，MySQL localhost:17747。

|角色|账号|密码|权限|
|---|---|---|---|
|admin|admin@qiyuan.test|Demo2026!|全部业务写入、运行与审计|
|operator|operator@qiyuan.test|Demo2026!|节点/请求编辑、运行、导出|
|viewer|viewer@qiyuan.test|Demo2026!|只读和导出，写API403|

## 一天的工作剧本

09:00 operator登录；09:05查看harbor-primary位点105、north-replica位点104/150ms、south-replica位点105/20ms；09:10打开 `QY-1117-A` 请求集；09:15确认八事件时序；09:20以100ms延迟门槛和500ms粘滞执行 `shift-20261117`；09:25检查：写入主库位点106、100ms读仍走主库、600ms南站追平106、700ms读走南站、800ms南站故障、900ms回主库、1000ms主库故障、1100ms拒绝读取；09:30导出route-1.json；09:40 viewer核验拒绝理由；10:00 admin看审计。

另有 `QY-1117-B` 高延迟副本回退主库、`QY-1117-C` 主库故障且副本未追平的拒绝案例。所有试验使用冻结快照，不会把模型故障写回节点台账。

## 真实计算规则

输入上限12节点、100事件；时间0–86400000ms，事件按at_ms非递减，时间相同按输入顺序执行。必须有且仅一个主节点，同谱系副本初始位点不能超过主库。事件字段：

```json
[
  {"at_ms":0,"kind":"write","session":"lab-A"},
  {"at_ms":100,"kind":"read","session":"lab-A"},
  {"at_ms":600,"kind":"replicate","node":"south-replica","position":106,"delay_ms":20},
  {"at_ms":700,"kind":"read","session":"lab-A"}
]
```

- write：主库在线才成功；主库位点+1，记录该会话required_position和last_write_ms。失败写入不推进会话位点。
- read：若距最近成功写入 `< sticky_ms`，必须主库在线、同谱系且位点满足；否则拒绝。到达边界即结束粘滞。
- 粘滞期外：副本必须在线、同谱系、position≥会话required_position、delay_ms≤max_delay_ms，四条件同时成立。多个候选按延迟升序、位点降序、名称字典序选择。无副本达标则回主库；主库也失效则拒绝。
- replicate：只允许在线同谱系副本；位点不能回退、不能超过主库已提交位点。node_down/node_up修改模型状态。未知节点或非法复制在入队前422。
- 每个会话独立维护位点；从未成功写入的会话required=0，允许最终一致副本读取，不承诺全局强一致。低延迟绝不覆盖落后位点。

输出每事件目标/拒绝、会话所需位点、实际位点、各副本四项资格、最终节点、会话状态、汇总和SHA256。相同输入产生相同摘要；不依赖随机数或外部网络。拒绝不安全读是正常业务结果，不算任务失败。快照与结果保存在MySQL，Redis只传递任务ID。

## 整套拓扑变更

主副角色调整不能逐节点改。流程固定为 **草稿 → 校验 → 发布原子切换**：

1. 以当前生效拓扑为基准创建草稿（`POST /topology/changes`），可反复调整节点与角色。
2. 校验整套集合：有且仅一个主库、节点名唯一、同谱系副本位点不高于主库、位点与延迟取值范围（`/validate`）。
3. 发布前重新做三方比对（基准 / 草稿 / 当前生效）。草稿没碰的节点一律保留期间已生效的修改；双方都改过的节点形成冲突，保留 `base`、`draft`、`current` 三份依据与校验和，必须逐项选择“采用草稿 / 采用当前”后发布（`/resolve`）。
4. 基准已前进时发布返回 409，需显式 `confirm_stale_base` 表示已核对冲突再合并。
5. 发布在一个事务内写入新版本并把 head 指向它；并发发布只有一个基于当前 head 的方案胜出，另一个收到 409 且保持草稿。
6. 中断或失败：`applying` 记录在启动时或 `POST /topology/recover` 恢复——已提交版本补记为已生效，未提交则退回草稿。重试沿用同一条记录；同一 `request_id` 重复发布不会二次生效。

变更期间仍可编辑草稿、登记节点、发起试验。试验创建时冻结所选拓扑版本（默认当前 head，也可指定历史版本），已有试验结果与快照保持不变，页面可打开历史版本、查看冲突依据与生效记录。

验证：`backend/tests/test_topology_changes.py` 覆盖换主、双主/无主校验、草稿过期冲突、并发发布、中断恢复、幂等重试、试验快照与版本钉选（20 项）。本地运行 `cd backend && python -m pytest tests`。

## 工程说明

|服务|技术|职责|
|---|---|---|
|frontend|原生HTML/CSS/JS + Nginx|5业务页（含整套拓扑变更）、编辑表单、共享组件、/api代理|
|backend|Python3.11/FastAPI/SQLAlchemy/PyJWT|校验、权限、审计、快照与导出|
|worker|Python3.11|会话时序与路由算法|
|db|MySQL8.0|真实持久化业务和JSON证据|
|redis|Redis7|持久队列|

各实体贯穿 `backend/src/{models,schemas,services,routes}` 和 `frontend/src/{api,stores,pages}`；其余强制目录含controllers/middlewares/config/constants/utils、components/common/hooks/types/router。三组件table/badge/frame共用；hooks/session/feedback和utils/forms/html共用；request_id/errors双中间件。

共享枚举角色admin/operator/viewer与任务queued/running/completed/failed位于 `backend/src/constants/enums.py` / `frontend/src/constants/enums.js`，security/trials、frame/badge消费。节点角色primary/replica和状态healthy/down由node schema及前端表单定义，事件类型由request_set schema及路由算法定义。

拓扑表：`topology_versions`（完整节点集 JSON + SHA256）、`topology_head`（单行指针）、`topology_changes`（草稿、冲突、处理历史、尝试计划）。首个版本在启动时由既有 `nodes` 表自动生成。

环境变量见.env.example：COMPOSE_PROJECT_NAME、三端口、DB_PASSWORD、JWT_SECRET、SEED_DEMO。默认密码仅本地演示、JWT8小时；seed自然键仅补缺失，不覆盖修改。schema由metadata.create_all初始建表，无在线升级引擎。5服务均健康检查，backend/worker依赖健康db/redis，frontend依赖backend；数据库、Redis、worker产物使用命名卷。

本地开发：Python3.11安装backend/requirements.txt、配置DATABASE_URL/REDIS_URL，在backend运行 `uvicorn src.main:app --reload` 和 `python -m src.worker`，前端经Nginx同源代理。停止清空：`docker compose down -v --remove-orphans`。

限制：单worker串行、无生产任务重试/崩溃续跑；不模拟网络随机抖动、自动选主或完整GTID集合。Google Fonts离线回退，业务不依赖字体网络。

## License

MIT。源码外 `../../test-artifacts/gb-4108/` 含真实测试、截图与结果。
