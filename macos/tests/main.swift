// Parser tests, run with macos/tests/test.sh (no Xcode needed).
import Foundation

var failures = 0
func check(_ cond: Bool, _ msg: String, line: Int = #line) {
    if cond { print("ok   \(msg)") } else { failures += 1; print("FAIL \(msg) (line \(line))") }
}
func date(_ iso: String) -> Date { ISO8601DateFormatter().date(from: iso)! }

// Real output captured from `claude -p "/usage"` (Claude Code 2.1.312).
let claudeOut = """
You are currently using your subscription to power your Claude Code usage

Current session: 18% used · resets Oct 29 at 8:59am (America/Los_Angeles)
Current week (all models): 66% used · resets Oct 30 at 4:59pm (America/Los_Angeles)
Current week (Fable): 0% used · resets Oct 30 at 5pm (America/Los_Angeles)

What's contributing to your usage?
  92%  Give "hafs-kb" subagents a cheaper model
"""
let now = date("2026-10-29T12:59:00Z")   // 05:59 PDT
let c = UsageParsers.parseClaude(claudeOut, now: now)
check(c.count == 3, "claude: 3 windows (got \(c.count))")
check(c.first?.label == "5h" && c.first?.usedPercent == 18, "claude: session 18%")
check(c.first?.resetsAt == date("2026-10-29T15:59:00Z"), "claude: session resets 8:59am PDT")
check(c[1].label == "Week" && c[1].usedPercent == 66, "claude: week 66%")
check(c[1].resetsAt == date("2026-10-30T23:59:00Z"), "claude: week resets Oct 30 4:59pm PDT")
check(c[2].label == "Week (Fable)" && c[2].resetsAt == date("2026-10-31T00:00:00Z"), "claude: Fable 5pm")

// Year rollover and time-only forms.
let dec = date("2026-12-31T20:00:00Z")
check(UsageParsers.parseClaudeReset("Jan 2 at 9am (UTC)", now: dec) == date("2027-01-02T09:00:00Z"), "claude: year rollover")
check(UsageParsers.parseClaudeReset("9pm (UTC)", now: dec) == date("2026-12-31T21:00:00Z"), "claude: time-only today")
check(UsageParsers.parseClaudeReset("7am (UTC)", now: dec) == date("2027-01-01T07:00:00Z"), "claude: time-only tomorrow")
check(UsageParsers.parseClaudeReset("Oct 29 at 12am (UTC)", now: now) == date("2026-10-29T00:00:00Z"), "claude: 12am")

// Real output captured from `agy -p "/usage"` (agy 1.4.7).
let agyOut = "Gemini Models\tWeekly Limit Remaining\t68%\t2026-10-31T01:13:49Z\n" +
    "Gemini Models\tFive Hour Limit Remaining\t94%\t2026-10-29T16:48:55Z\n" +
    "Claude and GPT models\tWeekly Limit Remaining\t98%\t2026-11-05T12:58:51Z\n" +
    "Claude and GPT models\tFive Hour Limit Remaining\t98%\t2026-10-29T17:58:51Z\n"
let a = UsageParsers.parseAgy(agyOut)
check(a.count == 4, "agy: 4 windows")
check(a[0].label == "Gemini · Week" && a[0].usedPercent == 32, "agy: Gemini week used 32%")
check(a[0].resetsAt == date("2026-10-31T01:13:49Z"), "agy: Gemini week reset")
check(a[1].label == "Gemini · 5h" && a[1].usedPercent == 6, "agy: Gemini 5h used 6%")
check(a[2].label == "Claude and GPT · Week" && a[2].usedPercent == 2, "agy: Claude+GPT week")
check(ProviderUsage(provider: .agy, windows: a).weekly?.usedPercent == 32, "agy: weekly = worst group")

// Real line shape from ~/.codex/sessions (codex-cli 0.163.0), trimmed.
let codexLine = #"{"timestamp":"2026-10-29T13:04:28.038Z","ordinal":1,"type":"event_msg","payload":{"type":"token_count","info":null,"rate_limits":{"limit_id":"codex","primary":{"used_percent":82.0,"window_minutes":10080,"resets_at":1793600105},"secondary":null,"plan_type":"pro"}}}"#
let x = UsageParsers.parseCodexLine(codexLine)
check(x?.windows.count == 1, "codex: 1 window")
check(x?.windows.first?.label == "Week" && x?.windows.first?.usedPercent == 82, "codex: week 82%")
check(x?.windows.first?.resetsAt == Date(timeIntervalSince1970: 1793600105), "codex: resets_at")
check(x?.asOf != nil, "codex: as-of timestamp")
check(UsageParsers.parseCodexLine(#"{"type":"event_msg","payload":{"type":"agent_message"}}"#) == nil, "codex: no rate_limits -> nil")

// A window whose reset time has passed counts as 0%.
let stale = UsageWindow(label: "Week", usedPercent: 82, resetsAt: date("2026-10-01T00:00:00Z"))
check(stale.effectiveUsed(now: now) == 0, "stale window reads as reset")

print(failures == 0 ? "\nALL PASSED" : "\n\(failures) FAILED")
exit(failures == 0 ? 0 : 1)
