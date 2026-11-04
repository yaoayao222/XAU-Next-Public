# XAU Next 接口契约

当前为 1.0.0 设计基线。接口路径统一 `/v1`；运行中的服务尚未实现。`x-release: V1.1` 标记可选实验，不属于 V1 日常版必须实现的接口。

## 权威源

- [openapi.json](openapi.json)：OpenAPI 3.1.1，定义 HTTP 操作、输入、响应、错误和 SSE 帧。
- [analysis-answer.schema.json](analysis-answer.schema.json)：JSON Schema Draft 2020-12，定义模型回答及共享价格/时间/方案结构。

公开类型只在上述两处维护。OpenAPI 使用相对引用共享答案结构。实施阶段由规范生成 Python wire DTO 和 TypeScript 客户端；领域模型独立。FastAPI 导出的 OpenAPI 用于语义核对，不反向替换权威源。

选择 3.1 系列是为了当前 Python/TypeScript 生成工具的共同支持，不声称它是 OpenAPI 最新编号。精确生成器版本必须在 T01 验证后锁定，支持声明不等于本项目生成链已经运行成功。[OpenAPI 3.1.1](https://spec.openapis.org/oas/v3.1.1.html)、[openapi-typescript](https://openapi-ts.dev/)、[datamodel-code-generator 输入支持](https://datamodel-code-generator.koxudaxi.dev/supported-data-types/)。

## 结构和业务校验

结构规范拒绝未知输入字段，金额使用十进制字符串，UTC 时间以 Z 结尾，缺失值显式为 null。`required` 与 `nullable` 是不同含义：一个字段可以必须出现而值为 null。

JSON Schema 不负责所有跨字段业务判断。服务端另核对：请求/快照/基础版本身份，phase 的连续时间范围，更新端点，计划角色唯一性，valid_from/entry_deadline/exit_deadline 顺序，价格几何和网格，订单建议覆盖、账户来源、费用适用性和当前版本。

`fixtures/answer-variants.json` 同时包含结构错误和“结构合法、业务不合法”的样例。自动结构校验通过不能替代这些业务检查。

## 写操作和事件

写操作携带 Idempotency-Key。同方法、路径、键和内容返回同一结果；同键不同内容返回 409。键至少保存 7 天。回答还受 request_id 与原文字节摘要的持久唯一约束，超过幂等键保留期仍不能重复生成版本。

更新携带 expected revision 或 base_version_id，数据库事务中重新检查。错误统一为 Problem schema 和 application/problem+json。任务受理 202 只表示已入队，成功结果以 Job.state=succeeded 为准。

工作台快照与 event_cursor 在同一读事务取得。SSE 的 data 为 Event JSON，只驱动重新读取权威结果；`resync.required` 要求重取快照。重连主动发送 Last-Event-ID。使用支持 Authorization 的 fetch SSE 客户端，不能直接用无法设置该头的原生 EventSource 替代。

MT5 内部桥协议在 T01 定义并交付其 schema，与 HTTP 使用不同信封；只公开已选定的只读操作，不提供任意 SDK 方法转发。

## 案例和完成状态

合成合法回答见 [analysis-answer.valid.json](../fixtures/analysis-answer.valid.json)，HTTP/SSE 示例见 [api-examples.json](../fixtures/api-examples.json)，业务预期见 [acceptance-cases.json](../fixtures/acceptance-cases.json)。

本轮只完成规范与样例验证。Python/TS 生成、运行时接口语义一致性、真实 MT5 读取和用户流程，均需在实施任务中实际验证。
