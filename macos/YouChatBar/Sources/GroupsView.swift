// Groups tab: what each team is for (charter), who is in it and who does what (roles).
import SwiftUI

struct GroupsView: View {
    @EnvironmentObject var model: AppModel

    var sorted: [Team] {
        model.teams.sorted { a, b in
            if (a.status == "active") != (b.status == "active") { return a.status == "active" }
            return (model.lastActive(a) ?? .distantPast) > (model.lastActive(b) ?? .distantPast)
        }
    }

    var body: some View {
        LazyVStack(alignment: .leading, spacing: 10) {
            if let err = model.hubError {
                Card(accent: Color.red.opacity(0.35)) {
                    VStack(alignment: .leading, spacing: 4) {
                        Label("连不上 hub", systemImage: "bolt.horizontal.circle.fill")
                            .font(.callout).foregroundStyle(.red)
                        Text("启动 hub：`youchat hub`，或连隧道：`youchat tunnel <host>`")
                            .font(.caption).foregroundStyle(.secondary)
                        Text(err).font(.caption2).foregroundStyle(.tertiary)
                            .lineLimit(3).textSelection(.enabled)
                    }
                }
            }
            if model.teams.isEmpty && model.hubError == nil {
                Text("还没有小组").foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, alignment: .center).padding(.vertical, 24)
            }
            ForEach(sorted) { TeamCard(team: $0) }
        }
        .padding(10)
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

    var body: some View {
        let active = team.status == "active"
        Card(accent: active ? Color.accentColor.opacity(0.3) : nil) {
            VStack(alignment: .leading, spacing: 9) {
                header(active)
                purpose
                members
                RecentMessages(messages: Array((model.recent[team.name] ?? []).prefix(3)))
            }
        }
    }

    private func header(_ active: Bool) -> some View {
        let online = team.members.filter { $0.online }.count
        let hasAgents = team.members.contains { $0.cli != "human" }
        return HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(team.name).font(.headline)
            Pill(text: team.status, color: active ? .green : Color.secondary)
            Spacer()
            Text("\(online)/\(team.members.count) 在线")
                .font(.caption).monospacedDigit()
                .foregroundStyle(online > 0 ? Color.secondary : Color.secondary.opacity(0.6))
            if let t = model.lastActive(team) {
                Text("· \(Fmt.relative(t))").font(.caption).foregroundStyle(.tertiary)
            }
            if hasAgents {
                Button { model.openGroup(team) } label: { Image(systemName: "macwindow.badge.plus") }
                    .buttonStyle(.borderless).help("在 iTerm2 + tmux 中打开整个小组（youchat open \(team.name)）")
            }
        }
    }

    private var purpose: some View {
        // One source line, two visual lines: a whole thought instead of a wall cut mid-path.
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
            ForEach(team.members) { MemberRow(member: $0) }
        }
    }
}

struct MemberRow: View {
    @EnvironmentObject var model: AppModel
    let member: Member

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
        HStack(alignment: .top, spacing: 6) {
            Circle().fill(member.online ? Color.green : Color.primary.opacity(0.18))
                .frame(width: 6, height: 6).padding(.top, 5)
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 5) {
                    Text(member.agent).font(.system(.caption, design: .monospaced)).bold()
                    Pill(text: member.cli.isEmpty ? "?" : member.cli, color: cliColor)
                    if !member.host.isEmpty {
                        Text(member.host).font(.caption2).foregroundStyle(.tertiary).lineLimit(1)
                    }
                    Spacer()
                    if !member.online && member.cli != "human" {
                        Button { model.open(member) } label: { Image(systemName: "terminal") }
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
