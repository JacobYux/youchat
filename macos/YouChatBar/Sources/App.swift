// Entry point: menu bar app (no Dock icon); the menu bar button lives in StatusItem.swift.
import SwiftUI

@main
struct YouChatBarApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate

    var body: some Scene {
        // Menu bar only (StatusItem.swift). A scene is required; Settings stays empty and unused.
        Settings { EmptyView() }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = AppModel()
    private var status: StatusItemController?

    func applicationDidFinishLaunching(_ notification: Notification) {
        status = StatusItemController(model: model)
    }
}

enum Tab: String, CaseIterable { case groups = "Groups", usage = "Usage" }

struct ContentView: View {
    @EnvironmentObject var model: AppModel
    @State private var tab: Tab = .groups
    var showsQuit = true   // the popover has no other way out; the window has the app menu and ⌘Q

    var body: some View {
        VStack(spacing: 0) {
            Picker("", selection: $tab) {
                ForEach(Tab.allCases, id: \.self) { Text(title($0)).tag($0) }
            }
            .pickerStyle(.segmented).labelsHidden().monospacedDigit()
            .padding(.horizontal, 10).padding(.top, 10).padding(.bottom, 8)
            Divider()
            ScrollView {
                switch tab {
                case .groups: GroupsView()
                case .usage: UsageView()
                }
            }
            Divider()
            footer
        }
    }

    /// Each tab carries its own headline number, so the other tab never has to be opened to check.
    private func title(_ t: Tab) -> String {
        switch t {
        case .groups:
            return model.teams.isEmpty ? "Groups" : "Groups · \(model.teams.count)"
        case .usage:
            let worst = model.worstWeekly
            return worst > 0 ? "Usage · \(Int(worst.rounded()))%" : "Usage"
        }
    }

    private var updated: Date? {
        tab == .groups ? model.groupsUpdated : model.usage.values.compactMap { $0.asOf }.max()
    }

    private var footer: some View {
        HStack(spacing: 10) {
            if let msg = model.actionMessage {
                Text(msg).font(.caption).foregroundStyle(.secondary).lineLimit(1)
            } else if let t = updated {
                Text("更新于 \(t.formatted(date: .omitted, time: .shortened))")
                    .font(.caption).foregroundStyle(.tertiary).monospacedDigit()
            }
            Spacer()
            Button {
                model.actionMessage = nil
                if tab == .groups { model.refreshGroups() } else { model.refreshUsage() }
            } label: { Image(systemName: "arrow.clockwise") }
            .help("刷新（⌘R）").keyboardShortcut("r")
            if showsQuit {
                Button { NSApplication.shared.terminate(nil) } label: { Image(systemName: "power") }
                    .help("退出 YouChat Bar（⌘Q）").keyboardShortcut("q")
            }
        }
        .buttonStyle(.borderless)
        .padding(.horizontal, 12).padding(.vertical, 8)
    }
}

// MARK: shared formatting
enum Fmt {
    static func relative(_ d: Date, now: Date = Date()) -> String {
        if abs(d.timeIntervalSince(now)) < 60 { return "刚刚" }   // not "0秒后"
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        f.locale = Locale(identifier: "zh_Hans_CN")   // the rest of the panel is Chinese; "2 min. ago" was not
        return f.localizedString(for: d, relativeTo: now)
    }

    static func duration(_ seconds: Double) -> String {
        let s = max(0, Int(seconds))
        let dd = s / 86400, hh = (s % 86400) / 3600, mm = (s % 3600) / 60
        return dd > 0 ? "\(dd)天\(hh)小时" : (hh > 0 ? "\(hh)小时\(mm)分" : "\(mm)分钟")
    }

    static func countdown(_ d: Date, now: Date = Date()) -> String {
        let s = d.timeIntervalSince(now)
        return s <= 0 ? "已重置" : duration(s) + "后"
    }

    /// How long the weekly budget lasts at the current rate — only set when it runs out early.
    static func exhaustion(_ w: UsageWindow, now: Date = Date()) -> String? {
        guard let p = AppModel.pace(w, now: now), p > 1,
              let r = w.resetsAt, w.usedPercent > 0 else { return nil }
        let elapsed = 7 * 86400 - r.timeIntervalSince(now)
        guard elapsed > 0 else { return nil }
        return duration((100 - w.usedPercent) / (w.usedPercent / elapsed))
    }

    /// Share of the weekly window already gone — where the bar ought to be by now.
    static func elapsedShare(_ w: UsageWindow, now: Date = Date()) -> Double? {
        guard w.label.contains("Week"), let r = w.resetsAt, r > now else { return nil }
        let e = 1 - r.timeIntervalSince(now) / (7 * 86400)
        return e > 0.02 && e < 1 ? e * 100 : nil
    }

    /// Bar fill. Tighter than the old 80/95 so a warning arrives before the window is gone.
    static func color(_ pct: Double) -> Color {
        pct >= 90 ? .red : (pct >= 75 ? .orange : .green)
    }

    /// The number is already the datum, so it stays plain type until it is worth noticing.
    static func numberColor(_ pct: Double) -> Color {
        pct >= 90 ? .red : (pct >= 75 ? .orange : .primary)
    }
}

// MARK: shared chrome
/// Both tabs are built out of these, so Groups and Usage read as one surface.
struct Card<Content: View>: View {
    private let accent: Color?
    private let content: Content

    init(accent: Color? = nil, @ViewBuilder content: () -> Content) {
        self.accent = accent
        self.content = content()
    }

    var body: some View {
        content
            .padding(10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 10).fill(Color.primary.opacity(0.045)))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(accent ?? Color.primary.opacity(0.08)))
    }
}

/// Caption above a block of rows: 用途 / 成员与分工 / 最近消息.
struct SectionLabel: View {
    let text: String
    let systemImage: String

    var body: some View {
        Label(text, systemImage: systemImage)
            .font(.caption2).fontWeight(.medium).foregroundStyle(.tertiary)
    }
}

/// Tag for statuses, CLIs and message kinds.
struct Pill: View {
    let text: String
    let color: Color

    var body: some View {
        Text(text)
            .font(.caption2).foregroundStyle(color)
            .padding(.horizontal, 5).padding(.vertical, 1)
            .background(Capsule().fill(color.opacity(0.18)))
    }
}

/// Usage bar; `height` is what separates a provider's headline window from its others.
/// `marker` is the pace tick: fill past it means the window is being burned faster than it refills.
struct UsageBar: View {
    let percent: Double
    var height: CGFloat = 6
    var marker: Double? = nil

    var body: some View {
        GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.09))
                Capsule().fill(Fmt.color(percent))
                    .frame(width: clamp(percent) * g.size.width)
                if let m = marker {
                    Rectangle().fill(Color.primary.opacity(0.5))
                        .frame(width: 2).offset(x: clamp(m) * g.size.width - 1)
                }
            }
        }
        .frame(height: height)
    }

    private func clamp(_ v: Double) -> Double { min(max(v, 0), 100) / 100 }
}
