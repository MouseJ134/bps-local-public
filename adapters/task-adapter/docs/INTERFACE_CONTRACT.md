# 工具接口契约

本文描述独立 Python 请求适配器 `0.6.0-dev.2` 的行为。版本号属于此适配器，不代表 CPA-BPS 插件发布，也不代表 `cpa-bps.so` 的源码已修改。

## 调用链

```text
Responses 客户端 → 本地认证/请求适配器 → 已配置的本地 BPS 兼容服务
                                    ← 原始 HTTP/SSE 响应 ←
```

| 层 | 职责 | 能力边界 |
| --- | --- | --- |
| 上游模型运行环境 | 提供模型及上游原生工具 | 上游工具不自动成为外部客户端工具 |
| BPS `run_officejs` 载体协议 | 在支持该协议的插件中解码客户端调用 | 在此协议中不是 Office.js 执行器 |
| 当前 client catalog | 声明客户端函数/custom 工具、精确名称和参数契约 | 声明不代替真实执行器、授权或资源会话 |
| 此请求适配器 | 追加受限的目录提醒和任务提示；观察特定错误 | 不执行工具、不注册工具、不扩充权限、不改写工具结果 |

可执行调用必须逐步满足：当前目录声明 → 正确传输格式 → 插件校验 → 客户端真实执行器 → 权限检查 → 真实结果。不能只复制工具名或参数 schema 就宣称接入完成。

## 当前目录的来源

收集器读取根 `tools` 和顶层 `input` 中 `type="additional_tools"` 项的 `tools`。支持 function、custom 和 namespace；命名空间按声明组成精确名称。根级 `web_search` / `web_search_preview` 被记录为 hosted 类型，不加入可分发客户端函数清单。

相同名称和相同规范化契约可合并。名称冲突、命名空间歧义、未知工具结构、无法确认的嵌套声明或资源超限会跳过提醒并保持请求体原样，不猜测覆盖顺序。schema 规范化只忽略 schema 节点上的描述性注释；不会删除名为 `description` 的实际参数，也不会改变 `enum`、`const`、`default` 等字面数据。摘要用于目录指纹与比较，原始工具 schema 不被改写。

这是契约收集器，**不是完整 JSON Schema 校验器**。参数合法性与核心插件的验证资源预算仍由其实际校验器决定。收集器通过不代表随后任何参数都能通过。

## 四类载体

仅用于明确支持以下 CPA-BPS 载体协议的服务。每个工具采用哪种传输，应由插件为该工具给出的声明确定；参数名恰好为 `code` / `cmd`，或某个联合分支包含该字段，不能用来猜测模式。

| 传输 | `summary` | 载体 `code` | `extended_summary` |
| --- | --- | --- | --- |
| FUNCTION | 简短操作说明 | 单个序列化 JSON 对象 `{"name":"CATALOG_NAME","arguments":{...}}` | 普通说明 |
| FUNCTION_CODE | `codex2api.function_code/CATALOG_NAME` | `code` 参数的原始字符串 | 其余已提供参数组成的 JSON 对象；无则 `{}` |
| FUNCTION_CMD | `codex2api.function_cmd/CATALOG_NAME` | `cmd` 参数的原始字符串 | 其余已提供参数组成的 JSON 对象；无则 `{}` |
| CUSTOM | `codex2api.custom/CATALOG_NAME` | 已声明 custom 工具的原始输入，满足其文法 | 不用于另一套函数参数封装 |

`CATALOG_NAME` 必须完整保留当前目录的命名空间。FUNCTION 的 `arguments` 是对象，不是再次编码的 JSON 字符串。CODE/CMD/CUSTOM 不额外包 JSON 信封或 Markdown 代码围栏；外层 JSON 本身仍需正确转义。CODE/CMD 的 `extended_summary` 排除已经放进载体 `code` 的字段。

协议提醒使用外层 `destructive=false`、`references=[]` 并包含 `extended_summary`。这些载体字段不能替代执行器权限检查，也不能保证特定 BPS 版本兼容。不要嵌套调用载体；每次消费实际工具返回后再继续。没有执行证据时，不生成成功结果或自动重放有副作用的调用。

## 提醒何时生效

策略默认关闭。启用后，适配器还会检查方法、路径、Session-Id、Content-Type、模型白名单、`tool_choice`、请求形状、完整性相关头和各项预算。仅对符合条件的 `POST /v1/responses` 追加一个 developer 消息；原有历史项和工具声明不变。

提醒包含完整的 `{name,type}` 清单、契约 SHA-256、固定传输规范与任务提示，并明确 `update_plan` / `list_connectors` 是否声明。没有计划工具时建议使用普通文本步骤，不生成同名空壳。

已有 v1/v2/v3 提醒时不重复添加，也不替换历史提醒。完整提醒超过 32 KiB 时整体跳过，不将截断目录称为完整目录。提醒是提示层措施，不能保证模型不再选择目录外工具。

任务 profile 仅允许 `generic`、`software_development`、`study_notes`、`document_creation`；通过显式 session 映射选择，未映射时使用 `generic`。不从聊天内容猜测任务，不扩大原目标或权限。

## 选定工具的参数契约补充

策略 v3 可通过 `parameter_tools_by_session` 显式指定 session 对应的精确 function 工具名。session 必须在策略名单内，请求模型也必须匹配；`all_bps` 不会使未映射 session 自动获得参数补充。v1/v2、禁用策略和未映射 session 均不启用此功能。

收集器从本次真实目录取得每个选择项的完整规范化验证契约，生成确定性 JSON 和 SHA-256。每 session 最多 8 项，总补充文本最多 16 KiB。列表重复、声明缺失、非 function、契约冲突/歧义、请求解码或资源预算失败时，不添加参数补充。相同契约在目录中重复声明仍遵循既有收集器的去重规则。

补充是独立的 developer 消息，既有目录提醒不会将其抑制。完全相同的消息不重复追加；选择或 schema 改变时追加当前版本，不替换旧历史。参数提示只要求遵守当前字段名、类型、required 和其他约束，不进行别名映射、类型强制转换或自动重试。

例如，若当前 schema 要求 `target` 与 `message` 且禁止额外字段，模型提交 `agent_name` 与 `task` 会同时违反 required 和 additionalProperties。此例说明一种 schema/resource 错误来源，不能据此将所有同文错误归因为字段名，也不能将字符串长度误认为已触发资源限制。

认证发生在补充之前；目录提醒与参数提醒顺序处理时会同步清理已解压请求的 Content-Encoding/Content-Length。完整性保护、范围检查和有界 gzip/zstd 解码继续生效。响应原字节及工具调用/结果不被改写。

## 工具结果配对

此公开版本不包含特定聊天的历史修补。`function_call_output` 必须对应真实调用及其 `call_id`；引用文本、跨聊天提示和工具成功结果不能互相替代。对于 `missing_tool_call`，应由产生请求的客户端恢复完整且真实的调用/结果链。适配器不凭空生成调用、ID 或成功状态，不删除任意历史项来掩盖问题。

## 有界错误诊断

错误观察器只检查符合范围的未压缩 `text/event-stream`，先转发响应字节，再做观察。非 SSE、压缩响应、普通工具增量和正文不用于错误分类。观察失败不改写响应、不重试请求。

| 分类 | 含义 |
| --- | --- |
| `native_not_in_catalog` | 上游原生工具不在客户端目录 |
| `tool_outside_client_catalog` | 载体选择了客户端目录外工具 |
| `schema_or_resource` | 参数 schema 或验证资源限制失败 |
| `missing_tool_call` | 结果缺少相应调用 |
| `transport_invalid` | 载体格式不合法 |
| `request_unsupported` | 其他已知请求不支持错误 |
| `unknown_error` | 已识别错误事件，但无法安全细分 |

每事件上限 64 KiB、错误正文上限 8 KiB、去重错误桶上限 32；跨网络分片的 CRLF 仍按连续 SSE 事件累计预算，不会因分隔符被拆分而重置上限。计数饱和而不无限增长。日志仅包含固定分类/代码、计数以及允许的有限字段。默认只允许输出 `update_plan`、`list_connectors`、`run_officejs` 及其 `functions.` 形式；未知工具名被省略。绝不输出原始错误正文、参数、结果、凭据或聊天文本。

当核心只给出笼统的 schema/resource 错误，适配器不会反推具体工具、字段路径或错误参数。诊断分类、单元测试通过、真实上游成功和生产部署是不同证据，应分别报告。

## 增加真正的新工具

1. 实现可调用的本地函数、MCP 服务或受支持的宿主执行器，定义准确的资源身份与授权。
2. 从实际实现写出输入/输出 schema，说明副作用、失败行为和资源限制。
3. 在客户端注册后，按其实际目录核对精确名称、命名空间和 schema；不要假定裸名不变。
4. 测试合法输入、缺字段、错类型、额外字段、边界预算、拒绝授权、超时和断流。
5. 观察真实执行结果及调用 ID 配对，再报告工具接入完成。

Excel 工作簿工具需要真实工作簿或文件执行环境；连接器需要对应服务与账号授权；规划工具需要真实状态实现。相似名称不意味着可互换。此适配器不自动同步这些执行能力。
