// Groups tab: what each team is for (charter), who is in it and who does what (roles).
import SwiftUI

struct GroupsView: View {
    @EnvironmentObject var model: AppModel

    private func sort(_ list: [Team]) -> [Team] {
        list.sorted { a, b in
            if (a.status == "active") != (b.status == "active") { return a.status == "active" }
            let ta = model.lastActive(a) ?? Date(timeIntervalSince1970: a.created_at)
            let tb = model.lastActive(b) ?? Date(timeIntervalSince1970: b.created_at)
            return ta > tb
        }
    }

    var body: some View {
        let visible = sort(model.visibleTeams)
        let hidden = sort(model.hiddenTeams)
        LazyVStack(alignment: .leading, spacing: 10) {
            if let err = model.hubError {
                Card(accent: Color.red.opacity(0.35)) {
                    VStack(alignment: .leading, spacing: 4) {
                        Label("连不上本地 hub", systemImage: "bolt.horizontal.circle.fill")
                            .font(.callout).foregroundStyle(.red)
                        Text("启动 hub：`youchat hub`，或连隧道：`youchat tunnel <host>`")
                            .font(.caption).foregroundStyle(.secondary)
                        Text(err).font(.caption2).foregroundStyle(.tertiary)
                            .lineLimit(3).textSelection(.enabled)
                    }
                }
            }
            ForEach(model.remoteErrors.keys.sorted(), id: \.self) { host in
                if let rerr = model.remoteErrors[host] {
                    Card(accent: Color.orange.opacity(0.35)) {
                        VStack(alignment: .leading, spacing: 3) {
                            Label("远程主机 \(host) 暂未连接", systemImage: "network.slash")
                                .font(.caption).foregroundStyle(.orange)
                            Text(rerr).font(.caption2).foregroundStyle(.tertiary).lineLimit(2)
                        }
                    }
                }
            }
            if visible.isEmpty && model.hubError == nil {
                Text(hidden.isEmpty ? "还没有小组" : "所有小组均已隐藏").foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center).padding(.vertical, 24)
            }
            ForEach(visible) { TeamCard(team: $0) }
            if !hidden.isEmpty {
                hiddenSection(hidden)
            }
        }
        .padding(10)
    }

    private func hiddenSection(_ hidden: [Team]) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Button {
                model.showHidden.toggle()
            } label: {
                HStack(spacing: 6) {
                    Image(systemName: model.showHidden ? "chevron.down" : "chevron.right")
                        .font(.caption2).foregroundStyle(.tertiary)
                    Text("已隐藏 \(hidden.count) 个小组（\(hidden.map(\.name).joined(separator: "、"))）")
                        .font(.caption).foregroundStyle(.secondary).lineLimit(1)
                    Spacer()
                    Text(model.showHidden ? "收起" : "显示")
                        .font(.caption2).foregroundStyle(.tertiary)
                }
                .padding(.horizontal, 6).padding(.vertical, 4)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            if model.showHidden {
                ForEach(hidden) { TeamCard(team: $0).opacity(0.75) }
            }
        }
    }
}

/// Title = first "# " heading; summary = first few prose lines (headings and code fences skipped).
func charterSummary(_ charter: String, maxLines: Int = 3) -> (title: String?, lines: [String]) {
    var title: String? = nil
    var lines: [String] = []
    var inCode = false
    for raw in charter.components(separatedBy: .newlines) {
        let s = raw.trimmingCharacters(in: .whitespaces)
        if s.hasPrefix("```") { inCode.toggle(); continue }
        if inCode || s.isEmpty { continue }
        if s.hasPrefix("# ") && title == nil { title = String(s.dropFirst(2)); continue }
        if s.hasPrefix("#") { continue }
        var line = s.replacingOccurrences(of: "**", with: "").replacingOccurrences(of: "`", with: "")
        // Charters often open with a list; the bullet or number on its own tells the reader nothing.
        if let marker = line.range(of: "^\\s*([-*+·]|\\d+[.)])\\s+", options: .regularExpression) {
            line.removeSubrange(marker)
        }
        lines.append(line)
        if lines.count >= maxLines { break }
    }
    return (title, lines)
}

struct TeamCard: View {
    @EnvironmentObject var model: AppModel
    let team: Team
    @State private var showCharter = false
    @State private var confirmDelete = false

    var body: some View {
        let active = team.status == "active"
        let msgs = model.recent[team.id] ?? model.recent[team.name] ?? []
        Card(accent: active ? Color.accentColor.opacity(0.3) : nil) {
            VStack(alignment: .leading, spacing: 9) {
                header(active)
                purpose
                members
                RecentMessages(messages: Array(msgs.prefix(3)))
            }
        }
    }

    private func header(_ active: Bool) -> some View {
        let online = team.members.filter { $0.online }.count
        let hasAgents = team.members.contains { $0.cli != "human" }
        let hidden = model.isHidden(team)
        return HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(team.name).font(.headline).lineLimit(1).truncationMode(.tail)
            Pill(text: team.status, color: active ? .green : Color.secondary).fixedSize()
            if let h = team.sshHost, !h.isEmpty {
                Pill(text: h, color: .indigo).fixedSize()
            }
            Spacer(minLength: 4)
            if confirmDelete {
                Button("确认删除") { model.deleteGroup(team) }
                    .buttonStyle(.borderless).font(.caption).foregroundStyle(.red).fixedSize()
                Button("取消") { confirmDelete = false }
                    .buttonStyle(.borderless).font(.caption).foregroundStyle(.secondary).fixedSize()
            } else {
                Text("\(online)/\(team.members.count) 在线")
                    .font(.caption).monospacedDigit().lineLimit(1).fixedSize()
                    .foregroundStyle(online > 0 ? Color.secondary : Color.secondary.opacity(0.6))
                if let t = model.lastActive(team) {
                    Text("· \(Fmt.relative(t))").font(.caption).foregroundStyle(.tertiary).lineLimit(1).fixedSize()
                }
                if hasAgents {
                    Button { model.openGroup(team) } label: { Image(systemName: "macwindow.badge.plus") }
                        .buttonStyle(.borderless).help("在 iTerm2 + tmux 中打开整个小组（youchat open \(team.name)）")
                }
                Button { model.toggleHide(team) } label: {
                    Image(systemName: hidden ? "eye" : "eye.slash")
                }
                .buttonStyle(.borderless).help(hidden ? "取消隐藏此小组" : "隐藏此小组（可在底部恢复）")
                Button { confirmDelete = true } label: { Image(systemName: "trash") }
                    .buttonStyle(.borderless).foregroundStyle(.secondary).help("删除此小组")
            }
        }
    }

    private var purpose: some View {
        let sum = charterSummary(team.charter, maxLines: 1)
        return VStack(alignment: .leading, spacing: 3) {
            SectionLabel(text: "用途", systemImage: "target")
            if let title = sum.title {
                Text(title).font(.subheadline).fontWeight(.medium)
            }
            if showCharter {
                Text(team.charter).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
            } else if !sum.lines.isEmpty {
                Text(sum.lines.joined(separator: "\n"))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(2)
            }
            if !team.charter.isEmpty {
                Button(showCharter ? "收起章程" : "完整章程") { showCharter.toggle() }
                    .buttonStyle(.link).font(.caption)
            }
        }
    }

    private var members: some View {
        VStack(alignment: .leading, spacing: 5) {
            SectionLabel(text: "成员与分工", systemImage: "person.2.fill")
            ForEach(team.members) { MemberRow(member: $0, sshHost: team.sshHost) }
        }
    }
}

struct MemberRow: View {
    @EnvironmentObject var model: AppModel
    let member: Member
    var sshHost: String? = nil

    var cliColor: Color {
        switch member.cli {
        case "claude": return .orange
        case "codex": return .blue
        case "antigravity": return .purple
        case "human": return .pink
        default: return .gray
        }
    }

    var body: some View {
        let hostLabel = member.host.isEmpty ? (sshHost ?? "") : member.host
        HStack(alignment: .top, spacing: 6) {
            Circle().fill(member.online ? Color.green : Color.primary.opacity(0.18))
                .frame(width: 6, height: 6).padding(.top, 5)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 5) {
                    Text(member.agent).font(.system(.caption, design: .monospaced)).bold()
                    Pill(text: member.cli.isEmpty ? "?" : member.cli, color: cliColor)
                    if !hostLabel.isEmpty {
                        Text(hostLabel).font(.caption2).foregroundStyle(.tertiary).lineLimit(1)
                    }
                    Spacer()
                    if !member.online && member.cli != "human" {
                        Button { model.open(member, sshHost: sshHost) } label: { Image(systemName: "terminal") }
                            .buttonStyle(.borderless).help("在终端里打开（youchat open）")
                    }
                }
                if !member.role.isEmpty {
                    Text(member.role).font(.caption).foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
    }
}

struct RecentMessages: View {
    let messages: [Message]

    var body: some View {
        if !messages.isEmpty {
            VStack(alignment: .leading, spacing: 5) {
                SectionLabel(text: "最近消息", systemImage: "text.bubble.fill")
                ForEach(messages) { m in
                    VStack(alignment: .leading, spacing: 1) {
                        HStack(spacing: 4) {
                            Text("\(m.sender) → \(m.recipient)")
                                .font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                            if let k = m.kind, k != "chat" { Pill(text: k, color: Self.kindColor(k)) }
                            Spacer()
                            Text(Fmt.relative(Date(timeIntervalSince1970: m.created_at)))
                                .font(.caption2).foregroundStyle(.tertiary)
                        }
                        Text(firstLine(m.body)).font(.caption).lineLimit(2)
                    }
                }
            }
        }
    }

    /// The kinds youchat sends, so a card's traffic can be skimmed by colour.
    static func kindColor(_ kind: String) -> Color {
        switch kind {
        case "task": return .blue
        case "handoff": return .purple
        case "result": return .green
        case "question": return .orange
        case "review", "review_request": return .teal
        case "ask_human": return .pink
        default: return .gray
        }
    }

    /// Bodies usually open with a markdown heading; the markers add nothing at this size.
    func firstLine(_ body: String) -> String {
        let line = body.components(separatedBy: .newlines)
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .first { !$0.isEmpty } ?? ""
        return line.trimmingCharacters(in: CharacterSet(charactersIn: "#*` "))
    }
}
