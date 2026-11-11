# dingtalk-mcp-bot

把 **钉钉群机器人**（@机器人）接到 [mcp-gateway](../../Library/git-root/thriftTester/mcp-gateway) 上：
群里 @机器人 发一句话，机器人调用 mcp-gateway 的元工具（`search_api` / `list_methods` /
`describe_method` / `get_servers`），把接口方法、签名、路由直接回帖。

不需要公网地址：用钉钉 **Stream 模式**（机器人主动连钉钉的 WebSocket），本机或内网跑就行。

```
钉钉群  ──@机器人──▶  bot.py  ──stdio JSON-RPC──▶  mcp-gateway  ──HTTP──▶  thriftTester
   ▲                    │
   └──────markdown──────┘
```

## 1. 前置：创建并配置钉钉机器人

1. 钉钉开发者后台 → **应用开发 → 企业内部应用** → 新建应用。
2. 添加 **机器人** 能力：
   - **消息接收模式**：选 **Stream 模式**（不是 HTTP 回调）。
   - 记下 **AppKey / AppSecret**（即本项目 `.env` 里的 `DINGTALK_CLIENT_ID` /
     `DINGTALK_CLIENT_SECRET`）。
3. 发布应用（内部应用可直接发布），然后在目标群里 **添加机器人**。
4. 群设置里机器人默认只接收 @ 它的消息，这正好。

> 需要「企业内部机器人」才能收消息。老的「群自定义 webhook 机器人」是单向的，只能发不能收。

## 2. 安装

```bash
cd /Users/xmly/dingtalk-mcp-bot
python3 -m venv .venv
./.venv/bin/pip install -r requirements.txt
cp .env.example .env     # 然后填 DINGTALK_CLIENT_ID / DINGTALK_CLIENT_SECRET
```

`mcp-gateway` 用你机器上已有的那份即可，`.env` 里 `GATEWAY_BIN` / `GATEWAY_CONFIG`
默认已指向当前环境；stdio 模式免登录，机器人侧身份通过 `GATEWAY_LOCAL_USER` 指定，
便于在网关审计里区分调用方。

## 3. 先自检（不需要钉钉凭据）

```bash
./.venv/bin/python smoke_test.py                          # 只握手 + 列工具
./.venv/bin/python smoke_test.py --search 修改用户昵称      # 再跑一次真实检索
./.venv/bin/python smoke_test.py --env test --search uid
```

能打印出 5 个工具和候选方法，说明与 mcp-gateway 的链路已经通。

## 4. 启动机器人

```bash
./.venv/bin/python bot.py
```

然后在群里 @机器人 发消息。

## 5. 指令

| 你说 | 机器人动作 |
|---|---|
| `/help` | 使用说明 |
| `/tools` | 列出 mcp-gateway 暴露的工具 |
| `/search 修改用户昵称` | `search_api` 语义检索，回帖命中方法 |
| `/search uid env=test` | 指定环境（默认取 `DEFAULT_ENV`，初始为 product） |
| `/call list_methods {"iface":"...","artifact_id":"standalone-pom"}` | 直接调用某个工具 |
| `修改用户昵称`（不加指令） | 等同于 `/search 修改用户昵称` |

### 检索后的多轮流程

检索结果按会话保存（2 小时有效，按最后一次交互滑动计时），落在本地 SQLite
（默认 `data/sessions.db`，可用 `SESSION_DB` 覆盖），**机器人重启后仍能接着上一轮继续**。
会话按群隔离（`conversation_id`），不按人区分。可以连续追问：

| 你说 | 机器人动作 |
|---|---|
| `第2条` / `第4个方法` / `选 3` / `第二条` | 选定当前组的方法，回显参数类型与路由，提示补参数和环境 |
| `换一组` / `换一组方法` / `下一组` | 换一批没展示过的候选；本轮候选用尽后会改写 query 重新做一次向量检索 |
| `currentLoginedUid=586218703 env=test` | 按请求参数模板生成请求体预览 |
| `确认` | 只读方法在放行 `invoke_rpc` 后执行；写方法一律不执行，提示走 IDE |
| `取消` | 结束本次检索流程，不执行任何调用 |

「换一组」的实现背景：mcp-gateway 的 `search_api` 只支持 `limit`（网关上限 10），
没有 `offset` / `exclude`，所以一次取满候选池后本地分页展示；池用尽时用改写过的
query 重新发起向量检索并按 `iface#method#artifact_id` 去重，保证换出来的是新候选。
回帖会标注本组来源（「本轮检索候选」或「重新向量检索」）。

环境也可以在任意消息里用 `env=test` / `--env test` 覆盖，检索和执行阶段都认中文
「线上 / 生产 / 测试 / 预发」。执行环境必须显式指定，除非检索那句话里已经带过。

## 6. 安全边界

- 机器人**默认只开放只读工具**（`search_api` / `list_methods` / `describe_method` / `get_servers`），
  `invoke_rpc` 需要把 `.env` 的 `ALLOW_INVOKE_RPC` 设为 `true` 才放行。
- 即便放行 `invoke_rpc`，生产环境的 write / destructive 方法仍会被 mcp-gateway 拦下，
  因为它强制要求 `confirm_token`（token 由 `describe_method` 现场签发、与参数绑定、5 分钟有效），
  聊天里无法完成这一步 —— 危险操作请继续走 IDE 里的人工确认流程。
- `GATEWAY_LOCAL_ALLOW_DESTRUCTIVE` 默认 `false`，不建议在机器人侧打开。
- 钉钉应用凭据只放在 `.env`（已 gitignore），不要提交。

## 7. 排障

| 现象 | 原因 / 处理 |
|---|---|
| 启动报 `缺少钉钉凭据` | `.env` 没填或没复制成功 |
| 群里 @机器人 无反应 | 机器人「消息接收模式」不是 Stream；或应用未发布/未加进群 |
| 回帖「网关调用失败：mcp-gateway 未运行」 | `GATEWAY_BIN` 路径不对，或该虚拟环境缺少依赖 |
| `env` 相关报错 | 网关强制要求显式 env，用 `env=test` / `env=product` 指定 |
| 长回复显示不全 | 调大 `.env` 的 `MAX_REPLY_CHARS`，或让检索少返回几条 |
| 想看协议细节 | `LOG_LEVEL=DEBUG`，`bot.py` 会把网关 stderr 转进日志 |

## 8. 文件

| 文件 | 作用 |
|---|---|
| `bot.py` | 钉钉 Stream 机器人 + 指令路由 |
| `mcp_client.py` | 手写 MCP stdio 客户端（JSON-RPC 2.0，按行分隔） |
| `config.py` | 环境变量配置 |
| `search_flow.py` | 检索结果会话状态（第N条选择 / 换一组 / 补参数环境 / 请求体预览）；SQLite 持久化 |
| `data/sessions.db` | 运行时自动生成，存检索会话状态（已 gitignore） |
| `ops_actions.py` | ops-quick-actions 快捷 RPC 接入（意图识别 + plan→确认→run） |
| `smoke_test.py` | 不依赖钉钉的链路自检 |

## 9. ops 快捷操作（线上运营）

复用 `ops-quick-actions` skill 的 `actions.yaml` 分发器（`scripts/dispatch.py`），
把群里 @机器人 的话术直接转成线上 RPC，无需走 IDE。安全边界与 skill 完全一致：
**write / destructive 必须先 plan 回显请求体，再等人在群里回复「确认 / 可以」才执行**，
token 与「操作 + 环境 + 参数」绑定，改参数即失效。

| 你说 | 机器人动作 |
|---|---|
| `/ops list` | 列出已登记操作（一键认证 / 查询认证信息 / 改手机号 / 强制注销 / 重置娱乐厅房间） |
| `/ops show <操作>` | 看该操作的参数要求 |
| `/ops <操作> k=v ... [--env test]` | 预演（plan，回显请求体与确认 token） |
| `/ops <操作> k=v ... --confirm <token>` | 直接执行（带 plan 签发的 token） |
| `给 uid 123 主播认证` / `改手机号 uid=1 mobile=...` / `强制注销 uid=1` | 自然语言别名命中，等价于先 `/ops ...` plan |
| 待确认时回复 `确认` / `可以` | 执行上一步 plan |
| 待确认时回复 `取消` | 放弃，不执行 |

自然语言的参数支持三种写法（可混用），机器人会自动识别：

- **显式 `k=v`**：`换绑 uid=594301073 mobile=17682488695`
- **中文标签**：`594301073 换绑成手机号：176 8248 8695`（手机号里的空格会自动合并）
- **位置推断**（按字段类型补必填项）：`594301073 强制注销`

环境同样认中文字眼：**线上 / 生产 → `product`**，**测试 / 预发 → `test`**；
标准写法 `env=test` / `--env test` 仍然可用。

> 命中 ops 但必填参数不全时，**不会硬跑**：机器人会提示缺哪些参数，
> 并**回退到全局接口检索**（`search_api`）。没命中任何 ops 别名时，同样走全局检索。

配置（均有默认值，一般不改）：

- `OPS_DISPATCH_PY`：调用 dispatch.py 的 Python（必须用含 PyYAML / certifi 的系统 Python）。
- `OPS_DISPATCH_SCRIPT`：dispatch.py 路径（默认 `~/.codebuddy/skills/ops-quick-actions/scripts/dispatch.py`）。
- `OPS_ALLOWED_SENDERS`：可选，逗号分隔的钉钉 `senderId` / `senderStaffId` 白名单；
  留空表示群里所有人都能触发（write/destructive 仍需二次确认）。

新增操作只改 skill 的 `actions.yaml`，本机器人重启后自动加载，无需改代码。

## 开机自启动（macOS）

用 `launchd` LaunchAgent 实现「登录即拉起 + 崩溃自动重启」，无需手动 `nohup`：

- 启动脚本：`run_bot.sh`（含重复实例防护，避免两个 DingTalk Stream 连接冲突）
- 注册文件：`~/Library/LaunchAgents/com.xmly.dingtalk-mcp-bot.plist`
- 日志：`logs/launchd.out.log` / `logs/launchd.err.log`

常用管理命令：

```bash
# 注册并立即启动（首次）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.xmly.dingtalk-mcp-bot.plist
# 停止并从自启列表移除
launchctl bootout  gui/$(id -u)/com.xmly.dingtalk-mcp-bot
# 仅停当前进程（launchd 会按 KeepAlive 立即重启，用于热重载）
launchctl kickstart -k gui/$(id -u)/com.xmly.dingtalk-mcp-bot
```

> 注：macOS 的用户级 LaunchAgent 在**登录**时启动（不是输入密码前的纯开机）。
> 机器人依赖用户会话的网络与 `~/.env` 凭据，故用用户级 Agent 而非系统级 LaunchDaemon。
> 改代码后执行 `launchctl kickstart -k ...` 即可热重载，无需重新 bootstrap。
