# deepseek-harness 借鉴日志

> 本地克隆：`vendor/deepseek-harness`（crontab 每日 09:17 自动 pull，日志 .pull.log）
> 审查方法：`git -C vendor/deepseek-harness log --oneline <上次SHA>..HEAD`
>
> **上次审查 SHA：`47f943859b`（最近检查 2026-08-17，上游无新提交）**

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
