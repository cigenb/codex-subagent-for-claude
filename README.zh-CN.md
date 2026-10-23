# codex-subagent-for-claude

[English](README.md) | 简体中文

在 Claude Code 里把 Codex 当子代理用：Claude 派任务，Codex 在后台跑，结果自动回到 Claude 的对话里。

两个 Python 文件，没有第三方依赖。目前只在 Windows 11 上验证过。

## 功能

- 异步派发：`codex_spawn` 立即返回，Claude 接着干自己的事。多个任务在同一个常驻的 Codex 进程里并行，不用每次冷启动
- 结果自动送回：Codex 做完后，结果在 Claude 下一次动作时出现在对话里，不用轮询。Claude 结束回合前会先收齐还没送回的结果
- 中途干预：`codex_steer` 追加指令，Codex 在当前步骤后转向；`codex_interrupt` 中断，已做完的部分保留；带 `thread_id` 可以接着问
- 代码审查：`codex_review` 调 Codex 自带的审查模式，不改文件、找缺陷、带文件行号
- 多窗口隔离：每个 Claude 窗口一个独立的 Codex 进程
- 模型独立：子代理默认 `gpt-6.1-sol` / 推理强度 `high` / 标准服务档，不受 Codex 桌面聊天设置影响；环境变量改默认值，也可以按任务传参（可选 GPT-6.1 Sol / GPT-6 Astra / GPT-6 Sol / GPT-6 Luna）

## 原理

```
Claude Code ──MCP──> server.py ──JSON-RPC──> codex app-server（常驻子进程）
Claude Code ──hook──> hook.py：任务结束后把结果注入回派出它的会话
```

## 前置条件

- Windows 11，Python 3.10+（`python` 在 PATH 里），Claude Code
- 已登录的 Codex。默认用 Codex 桌面应用自带的 `codex.exe`（`%LOCALAPPDATA%\OpenAI\Codex\bin\` 下，随应用更新），不需要打开应用；npm 安装的 `@openai/codex` 也可以，或用 `CODEX_SUB_BIN` 指定路径。两者共用 `~/.codex` 的登录态和配置

## 安装

克隆到任意目录，下面用 `<安装目录>` 代替。这几步让 Claude 或 Codex 代劳也行。

1. 注册 MCP 服务：

```bash
claude mcp add codex-sub -s user -- python "<安装目录>\server.py"
```

2. 把 hook 合并进 `~/.claude/settings.json`（JSON 里反斜杠写成 `\\`）：

```json
{
  "hooks": {
    "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "python \"<安装目录>\\hook.py\" UserPromptSubmit", "timeout": 10}]}],
    "PostToolUse":      [{"hooks": [{"type": "command", "command": "python \"<安装目录>\\hook.py\" PostToolUse", "timeout": 10}]}],
    "Stop":             [{"hooks": [{"type": "command", "command": "python \"<安装目录>\\hook.py\" Stop", "timeout": 10}]}]
  }
}
```

3. 可选：在 `permissions.allow` 里加 `"mcp__codex-sub"`，否则每次调用都会弹确认。

重启 Claude Code 会话生效。只想先试试、不改全局配置：`claude --plugin-dir "<安装目录>"`。

## 用法

直接跟 Claude 说就行，它知道什么时候该派：

> 把 auth 模块的重构派给 Codex，你先去改 API 测试，等它完了再合并。

> 让 Codex 审一下我刚才的改动。

> 派两个 Codex 任务并行：一个修 add，一个修 mul，各自开 worktree。

| 工具 | 作用 |
|---|---|
| `codex_spawn` | 派任务。`wait_s` 先等一会儿，`thread_id` 续问，`worktree` 在独立分支里干，`model` / `effort` 按任务覆盖 |
| `codex_review` | 代码审查：未提交改动 / 相对基准分支 / 某个提交 / 自定义说明 |
| `codex_wait` | 等结果，超时只返回状态不杀任务 |
| `codex_status` | 列任务，或看某个任务的详情和全文 |
| `codex_steer` | 给运行中的任务追加指令 |
| `codex_interrupt` | 中断，保留已产出的部分 |

- 任务默认是临时会话，不进 Codex 历史；子代理能用 `~/.codex` 里的技能和插件
- 不关心结果的后台活传 `detach=true`，Claude 结束回合时不等它

## 权限

子代理默认**不带沙箱**（`sandbox=danger-full-access`）且**不审批**（`approvalPolicy=never`）：能改任何文件、跑任何命令，不会停下来问你；偶尔遇到需要人回答的追问会自动跳过，让它自己决定，任务不会卡住等人。这么设是因为 Codex 的 Windows 沙箱用受限账户执行命令，读不到装在用户目录下的 Python、pnpm、uv 等工具，子代理连测试都跑不了。

`codex_review` 用同样的默认：审查模式本身不改文件，但要跑 `git` 等命令。没有按任务选沙箱的参数：`read-only` 启用的是同一个受限沙箱，沙箱里任何命令都起不来，Codex 连读文件都做不到。默认沙箱可用 `CODEX_SUB_SANDBOX` 改。

## 配置

| 环境变量 | 默认 |
|---|---|
| `CODEX_SUB_BIN` | 自动查找 |
| `CODEX_SUB_SANDBOX` | `danger-full-access` |
| `CODEX_SUB_MODEL` / `CODEX_SUB_EFFORT` / `CODEX_SUB_SERVICE_TIER` | `gpt-6.1-sol` / `high` / `default` |
| `CODEX_SUB_STATE_DIR` | `%LOCALAPPDATA%\codex-sub`（日志和会话状态；server 与 hook 要设同一个值） |

## 说明

- 依赖 `codex app-server` 的部分 experimental 接口，Codex 更新可能改协议；在 Codex 0.163 上验证
- 每个 Claude 会话常驻一个 app-server 进程（约 150 MB），会话结束随之退出

测试：`python tests/smoke.py`（会真的调 Codex，约 3 分钟）。

卸载：`claude mcp remove codex-sub -s user`，再删掉 settings.json 里的三条 hook。

## License

MIT
