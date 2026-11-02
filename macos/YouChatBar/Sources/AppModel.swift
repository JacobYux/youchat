// App state: polls the hub (cheap, local socket) and the usage providers (slower CLIs).
import Foundation
import SwiftUI

@MainActor
final class AppModel: ObservableObject {
    @Published var teams: [Team] = []
    @Published var recent: [String: [Message]] = [:]   // team.id -> newest messages (newest first)
    @Published var hubError: String? = nil
    @Published var remoteErrors: [String: String] = [:]
    @Published var groupsUpdated: Date? = nil
    @Published var hiddenGroups: Set<String> = []
    @Published var showHidden: Bool = false
    @Published var remoteHosts: [String] = []
    @Published var usage: [Provider: ProviderUsage] = [:]
    @Published var refreshing: Set<Provider> = []

    private var localTeams: [Team] = []
    private var localRecent: [String: [Message]] = [:]
    private var remoteCache: [String: RemoteSnapshot] = [:]
    private var pendingDeletes: Set<String> = []
    private var timers: [Timer] = []
    static let usageInterval: TimeInterval = 15 * 60

    init() {
        let cfg = Self.loadGroupsConfig()
        hiddenGroups = cfg.hidden
        remoteHosts = cfg.remoteHosts
        remoteCache = Self.loadRemoteCache()
        for u in UsageProviders.loadCache() { usage[u.provider] = u }
        rebuildMergedGroups()
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
    var visibleTeams: [Team] { teams.filter { !isHidden($0) } }
    var hiddenTeams: [Team] { teams.filter { isHidden($0) } }

    func isHidden(_ team: Team) -> Bool {
        hiddenGroups.contains(team.id) || hiddenGroups.contains(team.name)
    }

    private func rebuildMergedGroups() {
        var allTeams = localTeams.filter { !pendingDeletes.contains($0.id) }
        var allRecent = localRecent
        for host in remoteHosts {
            guard let snap = remoteCache[host] else { continue }
            let tagged = snap.teams.map { t -> Team in var c = t; c.sshHost = host; return c }
                .filter { !pendingDeletes.contains($0.id) }
            allTeams.append(contentsOf: tagged)
            for (k, v) in Self.attribute(snap.messages, to: tagged) { allRecent[k] = v }
        }
        teams = allTeams
        recent = allRecent
    }

    func refreshGroups() {
        let hosts = remoteHosts
        Task.detached {
            let hub = HubClient()
            do {
                let lTeams = try hub.teams()
                let lMsgs = (try? hub.recentMessages(limit: 500)) ?? []
                let lRecent = Self.attribute(lMsgs, to: lTeams)
                await MainActor.run {
                    self.localTeams = lTeams.filter { !self.pendingDeletes.contains($0.id) }
                    self.localRecent = lRecent
                    self.hubError = nil
                    self.groupsUpdated = Date()
                    self.rebuildMergedGroups()
                }
            } catch {
                await MainActor.run { self.hubError = "\(error)" }
            }
            for host in hosts {
                do {
                    let snap = try HubClient.fetchRemote(host: host)
                    await MainActor.run {
                        var filtered = snap
                        filtered.teams.removeAll { self.pendingDeletes.contains($0.id) }
                        self.remoteCache[host] = filtered
                        self.remoteErrors.removeValue(forKey: host)
                        self.groupsUpdated = Date()
                        self.rebuildMergedGroups()
                        Self.saveRemoteCache(self.remoteCache)
                    }
                } catch {
                    await MainActor.run { self.remoteErrors[host] = "\(error)" }
                }
            }
        }
    }

    nonisolated static func attribute(_ msgs: [Message], to teams: [Team]) -> [String: [Message]] {
        var out: [String: [Message]] = [:]
        for t in teams {
            let names = Set(t.members.map { $0.agent }).union(["team:" + t.name])
            out[t.id] = msgs.filter { m in
                names.contains(m.sender) ||
                    !names.isDisjoint(with: m.recipient.split(separator: ",").map(String.init))
            }.sorted { $0.seq > $1.seq }
        }
        return out
    }

    func lastActive(_ team: Team) -> Date? {
        (recent[team.id] ?? recent[team.name])?.first.map { Date(timeIntervalSince1970: $0.created_at) }
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

    func open(_ m: Member, sshHost: String? = nil) {
        guard let exe = UsageProviders.which("youchat") else {
            actionMessage = "youchat not found in PATH"; return
        }
        var args = ["open", m.agent, "--team", m.team]
        if let h = sshHost, !h.isEmpty { args += ["--ssh", h] }
        Task.detached {
            let out = (try? UsageProviders.run(exe, args, timeout: 20)) ?? ""
            let msg = out.trimmingCharacters(in: .whitespacesAndNewlines)
            await MainActor.run { self.actionMessage = msg.isEmpty ? "opened \(m.agent)" : msg }
        }
    }

    func openGroup(_ team: Team) {
        guard let exe = UsageProviders.which("youchat") else {
            actionMessage = "youchat not found in PATH"; return
        }
        var args = ["open", "--group", team.name]
        if let h = team.sshHost, !h.isEmpty { args += ["--ssh", h] }
        Task.detached {
            let out = (try? UsageProviders.run(exe, args, timeout: 30)) ?? ""
            let msg = out.trimmingCharacters(in: .whitespacesAndNewlines)
            await MainActor.run { self.actionMessage = msg.isEmpty ? "opened group \(team.name)" : msg }
        }
    }

    func toggleHide(_ team: Team) {
        if isHidden(team) {
            hiddenGroups.remove(team.id)
            hiddenGroups.remove(team.name)
            actionMessage = "已恢复显示 \(team.name)"
        } else {
            hiddenGroups.insert(team.id)
            actionMessage = "已隐藏 \(team.name)"
        }
        if hiddenTeams.isEmpty { showHidden = false }
        Self.saveGroupsConfig(GroupsConfig(hidden: hiddenGroups, remoteHosts: remoteHosts))
    }

    func deleteGroup(_ team: Team) {
        let tid = team.id
        let name = team.name
        let host = team.sshHost
        pendingDeletes.insert(tid)
        localTeams.removeAll { $0.id == tid }
        if let h = host { remoteCache[h]?.teams.removeAll { $0.name == name } }
        hiddenGroups.remove(tid)
        hiddenGroups.remove(name)
        rebuildMergedGroups()
        Self.saveGroupsConfig(GroupsConfig(hidden: hiddenGroups, remoteHosts: remoteHosts))
        Self.saveRemoteCache(remoteCache)
        Task.detached {
            do {
                if let h = host, !h.isEmpty {
                    try HubClient.deleteRemoteTeam(host: h, name: name)
                } else {
                    try HubClient().deleteTeam(name)
                }
                await MainActor.run {
                    self.pendingDeletes.remove(tid)
                    self.localTeams.removeAll { $0.id == tid }
                    if let h = host { self.remoteCache[h]?.teams.removeAll { $0.name == name } }
                    self.rebuildMergedGroups()
                    Self.saveRemoteCache(self.remoteCache)
                    self.actionMessage = "已删除小组 \(name)"
                }
            } catch {
                await MainActor.run {
                    self.pendingDeletes.remove(tid)
                    self.actionMessage = "删除失败: \(error)"
                    self.refreshGroups()
                }
            }
        }
    }

    // MARK: groups persistence (~/.youchat/mac/groups.json & remote-groups.json)
    nonisolated static var groupsConfigURL: URL { URL(fileURLWithPath: "\(UsageProviders.workDir)/groups.json") }
    nonisolated static var remoteCacheURL: URL { URL(fileURLWithPath: "\(UsageProviders.workDir)/remote-groups.json") }

    nonisolated static func loadGroupsConfig() -> GroupsConfig {
        let dflHosts = HubClient.defaultRemoteHosts()
        if let d = try? Data(contentsOf: groupsConfigURL),
           var cfg = try? JSONDecoder().decode(GroupsConfig.self, from: d) {
            if cfg.remoteHosts.isEmpty && !dflHosts.isEmpty { cfg.remoteHosts = dflHosts }
            return cfg
        }
        let initial = GroupsConfig(hidden: ["Youchat-Toolbar"], remoteHosts: dflHosts)
        saveGroupsConfig(initial)
        return initial
    }

    nonisolated static func saveGroupsConfig(_ cfg: GroupsConfig) {
        let enc = JSONEncoder()
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let d = try? enc.encode(cfg) { try? d.write(to: groupsConfigURL, options: .atomic) }
    }

    nonisolated static func loadRemoteCache() -> [String: RemoteSnapshot] {
        guard let d = try? Data(contentsOf: remoteCacheURL) else { return [:] }
        return (try? JSONDecoder().decode([String: RemoteSnapshot].self, from: d)) ?? [:]
    }

    nonisolated static func saveRemoteCache(_ cache: [String: RemoteSnapshot]) {
        if let d = try? JSONEncoder().encode(cache) { try? d.write(to: remoteCacheURL, options: .atomic) }
    }
}
