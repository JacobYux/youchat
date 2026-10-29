// App state: polls the hub (cheap, local socket) and the usage providers (slower CLIs).
import Foundation
import SwiftUI

@MainActor
final class AppModel: ObservableObject {
    @Published var teams: [Team] = []
    @Published var recent: [String: [Message]] = [:]   // team -> newest messages (newest first)
    @Published var hubError: String? = nil
    @Published var groupsUpdated: Date? = nil
    @Published var usage: [Provider: ProviderUsage] = [:]
    @Published var refreshing: Set<Provider> = []

    private var timers: [Timer] = []
    static let usageInterval: TimeInterval = 15 * 60

    init() {
        for u in UsageProviders.loadCache() { usage[u.provider] = u }
        refreshGroups()
        refreshUsage()
        timers.append(Timer.scheduledTimer(withTimeInterval: 5 * 60, repeats: true) { [weak self] _ in
            Task { @MainActor in
                self?.refreshGroups()
                self?.refreshUsage([.codex])     // local file read, cheap
            }
        })
        timers.append(Timer.scheduledTimer(withTimeInterval: Self.usageInterval, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refreshUsage([.claude, .agy]) }
        })
    }

    /// Menu bar title, e.g. "C 66 · X 82 · A 32" (weekly usage %).
    var menuTitle: String {
        Provider.allCases.map { p in
            let w = usage[p]?.weekly.map { String(Int($0.effectiveUsed().rounded())) } ?? "–"
            return "\(p.short) \(w)"
        }.joined(separator: " · ")
    }

    var worstWeekly: Double {
        usage.values.compactMap { $0.weekly?.effectiveUsed() }.max() ?? 0
    }

    // MARK: groups
    func refreshGroups() {
        Task.detached {
            let hub = HubClient()
            do {
                let teams = try hub.teams()
                let msgs = (try? hub.recentMessages(limit: 500)) ?? []
                let recent = Self.attribute(msgs, to: teams)
                await MainActor.run {
                    self.teams = teams
                    self.recent = recent
                    self.hubError = nil
                    self.groupsUpdated = Date()
                }
            } catch {
                await MainActor.run { self.hubError = "\(error)" }
            }
        }
    }

    /// Same rule as `youchat tail <team>`: a message belongs to a team if the sender or a
    /// recipient is a member, or it was addressed to "team:<name>".
    nonisolated static func attribute(_ msgs: [Message], to teams: [Team]) -> [String: [Message]] {
        var out: [String: [Message]] = [:]
        for t in teams {
            let names = Set(t.members.map { $0.agent }).union(["team:" + t.name])
            out[t.name] = msgs.filter { m in
                names.contains(m.sender) ||
                    !names.isDisjoint(with: m.recipient.split(separator: ",").map(String.init))
            }.sorted { $0.seq > $1.seq }
        }
        return out
    }

    func lastActive(_ team: Team) -> Date? {
        recent[team.name]?.first.map { Date(timeIntervalSince1970: $0.created_at) }
    }

    // MARK: usage
    func refreshUsage(_ which: [Provider] = Provider.allCases) {
        for p in which where !refreshing.contains(p) {
            refreshing.insert(p)
            Task.detached {
                let fresh = UsageProviders.fetch(p)
                await MainActor.run {
                    var u = fresh
                    if u.windows.isEmpty, let old = self.usage[p] {   // keep last good numbers
                        u.windows = old.windows
                        u.asOf = old.asOf
                    }
                    self.usage[p] = u
                    self.refreshing.remove(p)
                    UsageProviders.saveCache(Array(self.usage.values))
                }
            }
        }
    }

    /// Weekly pace: used% divided by the share of the week already elapsed.
    /// > 1 means this vendor will run out before its reset at the current rate.
    nonisolated static func pace(_ w: UsageWindow, now: Date = Date()) -> Double? {
        guard let r = w.resetsAt, r > now else { return nil }
        let elapsed = 1 - r.timeIntervalSince(now) / (7 * 86400)
        guard elapsed > 0.02 else { return nil }
        return w.usedPercent / 100 / elapsed
    }

    /// Which vendor to lean on next: the one with the lowest weekly pace.
    var advice: String? {
        let rows = usage.values.compactMap { u -> (Provider, Double)? in
            guard let w = u.weekly else { return nil }
            return (u.provider, Self.pace(w) ?? 0)
        }
        guard rows.count >= 2, let lo = rows.min(by: { $0.1 < $1.1 }),
              let hi = rows.max(by: { $0.1 < $1.1 }), hi.1 - lo.1 > 0.15 else { return nil }
        return "\(hi.0.title) 用得最快，接下来的任务建议优先交给 \(lo.0.title)"
    }

    // MARK: actions
    /// Reuse the existing CLI: `youchat open <member> --team <team>` opens Ghostty/Terminal.
    @Published var actionMessage: String? = nil

    func open(_ m: Member) {
        guard let exe = UsageProviders.which("youchat") else {
            actionMessage = "youchat not found in PATH"; return
        }
        Task.detached {
            let out = (try? UsageProviders.run(exe, ["open", m.agent, "--team", m.team], timeout: 20)) ?? ""
            let msg = out.trimmingCharacters(in: .whitespacesAndNewlines)
            await MainActor.run { self.actionMessage = msg.isEmpty ? "opened \(m.agent)" : msg }
        }
    }
}
