# ng-platform 修复结果汇总（2026-08-31）

> 覆盖：容器接线修复 → 测试席终审语义 4 项阻塞 → 审批 gate 通用化。
> 验证：本地 **43 测试全绿** + Docker E2E 会签通过 + 容器内 agent-run 语义链实测。

## 一、容器接线修复（测试席核验 ✅）

| # | 问题 | 修复 | 容器内证据 |
|---|---|---|---|
| 1 | `agents.run` 依赖 httpx 但 requirements 没声明 → 容器崩 | `requirements.txt` 加 `httpx>=0.27` | `import httpx` 0.28.1、`agents.run --help` 可用 |
| 2 | workers 未挂 shared/messages → TransferEscalation 容器路径失效 | compose workers 挂 `/shared/messages` + `OPENCLAW_SHARED_DIR` | 容器 mount 4586 files |
| 3 | 容器内 AutoAgentWorker 缺算力 env | compose workers 透传 `LLM_*`/`ANTHROPIC`/`OPENAI` | 容器实测就位 |
| 4 | TransferEscalation 幂等键含易变 stale_s → 同 key 冲突 | key=task+transfer_id、payload 去 volatile | 容器 0 异常 |

## 二、测试席终审语义 4 项阻塞（全部修复 ✅）

### P0 #1 `_is_builtin` latest-wins
- **问题**：AutoAgentWorker `_is_builtin` 取**首个**注册事件，main `_agents_registry` 取 latest → 不一致，agent 先 builtin 后 openclaw 仍误判执行
- **修复**：`app/workers/auto_agent.py` 改为取**最近一次**注册的 executor
- **回归测试**：`test_auto_agent_is_builtin_latest_wins`

### P1 #2 Transfer 升级推进 blocked
- **问题**：升级只发 `task.blocked` 事件，不推进状态 → `_task_stuck` 恒 True → 跨转移重复升级
- **修复**：`app/workers/transfer_escalation.py` `_block_task` 按状态机合法跳（TODO→IN_PROGRESS→BLOCKED / IN_PROGRESS→BLOCKED）
- **回归测试**：`test_transfer_escalation_blocks_task`（含"再扫不重复升级"断言）

### P1 #3 deliverable done 触发复核
- **问题**：`submit_deliverable` done 自动 in_review，但没触发 `review.requested`（交接链断）
- **修复**：API `submit_deliverable` + `AutoAgentWorker._execute` 都补 `_ensure_review_requested`（幂等，reviewer 取已指派）
- **回归测试**：`test_deliverable_done_triggers_review`、`test_auto_agent_worker_executes_builtin_task`（断言加 review.requested）

### P1 #4 artifacts 持久卷
- **问题**：容器内自研 agent 产出 `artifacts/` 随容器销毁丢失
- **修复**：compose workers 挂 `./artifacts:/app/artifacts`；宿主目录需可写（colima virtiofs 权限怪癖 → `chmod 777` 解决）
- **容器实测**：`640c58c3...md` 产出持久到宿主

**容器内完整语义链实测**：
```
task.created → agent.assigned → state(in_progress) → deliverable.submitted → state(in_review) → review.requested
```

## 三、审批 gate 通用化（新 R&D，架构文档 security/approval_gate.py）

- **问题**：审批门只有 `pause_project` 专用（手工查已批准审批），不通用
- **新增** `app/security/approval_gate.py`：
  - `ApprovalGate.ensure_approved(project_id, scope, task_id)`——已批准放行；否则**自动建审批请求**（幂等）抛 `PendingApproval` → **409 + approval_id**
  - `PendingApproval` 异常 → main.py 409 处理器（带 approval_id 供前端去决策）
- **重构** `pause_project` 用通用 gate（替代专用逻辑）
- **任务状态联动**：请求审批时任务 `IN_REVIEW → PENDING_APPROVAL`；决策批准 → `COMPLETED`、拒绝 → 退回 `IN_PROGRESS`（状态机第七节）
- **回归测试**：`test_approval_gate_general_409`、`test_request_approval_to_pending_approval`、`test_approval_decision_outcome`

## 四、验证证据

- **本地测试**：37 → **43 全绿**
- **Docker E2E 会签**：`scripts/docker_e2e.sh` exit=0，`✅ 全部通过`（P0 密码契约 / P1 409 / 全栈冒烟）
- **容器 agent-run**：语义链含 review.requested，artifacts 持久

## 五、待办/后续（README 边界）

- 前端看板（frontend/ 待建）
- 投影物化/乐观锁（projection_version / expected_version）
- 第二执行层（claude_sdk 等 AgentExecutor 实现）
- E2E 脚本注记：pgdata 卷有旧密码时需先 `docker compose down -v`
