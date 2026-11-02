// Menu bar button via plain AppKit NSStatusItem. SwiftUI MenuBarExtra created the item on
// macOS 26 but drew it empty, so the icon/title are set on the button directly here.
import AppKit
import SwiftUI

@MainActor
final class StatusItemController: NSObject, NSPopoverDelegate {
    private let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let popover = NSPopover()
    private let model: AppModel
    private var timer: Timer?
    private var globalMonitor: Any?
    private var resignObserver: NSObjectProtocol?
    private var lastClosedAt: Date = .distantPast
    private var lastSymbol = ""

    init(model: AppModel) {
        self.model = model
        super.init()
        popover.behavior = .transient
        popover.delegate = self
        popover.contentViewController = NSHostingController(
            rootView: ContentView().environmentObject(model).frame(width: 460, height: 600))
        if let b = item.button {
            b.imagePosition = .imageLeading
            b.font = .monospacedDigitSystemFont(ofSize: NSFont.systemFontSize, weight: .regular)
            b.target = self
            b.action = #selector(toggle)
        }
        refreshTitle()
        timer = Timer.scheduledTimer(withTimeInterval: 60, repeats: true) { [weak self] _ in
            Task { @MainActor in self?.refreshTitle() }
        }
    }

    private func refreshTitle() {
        let w = model.worstWeekly
        let name = w >= 95 ? "exclamationmark.bubble.fill" : (w >= 85 ? "exclamationmark.bubble" : "bubble.left.and.bubble.right")
        if name != lastSymbol {
            lastSymbol = name
            let img = NSImage(systemSymbolName: name, accessibilityDescription: "YouChat Bar")
            img?.isTemplate = true
            item.button?.image = img
        }
        item.button?.title = " " + model.menuTitle
    }

    @objc private func toggle() {
        guard let b = item.button else { return }
        if popover.isShown {
            closePopover()
        } else if Date().timeIntervalSince(lastClosedAt) > 0.25 {
            model.refreshGroups()
            popover.show(relativeTo: b.bounds, of: b, preferredEdge: .minY)
            NSApp.activate(ignoringOtherApps: true)
            popover.contentViewController?.view.window?.makeKeyAndOrderFront(nil)
            startOutsideMonitor()
        }
    }

    private func closePopover() {
        stopOutsideMonitor()
        if popover.isShown {
            lastClosedAt = Date()
            popover.performClose(nil)
        }
    }

    func popoverDidClose(_ notification: Notification) {
        lastClosedAt = Date()
        stopOutsideMonitor()
    }

    private func startOutsideMonitor() {
        stopOutsideMonitor()
        globalMonitor = NSEvent.addGlobalMonitorForEvents(matching: [.leftMouseDown, .rightMouseDown, .otherMouseDown]) { [weak self] _ in
            Task { @MainActor in self?.closePopover() }
        }
        resignObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.didResignActiveNotification, object: nil, queue: .main
        ) { [weak self] _ in
            Task { @MainActor in self?.closePopover() }
        }
    }

    private func stopOutsideMonitor() {
        if let m = globalMonitor { NSEvent.removeMonitor(m); globalMonitor = nil }
        if let o = resignObserver { NotificationCenter.default.removeObserver(o); resignObserver = nil }
    }
}
