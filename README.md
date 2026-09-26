# 建设危房改造资金分期门禁基础平台

本项目是一套可离线运行的 Python 服务端平台，供县、乡镇和村级工作人员管理新型城镇化安置、土地资源分配、危房安全勘察与改造复核。账号登录、角色权限、业务状态、幂等结果和审计事件保存在 SQLite 中，适合安置经办、自然资源、住建复核与审计人员在单个 Linux 应用容器内协作。

## 目录

- `src/rural_allocation/`：乡镇片区、地块资源池、土地批次、家庭申请、分配运行与移交情景；
- `src/housing_safety/`：危房勘察协议、测量导入、异常复核、分析任务租约和安全结论；
- `src/remediation_review/`：改造案件、现场测量、风险分析、账号登录与质量审批；
- `src/dilapidated_funding/`：危房改造分期资金门禁（版本化预算、项目风险申报、可解释拨付阶段、紧急加固豁免、原子锁定与按实结算）；
- `fixtures/`：离线验收使用的勘察协议和结构化测点；
- `tests/`：核心规则、权限、错误边界、事务、API 和命令行验收测试。

## 环境

- Linux
- Python 3.11 或更高版本
- 运行时仅使用 Python 标准库和 SQLite

## 测试

```bash
PYTHONPATH=src python3 -m unittest discover -s tests -v
```

## 构建检查

```bash
python3 -m compileall -q src tests
```

## 离线验收

```bash
PYTHONPATH=src python3 -m rural_allocation.acceptance --workspace .
PYTHONPATH=src python3 -m housing_safety.acceptance --workspace .
PYTHONPATH=src python3 -m remediation_review.acceptance
PYTHONPATH=src python3 -m dilapidated_funding.acceptance --workspace .
```

前三条命令使用临时 SQLite 数据库完成村镇与地块登记、家庭申请分配、危房测量分析和改造审批，不访问外部网络。
危房资金门禁验收在工作目录下生成 `funding_acceptance.sqlite3`，并在结尾关闭后重新连接，
演示服务重启后仍能恢复待确认计划、豁免授权与审计链。

## 危房改造分期资金门禁

角色：`finance`（财政，发布预算）、`township`（乡镇，申报项目与施工资源）、`housing`（住建，评估、确认、验收）、`authority`（应急主管，豁免授权与取消）、`auditor`（审计与查询）。

- `POST /budgets`：财政发布带 `version`、`valid_from`、`valid_to` 的资金额度；版本号只能递增，适用版本取当前时刻落在有效期内的最高版本，迟到调整不影响已确认计划。
- `POST /resources`：乡镇登记可同时开工的施工资源容量。
- `POST /projects`：乡镇申报鉴定等级、风险等级、计划节点（权重合计 100）、最迟入住日期和中央/地方/自筹资金构成，提交时即给出可解释的门禁评估与按节点拆分的拨付阶段。
- `POST /projects/{id}/evaluate`、`/confirm`：住建按最新预算重新评估；确认时预算冻结与施工资源锁定在同一事务内完成，任一失败整体回滚，不留部分冻结。
- `POST /exemptions`：紧急加固豁免必须包含授权人（与签发账号一致）、理由、失效时间和放宽的门禁清单；资金能力、施工资源、鉴定等级门禁不可豁免，豁免到期后自动失效。
- `POST /projects/{id}/accept|suspend|cancel|resume`：验收按实际完成量结算（当前节点按比例折算，已付不回收）；暂停/取消释放剩余冻结与施工资源；恢复时按最新预算版本重新过门、重新冻结。
- `GET /projects/{id}/explanation`：说明项目为何获批、被哪些门禁阻断、是否经豁免以及为何延期；`GET /projects/pending` 恢复待确认计划；`POST /projects/sweep-delays` 巡查延期。
- `GET /audit/chain`：哈希链审计，篡改任意事件载荷即可检出。

## HTTP 服务

```bash
PYTHONPATH=src python3 -m rural_allocation.api --database rural.sqlite3 --host 127.0.0.1 --port 8080
PYTHONPATH=src python3 -m housing_safety.api --database housing.sqlite3 --host 127.0.0.1 --port 8081
PYTHONPATH=src python3 -m remediation_review.api --database remediation.sqlite3 --host 127.0.0.1 --port 8082
PYTHONPATH=src python3 -m dilapidated_funding.api --database funding.sqlite3 --host 127.0.0.1 --port 8083
```

服务均提供 `GET /health`，其余接口使用 JSON。账号登录和角色权限由服务端校验，进程重启后可以继续查询 SQLite 中的业务状态与审计历史。
