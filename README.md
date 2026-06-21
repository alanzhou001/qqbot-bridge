# qqbot

NapCat 接管 QQ 个人号，独立 Python Bridge 通过 OneBot v11 WebSocket 接收 QQ 消息，再调用 OpenClaw `qqbot` agent 回答高考志愿咨询。

运行时数据和后续本地 RAG 资料默认放在 File 盘：

```text
qqbot-data/
├── qqbot.sqlite3
├── knowledge/
├── rag_context/
├── rag_index/
└── openclaw/workspaces/qqbot/
```

当前本机 OpenClaw 已配置为本机 Gateway：`对应监听端口`，默认本地记忆检索 embedding 走 Ollama `qwen3-embedding:0.6b`。Bridge 不接入 OpenClaw 的 QQ channel，只调用固定 `qqbot` agent。

## 1. 初始化 File 盘数据目录

```bash
chmod +x scripts/init-data-dirs.sh
scripts/init-data-dirs.sh
```

这一步会创建：

```text
qqbot-data/knowledge/
qqbot-data/rag_context/
qqbot-data/rag_index/
qqbot-data/openclaw/workspaces/qqbot/
```

## 2. OpenClaw Agent

当前 OpenClaw agent 使用 `qqbot`。确认 agent 存在：

```bash
openclaw agents list
```

测试：

```bash
/opt/homebrew/bin/openclaw agent \
  --agent qqbot \
  --session-key test \
  --channel qqbot \
  --message "我是浙江考生，物化生，620分，位次2万左右，想学计算机，帮我看看志愿"
```

## 3. 配置 NapCat

在 NapCat WebUI 中启用 OneBot v11 WebSocket Server：

```text
Host: 127.0.0.1
Port: 3001
Access Token: 强随机 token
```

## 4. 配置 Bridge

```bash
cd proj/qqbot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，至少修改：

```text
NAPCAT_ACCESS_TOKEN=你的NapCatToken
BOT_QQ=你的QQ号
OPENCLAW_GATEWAY_TOKEN=你的OpenClaw Gateway Token
ALLOWED_GROUPS=正式使用时填写群号白名单
```

如果你的 OpenClaw 使用 password 而不是 token，则填写 `OPENCLAW_GATEWAY_PASSWORD`。

数据库默认写入 `qqbot-data/qqbot.sqlite3`。如果 File 盘路径变化，改 `.env` 中的 `DATA_DIR` 和 `DB_PATH`。

## 5. 本地 RAG 预留接口

Bridge 已预留两种本地资料接入方式：

- SQLite 表：`rag_documents` / `rag_chunks`，适合后续把本地模型 RAG 的索引元数据和 chunk 结果写入数据库。
- Prompt 上下文文件：`qqbot-data/rag_context/*.md`，适合先由本地 RAG 进程生成短摘要，再交给 OpenClaw。

Bridge 当前会按顺序读取：

```text
qqbot-data/rag_context/shared.md
qqbot-data/rag_context/groups/<群号>.md
qqbot-data/rag_context/users/<QQ号>.md
```

读取到的内容会被限制在 `RAG_CONTEXT_MAX_CHARS` 内，再注入 `qqbot` prompt。后续本地 RAG 可以独立演进，不需要把 QQ 协议层塞进 OpenClaw Gateway。

新增或更新 `qqbot-data/knowledge/` 下的录取资料后，运行：

```bash
python scripts/update-knowledge-index.py
```

这会刷新 `qqbot-data/rag_context/shared.md`，让 `qqbot` 知道可查阅的本地资料路径。

如果要把投档线/省控线导入 SQLite，并把非表格资料用本地 Qwen embedding 写入向量库，运行：

```bash
python scripts/import-knowledge.py
```

Bridge 会在高考志愿问题里先查本地 SQLite 和 Qwen 向量结果，再把命中内容交给 OpenClaw。

导入脚本处理规则：

- `.xls/.xlsx/.csv/.tsv`：通过 LibreOffice/pandas 抽取表格，投档线写入 `admission_line_rows`。
- 可抽取文本/表格的 PDF：通过 `pdfplumber` 抽取。
- 图片型 PDF：自动尝试 `pdftoppm + tesseract` OCR，再把文字写入向量 chunk。中文 OCR 需要本机 tesseract 安装 `chi_sim` 语言包；如果只有 `eng`，中文图片 PDF 的 OCR 效果会很差。
- 招生折页、专业介绍等非结构化资料：切 chunk 后调用本地 Ollama `qwen3-embedding:0.6b`，写入 `knowledge_text_chunks`。

注意：当前库里投档线已经能结构化查询；如果一分一段表没有被解析出 `score_segment_rows`，Bridge 不会把分数强行换算成位次，会要求用户补充位次或提示需要联网/官方数据核对。

## 6. 手动运行

```bash
cd proj/qqbot
source .venv/bin/activate
python src/qqbot_bridge.py
```

看到 `Connected to NapCat` 后，说明 QQ 协议层已经接通。用另一个 QQ 号私聊测试：

```text
我是浙江考生，物化生，620分，位次2万，想学计算机，怎么填志愿？
```

群聊测试：

```text
@你的QQ 高考志愿怎么冲稳保？
```

群聊只在 `@你的QQ` 时触发；普通关键词不会自动触发。被 @ 后，如果内容与高考志愿相关会按专业规则回答，其他内容可以正常闲聊。
Bridge 会在群聊回复中引用原消息，并在发送前清理常见 Markdown 标记，确保 QQ 里显示为纯文本。

## 7. 配置 macOS LaunchAgent

确认手动运行成功后：

```bash
chmod +x scripts/start-bridge.sh
mkdir -p ~/Library/Logs/qqbot
cp launchd/ai.openclaw.qqbot-bridge.plist ~/Library/LaunchAgents/
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.qqbot-bridge.plist
launchctl kickstart -k gui/$(id -u)/ai.openclaw.qqbot-bridge
```

查看状态和日志：

```bash
launchctl print gui/$(id -u)/ai.openclaw.qqbot-bridge | head -80
tail -f ~/Library/Logs/qqbot/stdout.log
tail -f ~/Library/Logs/qqbot/stderr.log
```

停止：

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.qqbot-bridge.plist
```

## 安全边界

- Bridge 固定调用 `OPENCLAW_AGENT=qqbot`，不要转给默认 agent。
- 正式群聊建议设置 `GROUP_REQUIRE_MENTION=true` 和 `ALLOWED_GROUPS=群号白名单`。`GROUP_REQUIRE_MENTION=true` 表示群聊只在 @ 机器人时回复，不使用关键词触发。
- QQ 用户输入只作为 prompt 传给 OpenClaw，`subprocess.run` 使用参数列表，不走 shell。
- Agent 提示词明确禁止承诺录取、编造投档线、招生计划或专业组代码。
