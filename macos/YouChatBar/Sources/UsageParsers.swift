// Pure parsers for each vendor's usage output. No I/O here, so they are unit-tested
// in macos/tests/main.swift against real captured output.
import Foundation

enum UsageParsers {
    // MARK: helpers
    static func firstMatch(_ pattern: String, in s: String) -> [String?]? {
        guard let re = try? NSRegularExpression(pattern: pattern, options: [.caseInsensitive]),
              let m = re.firstMatch(in: s, range: NSRange(s.startIndex..., in: s)) else { return nil }
        return (0..<m.numberOfRanges).map { i in
            Range(m.range(at: i), in: s).map { String(s[$0]) }
        }
    }

    static let months = ["jan", "feb", "mar", "apr", "may", "jun",
                         "jul", "aug", "sep", "oct", "nov", "dec"]

    /// "Oct 29 at 8:59am (America/Los_Angeles)", "Oct 30 at 5pm (...)", "9am (...)".
    /// The year is not printed, so pick the nearest one that is not in the past.
    static func parseClaudeReset(_ s: String, now: Date) -> Date? {
        let p = #"(?:([a-z]{3,9})\.?\s+(\d{1,2})\s+at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:\s*\(([^)]+)\))?"#
        guard let g = firstMatch(p, in: s), let hs = g[3], var hour = Int(hs) else { return nil }
        let minute = g[4].flatMap { Int($0) } ?? 0
        let pm = g[5]?.lowercased() == "pm"
        if hour == 12 { hour = 0 }
        if pm { hour += 12 }
        var cal = Calendar(identifier: .gregorian)
        cal.timeZone = g[6].flatMap { TimeZone(identifier: $0) } ?? .current
        var c = cal.dateComponents([.year, .month, .day], from: now)
        c.hour = hour; c.minute = minute; c.second = 0
        if let mon = g[1]?.lowercased().prefix(3), let idx = months.firstIndex(of: String(mon)),
           let day = g[2].flatMap({ Int($0) }) {
            c.month = idx + 1; c.day = day
            guard var d = cal.date(from: c) else { return nil }
            if d < now.addingTimeInterval(-2 * 86400) {
                d = cal.date(byAdding: .year, value: 1, to: d) ?? d
            }
            return d
        }
        guard var d = cal.date(from: c) else { return nil }
        if d < now { d = cal.date(byAdding: .day, value: 1, to: d) ?? d }
        return d
    }

    // MARK: Claude — `claude -p "/usage"`
    /// Lines like "Current week (all models): 66% used · resets Oct 30 at 4:59pm (America/Los_Angeles)".
    static func parseClaude(_ text: String, now: Date = Date()) -> [UsageWindow] {
        let p = #"^\s*Current (session|week)(?:\s*\(([^)]*)\))?\s*:\s*(\d+(?:\.\d+)?)%\s*used(?:.*?resets\s+(.+))?$"#
        var out: [UsageWindow] = []
        for raw in text.components(separatedBy: .newlines) {
            guard let g = firstMatch(p, in: raw), let kind = g[1], let pct = g[3].flatMap(Double.init)
            else { continue }
            var label = kind.lowercased() == "session" ? "5h" : "Week"
            if let scope = g[2], label == "Week", scope.lowercased() != "all models" {
                label = "Week (\(scope))"
            }
            out.append(UsageWindow(label: label, usedPercent: pct,
                                   resetsAt: g[4].flatMap { parseClaudeReset($0, now: now) }))
        }
        return out
    }

    // MARK: Antigravity — `agy -p "/usage"`
    /// TSV: "Gemini Models\tWeekly Limit Remaining\t68%\t2026-10-31T01:13:49Z".
    static func parseAgy(_ text: String) -> [UsageWindow] {
        let iso = ISO8601DateFormatter()
        let isoFrac = ISO8601DateFormatter()
        isoFrac.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var out: [UsageWindow] = []
        for raw in text.components(separatedBy: .newlines) {
            let f = raw.components(separatedBy: "\t").map { $0.trimmingCharacters(in: .whitespaces) }
            guard f.count >= 3, let n = Double(f[2].replacingOccurrences(of: "%", with: "")) else { continue }
            let what = f[1].lowercased()
            let window = what.contains("week") ? "Week" : (what.contains("five") || what.contains("5") ? "5h" : f[1])
            let group = f[0].replacingOccurrences(of: " Models", with: "").replacingOccurrences(of: " models", with: "")
            let used = what.contains("remaining") ? 100 - n : n
            let reset = f.count >= 4 ? (iso.date(from: f[3]) ?? isoFrac.date(from: f[3])) : nil
            out.append(UsageWindow(label: "\(group) · \(window)", usedPercent: used, resetsAt: reset))
        }
        return out
    }

    // MARK: Codex — `rate_limits` in ~/.codex/sessions/**/rollout-*.jsonl
    static func windowLabel(minutes: Double) -> String {
        if minutes >= 10000 && minutes <= 10200 { return "Week" }
        if minutes == 300 { return "5h" }
        return minutes >= 1440 ? "\(Int(minutes / 1440))d" : "\(Int(minutes / 60))h"
    }

    /// Parse one rollout line that contains "rate_limits". Returns nil if it has none.
    static func parseCodexLine(_ line: String) -> (windows: [UsageWindow], asOf: Date?)? {
        guard let data = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let payload = obj["payload"] as? [String: Any],
              let rl = payload["rate_limits"] as? [String: Any] else { return nil }
        var out: [UsageWindow] = []
        for key in ["primary", "secondary"] {
            guard let w = rl[key] as? [String: Any],
                  let used = (w["used_percent"] as? NSNumber)?.doubleValue else { continue }
            let mins = (w["window_minutes"] as? NSNumber)?.doubleValue ?? 0
            let reset = (w["resets_at"] as? NSNumber).map { Date(timeIntervalSince1970: $0.doubleValue) }
            out.append(UsageWindow(label: windowLabel(minutes: mins), usedPercent: used, resetsAt: reset))
        }
        let isoFrac = ISO8601DateFormatter()
        isoFrac.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let asOf = (obj["timestamp"] as? String).flatMap { isoFrac.date(from: $0) ?? ISO8601DateFormatter().date(from: $0) }
        return out.isEmpty ? nil : (out, asOf)
    }
}
