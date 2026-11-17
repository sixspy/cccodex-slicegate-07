# 日切放行

> 遵守 `ops/底座生成前必读_效率与技术栈.md`。FastAPI + Vue 3 + SQLite，交付须 docker compose 一键起；库用 SQLite，不挂外置 Postgres。  
> **禁止同构换皮**：本仓主链路、表结构、接口与页面均独立，不得与其它仓共用同一套 `/api/items` 壳。

## 骨架一句话

业务日分区切片登记后裁决完整度，完整才放行下游，并与消费水位对账

## 基本信息

| 项 | 值 |
|----|-----|
| 产品名（首页） | 日切放行 |
| 目录 | `04-slicegate` |
| 领域建议 | 数仓与数据湖 |
| 接口 | http://127.0.0.1:10617 |
| 库 | `data/app.db` |
| 账号 | admin / 123456 |

## 为何能出多题

迟到切片、重切、跨分区依赖、部分完整冻结、消费水位回拨

## 避开

别做成作业拓扑编排换皮；别做成纯水位大盘

## 互斥切口候选

- 迟到切片整批拒收 vs 进待补齐冻结区
- 重切失败整笔回滚 vs 保留已完整分区
- 放行与水位同步闸 vs 异步放行后纠偏

## 期望变更闭环（当日钉死口径）

策略条、重裁目录、期望详情、放行闸、列表旁注共用同一份切片视图（`slice_view`），口径只此一种写法：

- **甲规则（本日钉死）**：期望行数变更时，在单一事务内整行写入——期望版本 +1、完整度改“待重裁”、放行态回“未放行”、待重裁标记置位、记一条作废基线；旧完整标记随期望版本失效，不得再凭旧完整放行或推水位。放行闸只认 `evaluation_rev == expected_rev` 且重裁结论为“完整”的切片。
- 变更必须携带读取到的 `expected_rev`；不一致记 **期望争用**，本次不生效（后手不静默成功），且切片字段不半更新。
- 提交口径 `rule` 或 `statement` 与当日钉死说明不一致，拒绝并记 **期望口径偏了**；`POST /api/policy` 当天换口径同样被拒并落 `policy_events`。
- 实收变更（`POST /api/slices/{id}/arrival`）同样作废旧结论，必须重裁。
- 期望升高后仍低于当前期望的旧水位，在重裁目录与期望详情里单列（`watermarks_below_expectation`），只做对照，不作为放行依据。

## 主要接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/slices` | 切片列表（含 `gate`/`note`/`expected_rev`/`evaluation_rev`/`recut_required`）与当日口径 |
| POST | `/api/slices/{id}/expectation` | 期望变更（`expected_count`、`expected_rev`、可选 `rule`/`statement`/`reason`） |
| GET | `/api/expectations` | 期望变更流水（已生效 / 期望争用 / 期望口径偏了） |
| GET | `/api/slices/{id}/expectation` | 期望详情：切片视图、变更流水、重裁轨迹、低于期望的水位 |
| GET | `/api/recut` | 重裁目录：待重裁切片、重裁写入记录、低于期望的水位 |
| POST | `/api/slices/{id}/evaluate` | 按当前期望版本重裁并写入结论 |
| POST | `/api/slices/{id}/arrival` | 更新实收，作废旧结论 |
| POST | `/api/slices/{id}/release` | 放行闸：待重裁 / 未完整一律拒绝并记拒绝流水，不写水位 |
| GET/POST | `/api/policy` | 当日期望口径（GET 首读即钉死，POST 换口径被拒并记“期望口径偏了”） |

## 启动（一键）

`powershell
cd d:\document\cccodex\workspace\slicegate\slicegate-07
docker compose up -d --build
`

浏览器：http://127.0.0.1:10617/  
健康检查：GET http://127.0.0.1:10617/api/health

本机开发（可选）：

`powershell
python -m pip install -r requirements.txt
python -m uvicorn app:app --app-dir backend --host 127.0.0.1 --port 10617
python -m pytest -q
`
