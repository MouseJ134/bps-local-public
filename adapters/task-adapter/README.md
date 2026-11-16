# BPS Task Adapter 0.6.0-dev.2

独立的 Python 请求适配器，用于在 Responses 客户端和支持 CPA-BPS 载体协议的本地服务之间，补充当前工具目录、明确传输规范，并提供有界错误诊断。

这是适配器的开发版本，**不是官方 CPA-BPS 插件升级**。它不包含或修改 `cpa-bps.so`，不自动安装或注册上游工具。

## 功能

- 每次请求按真实声明合并工具契约，追加完整的精确名称/类型清单；冲突或超限时保留原始请求体。
- 明确 FUNCTION、FUNCTION_CODE、FUNCTION_CMD、CUSTOM 的载体规范，不根据字段名猜测模式。
- 可按 session 显式选择函数工具，额外提示当前完整参数验证契约，避免旧历史中的参数名覆盖当前 schema。
- 显式选择通用、软件开发、学习笔记或文档制作提示；不从聊天内容猜测任务或增加权限。
- 观察未压缩 SSE 的已知错误类别，转发原始响应字节；不保存错误正文、参数、凭据或聊天历史。
- 保持 loopback 访问、独立凭据、Host/Origin 校验、禁止重定向和不自动重试的边界。

版本变化见 [更新记录](CHANGELOG.md)。完整规范见 [工具接口契约](docs/INTERFACE_CONTRACT.md)，发布与隐私边界见 [安全说明](docs/SECURITY.md)。

## 安装与配置

需要 Python 3.14（标准库 `compression.zstd` 用于有界请求解压）。从仓库根目录进入本目录：

```bash
cd adapters/task-adapter
python -m venv .venv
```

Linux/macOS 激活环境使用 `. .venv/bin/activate`；Windows PowerShell 使用 `.\.venv\Scripts\Activate.ps1`。也可以直接使用该虚拟环境中的 Python。随后安装依赖：

```bash
python -m pip install -r requirements.txt
```

在**仓库外**准备两份凭据文件，并限制文件访问权限：

- 客户端凭据 JSON：`{"token":"REPLACE_WITH_A_LONG_RANDOM_CLIENT_TOKEN"}`。客户端发送 `Authorization: Bearer <该 token>`。显式指定的文件也支持 `tokens.access_token` 或 `OPENAI_API_KEY`；程序不会自动扫描账号目录，也不读取 refresh/id token。
- 上游密钥文件：仅一行上游服务认可的密钥。适配器用它替换客户端凭据，上游不会收到原客户端 Authorization。

以下命令中的文件路径是示例，须指向自己准备的文件；服务默认监听 `127.0.0.1:8319`：

```bash
python src/auth_bridge.py --client-auth-file ../../../private/client-auth.json --upstream-key-file ../../../private/upstream-key.txt --upstream-base http://127.0.0.1:8317
```

相对路径以当前工作目录解析。上游只允许不同端口的数字回环 HTTP(S) origin，例如 `http://127.0.0.1:8317`；不允许域名、用户信息、路径、查询或片段。客户端使用 `http://127.0.0.1:8319/v1` 作为 API base，并使用 HTTP/SSE。WebSocket 请求认证后返回 426，需要客户端支持回退。

| CLI 参数 | 环境变量 | 默认值/要求 |
| --- | --- | --- |
| `--listen-port` | `BPS_ADAPTER_LISTEN_PORT` | `8319`；1024–65535，不能与上游端口相同 |
| `--upstream-base` | `BPS_ADAPTER_UPSTREAM_BASE` | `http://127.0.0.1:8317` |
| `--client-auth-file` | `BPS_ADAPTER_CLIENT_AUTH_FILE` | 必须显式提供 |
| `--upstream-key-file` | `BPS_ADAPTER_UPSTREAM_KEY_FILE` | 必须显式提供 |
| `--policy-file` | `BPS_ADAPTER_POLICY_FILE` | 随包 `src/tool_contract_policy.json`，默认关闭 |
| `--stream-idle-timeout` | `BPS_ADAPTER_STREAM_IDLE_TIMEOUT` | `300` 秒；1–3600 |

CLI 显式参数覆盖对应环境变量。不要把密钥值写在 CLI 参数、仓库或 issue 中。

## 启用目录提醒与任务提示

随包策略关闭时，认证/HTTP 转发仍可工作，不追加目录提醒。把 `src/tool_contract_policy.example.json` 复制到仓库外，修改示例 session 和模型名称，再通过 `--policy-file` 指定它：

```json
{
  "version": 2,
  "enabled": true,
  "scope": "session_ids",
  "session_ids": ["example-session"],
  "models": ["YOUR_MODEL"],
  "profile_by_session": {"example-session": "software_development"}
}
```

将 `example-session` 换成客户端实际 `Session-Id`，`YOUR_MODEL` 换成请求中实际使用的模型名。模型必须列入白名单；Session-Id 缺失、`tool_choice` 不为 `auto`、类型/形状不支持时均不追加提醒。策略文件不应纳入版本控制。

`scope` 可选 `session_ids` 或 `all_bps`。`all_bps` 放宽会话名单范围，但仍要求请求存在 Session-Id、模型匹配和其他检查通过；profile 只从明确列出的 session 映射获取，其余为 `generic`。可选 profile 为 `generic`、`software_development`、`study_notes`、`document_creation`。策略热读取且有修改时间缓存；未知字段、无效版本/映射或不可读取的策略会关闭提醒。

## 启用参数 schema 提醒（策略 v3）

目录中存在工具，不代表模型会使用正确参数。需要针对已确认问题的工具补充完整参数契约时，使用 v3 策略；v1/v2 仍保持原行为，不启用这项补充：

```json
{
  "version": 3,
  "enabled": true,
  "scope": "session_ids",
  "session_ids": ["example-session"],
  "models": ["YOUR_MODEL"],
  "profile_by_session": {"example-session": "software_development"},
  "parameter_tools_by_session": {
    "example-session": ["collaboration.followup_task"]
  }
}
```

使用自己的 session、模型及**当前客户端确实声明的完整工具名**。示例名不会注册工具，也不产生执行能力。随包实际策略仍关闭。每个 session 最多选择 8 个工具，完整补充提醒总长度最多 16 KiB；选择项必须全部是无歧义的 function 契约。缺失、custom 类型、冲突或超限时整个参数补充原样跳过，不加入部分契约。

参数提醒独立于已有 v1/v2/v3 目录提醒。完全相同的补充不重复追加；当前 schema 改变时追加新摘要对应的补充，并保留历史。它展示当前规范化的完整验证 schema，而不是只列工具名。不会把 `agent_name` 自动改成 `target`，也不会把 `task` 自动改成 `message`；正确字段始终由当前目录决定。

新增日志字段 `parameter_guard_added` 仅表示本次增加了参数提醒。它不能证明模型输出合法、工具已执行或会话已恢复。实际参数校验仍由插件负责。

## 开发与自动测试

在适配器目录、已安装依赖的虚拟环境中运行：

```bash
python run_tests.py --report test-results.json
python scripts/build_zip.py --output dist
```

`--verbose` 可显示每项测试。测试涵盖目录/命名空间合并、schema 摘要、策略范围、任务 profile、错误观察与本地 HTTP/SSE 集成。报告写入 `test-results.json`；打包输出 `dist/task-adapter-0.6.0-dev.2.zip` 和对应 `.zip.sha256`。ZIP 是可审查的 Python 源码包，不是可替换 `cpa-bps.so` 的插件二进制。

GitHub Actions 工作流 [Task Adapter](../../.github/workflows/task-adapter.yml) 在 Linux 和 Windows 的 Python 3.14 环境测试并构建。提交后应查看该提交实际运行的结果；工作流文件存在不等于已经运行通过。CI 不使用生产凭据、真实 BPS 服务或真实工具执行器，不会自动部署。

## 本地验收与回滚

启用真实客户端前，先用测试环境核对 `/v1/models`、普通响应、流式终止事件、一次真实工具调用及其返回。HTTP 200、请求计数或出现工具名都不足以证明完整调用成功；应检查实际执行结果和最终事件。真实上游测试使用自己的已授权环境，不需要把凭据上传到 CI。

此目录不包含自动部署/重启生产服务脚本。需要试运行时使用独立端口和配置；切换前保留客户端旧 API base 与凭据配置。回滚时停止适配器并恢复之前的 API 路径/配置即可；正在传输的请求可能中断，应在空闲时切换。

## 能力边界

目录提醒是提示层措施，不能保证模型不再生成目录外工具。参数 schema 与资源预算仍由核心插件校验；插件未披露的具体拒绝参数不能由适配器还原。复制工具名称和 schema 不会产生客户端执行器或工作簿/连接器权限。

本公开版本不包含针对特定聊天的历史修补。缺少真实 `call_id` 对应调用时，必须修正客户端请求链，不能伪造工具调用或成功结果。

本目录的自动测试使用合成数据和本地模拟服务器。测试结果、真实上游验收、生产部署状态应分别记录，不能互相替代。

## 许可与来源

适配器按仓库根目录 [LICENSE](../../LICENSE) 的 baseline project license 发布；第三方依赖与原有项目保留各自许可。原 `bps-local` CLI 与这个 Python 前置适配器是不同组件，本文不声称它们是同版 CPA-BPS 插件源码。
