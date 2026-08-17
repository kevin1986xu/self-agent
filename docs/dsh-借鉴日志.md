# deepseek-harness 借鉴日志

> 本地克隆：`vendor/deepseek-harness`（crontab 每日 09:17 自动 pull，日志 .pull.log）
> 审查方法：`git -C vendor/deepseek-harness log --oneline <上次SHA>..HEAD`
>
> **上次审查 SHA：`47f943859b`（最近检查 2026-08-17，上游无新提交）**

## 2026-08-17 接入层专项（用户点题：MCP/CLI 如何接入）

**dsh 的接入形态**：① MCP 消费——每 server 一个插件实例，工具名带
`mcp__<server>__<name>` 限定前缀（防跨服务器重名），配置改动热重连；
② CLI 三形态——交互 profile / **headless 一次性**（跑完打印答案退出）/ web；
③ ACP 协议——把 agent 暴露给程序化客户端 + 反向把外部 agent 当子代理接入。

**已借鉴 → 落地**

- **CLI 接入形态** → `src/self_agent/cli.py`（console script `self-agent`）：
  `login`（令牌落 ~/.self-agent，0600）/ `run "任务" -p 项目`（headless，
  待审批时 exit=4 供脚本分支）/ `chat`（REPL，审批口令直接输入）。
  走网关 local 通道——留痕/审批/身份/项目路由全套复用，CLI 即一个终端渠道。
  实测：headless 查询、-p 切项目（default 通用应答）、审批卡片 exit=4。

**候选观察项（新增）**

- 工具名 server 限定前缀：我们目前裸名，跨 MCP 同名会冲突——真冲突时再做
  （需兼容 evalset 期望名与 subagents.json 工具清单）；
- ACP：等编辑器/外部 agent 集成需求；subagent-acp 的「外部进程 agent 当
  子代理」思路留意。

## 2026-08-14 首次审查（全库设计巡览）

**已借鉴 → 落地**

- **guard 包（循环卫生守卫）** → `src/self_agent/guard.py` LoopGuardMiddleware：
  重复同参工具调用检测（消息历史统计、无实例状态），超阈值返回纠偏
  ToolMessage。对应我们实测三次观察到的重复调用失败模式。预算分层同步补齐：
  ModelCallLimitMiddleware(run_limit=80) 硬顶 + 网关 600s 流超时作 deadline。

**评估过、暂不引入（记录理由）**

- **compaction/spill（上下文压实+大结果外溢）**：deepagents 内置
  SummarizationMiddleware / SummarizationToolMiddleware / ContextEditingMiddleware
  已覆盖同类能力；待实际遇到上下文压力再启用配置，不重复造。
- **invariants（包级运行时契约）**：与 Cordis 插件架构强绑定，我们的
  中间件+评测回归承担同类质量职责。
- **tool-execution-pipeline 的五段式钩子**：设计漂亮（前置守卫→单调守卫→执行
  →后置→规范化），langchain middleware 的 wrap_tool_call 组合已是同构简化版；
  其「审批一次性提示语在守卫之前」的顺序我们已经通过 interrupt 实现。
- **defensive-patterns #6（不给不信任代码环境/可预测路径）**：我们沙箱已做
  （inherit_env=False + Docker 无网络 + 非 root）；#4「清理必须达到静止」
  值得在将来 per-session 容器回收时参考。

**候选观察项（未来提交里留意）**

- session-projection / session-telemetry：会话投影与遥测的边界划分；
- goal 包：显式目标对象 vs 我们的 todos，看其演化；
- permission-presets：权限预设组合，R19 连接器授权做细时参考。
