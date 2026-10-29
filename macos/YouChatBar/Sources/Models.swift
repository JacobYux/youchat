// Data models shared by the hub client, usage providers and views.
import Foundation

struct Member: Codable, Identifiable, Hashable {
    let team: String
    let agent: String
    let role: String
    let host: String
    let cli: String
    let session_id: String
    let cwd: String
    let online: Bool
    var id: String { team + "/" + agent }
}

struct Team: Codable, Identifiable, Hashable {
    let name: String
    let charter: String
    let status: String
    let created_by: String
    let created_at: Double
    let members: [Member]
    var id: String { name }
}

struct Message: Codable, Identifiable, Hashable {
    let seq: Int
    let thread_id: String
    let sender: String
    let recipient: String
    let body: String
    let created_at: Double
    let kind: String?
    var id: Int { seq }
}

/// One rate-limit window (e.g. "5h" or "Week") of one provider.
struct UsageWindow: Codable, Hashable, Identifiable {
    var label: String          // "5h", "Week", "Week (Fable)", "Gemini · Week" ...
    var usedPercent: Double    // 0...100
    var resetsAt: Date?
    var id: String { label }

    /// Observed numbers can be old (Codex only updates on use): past the reset time it is 0%.
    func effectiveUsed(now: Date = Date()) -> Double {
        if let r = resetsAt, r <= now { return 0 }
        return usedPercent
    }
}

enum Provider: String, Codable, CaseIterable, Identifiable {
    case claude, codex, agy
    var id: String { rawValue }
    var title: String {
        switch self {
        case .claude: return "Claude"
        case .codex: return "Codex"
        case .agy: return "Antigravity"
        }
    }
    var short: String {
        switch self {
        case .claude: return "C"
        case .codex: return "X"
        case .agy: return "A"
        }
    }
}

struct ProviderUsage: Codable, Hashable, Identifiable {
    var provider: Provider
    var windows: [UsageWindow] = []
    var asOf: Date? = nil      // when the numbers were observed
    var error: String? = nil
    var id: String { provider.rawValue }

    /// The weekly window that matters most (highest weekly usage).
    var weekly: UsageWindow? {
        windows.filter { $0.label.contains("Week") }.max { $0.effectiveUsed() < $1.effectiveUsed() }
    }
}
