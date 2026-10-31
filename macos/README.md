# YouChat Bar (macOS only)

A menu bar companion for youchat (no Dock icon): the button shows weekly usage, e.g.
`C 82 · X 33 · A 34`, and opens a panel. It lives in `macos/` and is **not** part of the Python
package: nothing in `src/youchat` imports it, and Linux/server installs never see it.

- **Groups**: every team's purpose (charter), members, roles, online state, recent messages, and one-click buttons to open a whole team workspace in iTerm2 + `tmux` (`youchat open <TEAM>`) or an individual member session.
- **Usage**: Claude / Codex / Antigravity 5-hour and weekly usage, with reset times.

## Build / install

```sh
macos/build.sh             # -> macos/build/YouChatBar.app
macos/build.sh --install   # copy to ~/Applications and start at login
macos/tests/test.sh        # parser tests
```

Needs macOS 14+ and `swiftc` (Xcode Command Line Tools). No Xcode project.

> **Start it with `--install` (launchd), not from an agent's terminal.** macOS 26 files a menu bar item
> under the app that launched it (e.g. Antigravity or Claude). If that app's menu bar icon gets hidden, or
> was dragged out of the menu bar, YouChat Bar is hidden with it. Hidden apps can be turned back on in
> System Settings → Menu Bar → "Allow in the Menu Bar".

## Where the data comes from

| | Source | Refresh |
|---|---|---|
| Groups | hub socket (`$YOUCHAT_SOCK` or `/tmp/youchat-<uid>/hub.sock`), read-only `teams` + `log` | 5 min, and each time the panel opens |
| Claude | `claude -p "/usage" --no-session-persistence` | 15 min |
| Antigravity | `agy -p "/usage"` | 15 min |
| Codex | latest `rate_limits` event in `~/.codex/sessions/**.jsonl` | 5 min (only changes when you use Codex) |

No tokens are read and no private HTTP APIs are called. The last result is cached in
`~/.youchat/mac/usage.json`.

Uninstall: `launchctl bootout gui/$(id -u)/com.youchat.bar; rm -rf ~/Applications/YouChatBar.app ~/Library/LaunchAgents/com.youchat.bar.plist`
