# 提示词模板

[English](PROMPTS.md) | **简体中文**

youchat 的设计初衷是**通过和 agent 对话来驱动**。把下面任意一段粘贴到任何装了 youchat MCP 服务器的
Claude Code / Codex / Antigravity 对话里，把 `<...>` 换成你的内容即可。agent 会把你的话转换成
youchat 工具调用（`find_sessions`、`create_team`、`add_member`、`send_message`……）。

> [!TIP]
> 告诉总控**为什么**，而不只是**谁来做**。章程里写清目标、每个角色的职责、交接顺序和验收标准，小组才能在你不在场时自己运转。

---

## 0. 安装

把这段交给你 Mac 上的任意 agent，它会替你执行命令。

```text
帮我从 <youchat 仓库路径> 安装 youchat。
部署方式：<"纯本地，全部在这台 Mac 上" | "hub 放在我的服务器 <ssh 别名> 上，两边都有 agent">。

步骤：执行 ./scripts/build.sh 和测试，把 dist/youchat 复制到 ~/.local/bin/youchat；
纯本地：`youchat service install --hub`；
有服务器：`./scripts/install-remote.sh <ssh 别名>`，然后 `youchat service install --tunnel <ssh 别名>`。
接着在每台机器上运行 `youchat setup`，为我已安装的 CLI（claude / codex / agy）执行打印出来的 `mcp add` 命令，
加上打印出来的 Codex config.toml 配置，并运行 `youchat hook install-agy`。
最后运行 `youchat status`，告诉我装了什么、哪些需要我手动处理。不要用 root 执行任何操作。
```

## 1. 用我已有的对话组建小组

```text
用 youchat 组建一个叫 <TEAM> 的小组。
成员是我已有的对话：<名字-1>、<名字-2>、<名字-3>。用 find_sessions 找到它们（同名的取最近活跃的那个）。
分工：
- <名字-1>：<例如：审核、验收、下发任务大纲>
- <名字-2>：<例如：任务理解、计划、开发、测试>
- <名字-3>：<例如：运行实验/案例>
目标：<一两句话>。
交接顺序：<例如：总控派任务 → 开发实现并测试 → handoff 给运行 → result 交回开发 → review_request 给总控 → review>。
后台运行时的权限：permission_mode=<default | acceptEdits | bypass>。

先给我起草章程和成员列表，在我确认前不要创建任何东西。
我确认后：create_team，逐个 add_member，然后给 <名字-?> 发开工任务。
```

## 2. 用全新的对话组建小组

```text
用 youchat 创建一个叫 <TEAM> 的小组，成员都是新对话（session_id 填 "new"）：
- <TEAM>-DEV-CLD-1：cli=claude，model=<...>，effort=<low|medium|high|xhigh|max>，cwd=<目录>，职责：<...>
- <TEAM>-RUN-CDX-1：cli=codex，model=<...>，ultra=<true|false>，cwd=<目录>，职责：<...>
- <TEAM>-LEAD-AGY-1：cli=antigravity，boost=true，cwd=<目录>，职责：<...>
目标：<...>。先起草章程等我确认，然后建组并发出第一个任务。
新对话会在第一次收到消息时自动创建。
```

## 3. 让一个已经开着的对话入组

在那个对话里直接说：

```text
你是 youchat 小组 <TEAM> 的 <名字>。调用 join_team（职责："<你的职责>"），
读完入组说明，之后按小组章程工作。
```

## 4. 下发工作

```text
通过 youchat 给 <成员> 发一条 kind=task 的消息，内容是我们刚才定下的方案：<方案摘要，或"上面的方案">。
写清文件路径、约束条件和"完成"的标准，并要求完成后向 <总控> 提交 review_request。
不用等它回复，小组会在需要时唤醒你。
```

## 5. 暂停 / 恢复群聊

```text
暂停 youchat 小组 <TEAM>。      （或：恢复 youchat 小组 <TEAM>，并通知所有人从中断处继续。）
```

## 6. 进度汇报

```text
用 youchat（get_team、get_thread、list_agents）给我简要汇报 <TEAM> 小组的进度：
<某时间>以来谁做了什么、哪些事在等谁、有没有需要我拍板的。
```

## 7. 常设授权（让小组别再来问你）

```text
在 <TEAM> 小组的章程里加一节"常设授权"：<例如：两个集群的算力都可以直接使用，无需请示；禁止推送到 main>。
然后通知全组（to "team:<TEAM>"）。
```

## 8. 让每个 agent 都成为好队友

把 [AGENTS_SNIPPET.md](AGENTS_SNIPPET.md) 追加到你的 `CLAUDE.md`、`AGENTS.md` 或 Antigravity 规则里。

---

### 命名建议

- 用 `<小组>-<角色>-<CLI>-<序号>`，例如 `APP-DEV-CLD-1`、`APP-RUN-CDX-1`、`APP-LEAD-AGY-1`。agent 会看到这些名字，可读性好的名字能帮它们正确投递消息。
- 在 Claude Code 里，用 `/rename <名字>` 给已有对话起名，之后 `find_sessions` 就能找到它。
