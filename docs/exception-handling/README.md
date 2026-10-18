# 异常处理文档大纲

本目录按主题拆成四篇文档。点击任意条目跳转到对应章节。整理于 2026-10-18。

## 快速跳转

| 我想了解 | 看这里 |
| --- | --- |
| 异常在 LLM、节点、工具、HTTP 之间怎么传播 | [总览：异常传播主流程](<LangGraph 异常处理总览.md#异常传播主流程>) |
| 节点失败后 checkpoint 处于什么状态 | [总览：失败后的状态](<LangGraph 异常处理总览.md#失败后的状态>) |
| 某个 agent 当前有哪些降级点 | [总览：Agent / 工具层的降级点](<LangGraph 异常处理总览.md#agent--工具层的降级点>) |
| 配 `RetryPolicy`、退避、`retry_on` | [节点重试：RetryPolicy](<LangGraph 节点重试与超时.md#retrypolicy>) |
| 给节点配超时 | [节点重试：节点超时](<LangGraph 节点重试与超时.md#节点超时>) |
| 重试耗尽后优雅降级 | [节点重试：error_handler](<LangGraph 节点重试与超时.md#error_handler>) |
| RetryPolicy 该挂在哪个 agent 上 | [节点重试：挂载位置](<LangGraph 节点重试与超时.md#挂载位置>) |
| 工具参数错误怎么处理 | [ToolNode：三类错误](<LangGraph ToolNode 异常处理.md#三类错误>) |
| `handle_tool_errors` 怎么分流 | [ToolNode：异常分流](<LangGraph ToolNode 异常处理.md#异常分流handle_tool_errors>) |
| 工具级重试、退避、单次超时 | [ToolNode：单次调用级重试](<LangGraph ToolNode 异常处理.md#单次调用级重试awrap_tool_call>) |
| 工具异常在项目里怎么落地 | [ToolNode：本项目落地](<LangGraph ToolNode 异常处理.md#本项目落地>) |
| 接口什么时候返回 400/401/404/422/500 | [服务实践：HTTP 状态码约定](<Agent 服务异常处理实践.md#http-状态码约定>) |
| SSE 流式错误怎么发 | [服务实践：SSE 流式](<Agent 服务异常处理实践.md#sse-流式message_generator>) |
| AG-UI 流中断 | [服务实践：AG-UI](<Agent 服务异常处理实践.md#ag-ui>) |
| 客户端为什么只捕获到部分错误 | [服务实践：客户端与 UI](<Agent 服务异常处理实践.md#客户端与-ui>) |

## 1. LangGraph 异常处理总览

[打开全文](<LangGraph 异常处理总览.md>)

- [一句话总结](<LangGraph 异常处理总览.md#一句话总结>)
- [分层一览](<LangGraph 异常处理总览.md#分层一览>)
- [异常传播主流程](<LangGraph 异常处理总览.md#异常传播主流程>)
  - [失败后的状态](<LangGraph 异常处理总览.md#失败后的状态>)
- [各入口处置](<LangGraph 异常处理总览.md#各入口处置>)
- [Agent / 工具层的降级点](<LangGraph 异常处理总览.md#agent--工具层的降级点>)
- [配置 / 存储 / 可观测性](<LangGraph 异常处理总览.md#配置--存储--可观测性>)
- [待修问题索引](<LangGraph 异常处理总览.md#待修问题索引>)
- [测试覆盖概览](<LangGraph 异常处理总览.md#测试覆盖概览>)

## 2. LangGraph 节点重试与超时

[打开全文](<LangGraph 节点重试与超时.md>)

- [何时使用](<LangGraph 节点重试与超时.md#何时使用>)
- [RetryPolicy](<LangGraph 节点重试与超时.md#retrypolicy>)
  - [retry_on 的坑](<LangGraph 节点重试与超时.md#retry_on-的坑>)
- [节点超时](<LangGraph 节点重试与超时.md#节点超时>)
- [error_handler](<LangGraph 节点重试与超时.md#error_handler>)
- [挂载位置](<LangGraph 节点重试与超时.md#挂载位置>)
- [与 SDK 重试的关系](<LangGraph 节点重试与超时.md#与-sdk-重试的关系>)
- [本项目现状](<LangGraph 节点重试与超时.md#本项目现状>)
- [注意点](<LangGraph 节点重试与超时.md#注意点>)
- [测试](<LangGraph 节点重试与超时.md#测试>)

## 3. LangGraph ToolNode 异常处理

[打开全文](<LangGraph ToolNode 异常处理.md>)

- [三类错误](<LangGraph ToolNode 异常处理.md#三类错误>)
- [ToolNode 的默认行为](<LangGraph ToolNode 异常处理.md#toolnode-的默认行为>)
- [异常分流：handle_tool_errors](<LangGraph ToolNode 异常处理.md#异常分流handle_tool_errors>)
- [单次调用级重试：awrap_tool_call](<LangGraph ToolNode 异常处理.md#单次调用级重试awrap_tool_call>)
- [节点级重试与超时](<LangGraph ToolNode 异常处理.md#节点级重试与超时>)
- [工具内部处理](<LangGraph ToolNode 异常处理.md#工具内部处理>)
- [本项目落地](<LangGraph ToolNode 异常处理.md#本项目落地>)
  - [共享策略：src/agents/tool_retry.py](<LangGraph ToolNode 异常处理.md#共享策略srcagentstool_retrypy>)
  - [research-assistant](<LangGraph ToolNode 异常处理.md#research-assistant>)
  - [尚未接入](<LangGraph ToolNode 异常处理.md#尚未接入>)
- [验证](<LangGraph ToolNode 异常处理.md#验证>)

## 4. Agent 服务异常处理实践

[打开全文](<Agent 服务异常处理实践.md>)

- [HTTP 状态码约定](<Agent 服务异常处理实践.md#http-状态码约定>)
- [各端点](<Agent 服务异常处理实践.md#各端点>)
  - [/invoke](<Agent 服务异常处理实践.md#invoke>)
  - [/history 与 /threads](<Agent 服务异常处理实践.md#history-与-threads>)
  - [/feedback](<Agent 服务异常处理实践.md#feedback>)
  - [/health](<Agent 服务异常处理实践.md#health>)
- [SSE 流式（message_generator）](<Agent 服务异常处理实践.md#sse-流式message_generator>)
- [AG-UI](<Agent 服务异常处理实践.md#ag-ui>)
- [启动与关闭（lifespan）](<Agent 服务异常处理实践.md#启动与关闭lifespan>)
- [LLM 失败时的入口行为](<Agent 服务异常处理实践.md#llm-失败时的入口行为>)
- [日志与可观测性](<Agent 服务异常处理实践.md#日志与可观测性>)
- [客户端与 UI](<Agent 服务异常处理实践.md#客户端与-ui>)
  - [AgentClient](<Agent 服务异常处理实践.md#agentclient>)
  - [Streamlit](<Agent 服务异常处理实践.md#streamlit>)
- [待修问题](<Agent 服务异常处理实践.md#待修问题>)
- [测试覆盖](<Agent 服务异常处理实践.md#测试覆盖>)

## 推荐阅读顺序

1. 先读 [总览](<LangGraph 异常处理总览.md>)，建立异常传播的心智模型。
2. 需要给 LLM 节点加保护时，读 [节点重试与超时](<LangGraph 节点重试与超时.md>)。
3. 工具报错时，读 [ToolNode 异常处理](<LangGraph ToolNode 异常处理.md>)。
4. 排查接口 500、流式中断或客户端错误时，读 [Agent 服务异常处理实践](<Agent 服务异常处理实践.md>)。
