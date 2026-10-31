# Prompt templates

**English** | [简体中文](PROMPTS.zh-CN.md)

youchat is meant to be driven **by talking to an agent**. Paste one of these into any Claude Code /
Codex / Antigravity session that has the youchat MCP server, and replace the `<...>` placeholders.
The agent turns your words into youchat tool calls (`find_sessions`, `create_team`, `add_member`,
`send_message`, …).

> [!TIP]
> Give the coordinator the *why*, not only the *who*. A charter that states the goal, each role's
> responsibilities, the hand-off order and the acceptance criteria is what lets the team run without you.

---

## 0. Install

Give this to an agent on your Mac. It runs the commands for you.

```text
Install youchat for me from <path-to-youchat-repo>.
Setup: <"local only, everything on this Mac" | "hub on my server <ssh-alias>, agents on both sides">.

Steps: run ./scripts/build.sh and the tests, copy dist/youchat to ~/.local/bin/youchat;
local only: `youchat service install --hub`;
with a server: `./scripts/install-remote.sh <ssh-alias>`, then `youchat service install --tunnel <ssh-alias>`.
Then run `youchat setup` on each machine and execute the printed `mcp add` commands for the CLIs I have
(claude / codex / agy), add the printed Codex config.toml block, and run `youchat hook install-agy`.
Finish with `youchat status` and tell me what was installed and anything that needs my action.
Don't run anything as root.
```

## 1. Form a team from sessions I already have

```text
Use youchat to form a team called <TEAM>.
Members are my existing sessions: <NAME-1>, <NAME-2>, <NAME-3>. Use find_sessions to locate them
(take the most recent one if a name appears more than once).
Roles:
- <NAME-1>: <e.g. reviews, accepts, assigns task outlines>
- <NAME-2>: <e.g. understands the task, plans, implements, tests>
- <NAME-3>: <e.g. runs the experiments / cases>
Goal: <one or two sentences>.
Hand-off order: <e.g. lead assigns → dev implements & tests → handoff to runner → result back to dev →
review_request to lead → review>.
Default model & effort per session (confirm each session's model and effort level with me first):
- Antigravity (agy): model=gemini-4-argon-high, effort=high, boost=true
- Codex (codex): model=gpt-6.1-sol, effort=xhigh
- Claude Code (claude): model=claude-opus-5-5, effort=high
Background turns may use permission_mode=<default | acceptEdits | bypass>.

Draft the charter and the member list (including each session's model and effort) for me first. Create nothing until I approve.
After I approve: create_team, add_member for each, then send the kickoff task to <NAME-?>.
```

## 2. Form a team with brand-new sessions

```text
Use youchat to create a team called <TEAM> with NEW sessions (session_id "new"):
- <TEAM>-DEV-CLD-1: cli=claude, model=claude-opus-5-5, effort=high, cwd=<dir>, role: <...>
- <TEAM>-RUN-CDX-1: cli=codex, model=gpt-6.1-sol, effort=xhigh, cwd=<dir>, role: <...>
- <TEAM>-LEAD-AGY-1: cli=antigravity, model=gemini-4-argon-high, effort=high, boost=true, cwd=<dir>, role: <...>
Goal: <...>. Draft the charter and ask me to confirm each session's model and effort level, then create the team and send the first task.
The sessions are created automatically the first time they receive a message.
```

## 3. Put an already-open session into a team

Say this inside the session itself:

```text
You are <NAME> in youchat team <TEAM>. Call join_team (role: "<responsibilities>"),
read the briefing, and follow the team charter from now on.
```

## 4. Hand out work

```text
Through youchat, send <MEMBER> a kind=task message with the plan we just agreed on:
<summary or "the plan above">. Include file paths, constraints and what "done" means,
and ask for a review_request to <LEAD> when finished. Don't wait for the answer: the team will wake you.
```

## 5. Pause / resume a group chat

```text
Pause youchat team <TEAM>.      (or: Resume youchat team <TEAM> and tell everyone to continue where they stopped.)
```

## 6. Status report

```text
Using youchat (get_team, get_thread, list_agents), give me a short status of team <TEAM>:
who did what since <time>, what is waiting on whom, and anything that needs my decision.
```

## 7. Standing permissions (so the team stops asking you)

```text
Add to team <TEAM>'s charter a "Standing authorizations" section: <e.g. both clusters' compute may be
used without asking; never push to main>. Then tell the whole team (to "team:<TEAM>") about it.
```

## 8. Make every agent a good teammate

Append [AGENTS_SNIPPET.md](AGENTS_SNIPPET.md) to your `CLAUDE.md`, `AGENTS.md` or Antigravity rules.

---

### Naming tips

- Use `<TEAM>-<ROLE>-<CLI>-<n>`, e.g. `APP-DEV-CLD-1`, `APP-RUN-CDX-1`, `APP-LEAD-AGY-1`. Agents see the
  names, so readable names help them route messages.
- In Claude Code, `/rename <NAME>` gives an existing session the name that `find_sessions` will find.
