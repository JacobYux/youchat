// Usage tab: 5-hour and weekly windows for Claude / Codex / Antigravity, with reset times.
import SwiftUI

struct UsageView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if let a = model.advice {
                Label(a, systemImage: "lightbulb.fill")
                    .font(.callout).padding(9)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(RoundedRectangle(cornerRadius: 10).fill(Color.yellow.opacity(0.14)))
                    .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Color.yellow.opacity(0.3)))
            }
            ForEach(Provider.allCases) { p in
                ProviderCard(provider: p, usage: model.usage[p], busy: model.refreshing.contains(p))
            }
            Text("Claude / Antigravity 每 15 分钟用 `/usage` 刷新；Codex 读本地会话记录，只在你用过之后才会变。")
                .font(.caption2).foregroundStyle(.tertiary)
        }
        .padding(10)
    }
}

/// One vendor: the weekly window it is judged by up top, its other windows underneath.
struct ProviderCard: View {
    let provider: Provider
    let usage: ProviderUsage?
    let busy: Bool

    var body: some View {
        let headline = usage?.weekly
        let others = (usage?.windows ?? []).filter { $0.id != headline?.id }
        Card {
            VStack(alignment: .leading, spacing: 8) {
                header(headline)
                if let w = headline {
                    HeadlineWindow(window: w)
                } else if !busy {
                    Text("暂无数据").font(.caption).foregroundStyle(.secondary)
                }
                if !others.isEmpty {
                    Divider()
                    VStack(alignment: .leading, spacing: 6) {
                        ForEach(others) { SecondaryWindow(window: $0) }
                    }
                }
                if let e = usage?.error {
                    Label(e, systemImage: "exclamationmark.triangle.fill")
                        .font(.caption2).foregroundStyle(.red).lineLimit(3)
                }
            }
        }
    }

    private func header(_ headline: UsageWindow?) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(provider.title).font(.headline)
            if busy {
                ProgressView().controlSize(.small)
            } else if let t = usage?.asOf {
                Text("截至 \(Fmt.relative(t))").font(.caption2).foregroundStyle(.tertiary)
            }
            Spacer()
            if let w = headline {
                let used = w.effectiveUsed()
                Text("\(Int(used.rounded()))%")
                    .font(.system(.title2, design: .rounded)).bold().monospacedDigit()
                    .foregroundStyle(Fmt.numberColor(used))
            }
        }
    }
}

/// The weekly window the menu bar number comes from: full-width bar, countdown, pace.
struct HeadlineWindow: View {
    let window: UsageWindow

    var body: some View {
        let used = window.effectiveUsed()
        let pace = window.label.contains("Week") ? AppModel.pace(window) : nil
        VStack(alignment: .leading, spacing: 5) {
            UsageBar(percent: used, height: 7, marker: Fmt.elapsedShare(window))
                .help("竖线是本周已经过去的比例；填充超过竖线，就是用得比补得快")
            HStack(spacing: 6) {
                Text(window.label).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                if let r = window.resetsAt {
                    Text("重置 \(Fmt.countdown(r))")
                        .font(.caption).foregroundStyle(.secondary).monospacedDigit()
                        .help(r.formatted(.dateTime.weekday(.abbreviated).month().day().hour().minute()))
                }
                if let p = pace {
                    Pill(text: "节奏 \(Int((p * 100).rounded()))%", color: p > 1 ? .orange : Color.secondary)
                        .help("已用比例 ÷ 本周已过去的时间比例；超过 100% 表示照现在的速度会提前用完")
                }
            }
            if let left = Fmt.exhaustion(window) {
                Label("照现在的速度 \(left) 后就用完了", systemImage: "hare.fill")
                    .font(.caption2).foregroundStyle(.orange)
            }
        }
    }
}

/// Every other window of the vendor, kept deliberately quieter than the headline.
struct SecondaryWindow: View {
    let window: UsageWindow

    var body: some View {
        let used = window.effectiveUsed()
        HStack(spacing: 8) {
            Text(window.label).font(.caption).foregroundStyle(.secondary)
                .lineLimit(1).frame(width: 148, alignment: .leading)
            UsageBar(percent: used, height: 3)
            Text("\(Int(used.rounded()))%")
                .font(.system(.caption, design: .monospaced)).monospacedDigit()
                .foregroundStyle(used >= 75 ? Fmt.numberColor(used) : Color.secondary)
                .frame(width: 32, alignment: .trailing)
            Text(window.resetsAt.map { Fmt.countdown($0) } ?? "")
                .font(.caption2).foregroundStyle(.tertiary).monospacedDigit()
                .lineLimit(1).frame(width: 72, alignment: .trailing)
        }
    }
}
