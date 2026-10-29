// Collect usage from each vendor through its own CLI or local files.
// Claude: `claude -p "/usage" --no-session-persistence`, AGY: `agy -p "/usage"`,
// Codex: last `rate_limits` event in ~/.codex/sessions. No tokens, no private HTTP APIs.
import Foundation

enum UsageProviders {
    static let home = FileManager.default.homeDirectoryForCurrentUser.path

    /// GUI apps get a bare PATH from launchd; add the usual install locations.
    static var searchPath: String {
        let extra = ["\(home)/.local/bin", "/opt/homebrew/bin", "/usr/local/bin", "\(home)/.npm-global/bin"]
        let cur = ProcessInfo.processInfo.environment["PATH"] ?? "/usr/bin:/bin"
        return (extra + [cur]).joined(separator: ":")
    }

    static func which(_ name: String) -> String? {
        for dir in searchPath.split(separator: ":") {
            let p = "\(dir)/\(name)"
            if FileManager.default.isExecutableFile(atPath: p) { return p }
        }
        return nil
    }

    /// Working directory for the CLI probes, so they never pick up a project's settings.
    static var workDir: String {
        let d = "\(home)/.youchat/mac"
        try? FileManager.default.createDirectory(atPath: d, withIntermediateDirectories: true)
        return d
    }

    /// Run a command, return its stdout+stderr. Kills it after `timeout` seconds.
    static func run(_ exe: String, _ args: [String], timeout: TimeInterval = 60) throws -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        p.currentDirectoryURL = URL(fileURLWithPath: workDir)
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = searchPath
        env["NO_COLOR"] = "1"
        p.environment = env
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        p.standardInput = FileHandle.nullDevice
        var out = Data()
        let lock = NSLock()
        pipe.fileHandleForReading.readabilityHandler = { h in
            let d = h.availableData
            lock.lock(); out.append(d); lock.unlock()
        }
        try p.run()
        let deadline = Date().addingTimeInterval(timeout)
        while p.isRunning && Date() < deadline { Thread.sleep(forTimeInterval: 0.2) }
        if p.isRunning { p.terminate() }
        p.waitUntilExit()
        pipe.fileHandleForReading.readabilityHandler = nil
        lock.lock(); out.append(pipe.fileHandleForReading.readDataToEndOfFile()); lock.unlock()
        return String(decoding: out, as: UTF8.self)
    }

    static func stripANSI(_ s: String) -> String {
        s.replacingOccurrences(of: "\u{1B}\\[[0-9;?]*[A-Za-z]", with: "", options: .regularExpression)
    }

    static func claude() -> ProviderUsage {
        var u = ProviderUsage(provider: .claude)
        guard let exe = which("claude") else { u.error = "claude not found in PATH"; return u }
        do {
            let out = stripANSI(try run(exe, ["-p", "/usage", "--no-session-persistence"]))
            u.windows = UsageParsers.parseClaude(out)
            u.asOf = Date()
            if u.windows.isEmpty { u.error = String(out.prefix(200)) }
        } catch { u.error = "\(error)" }
        return u
    }

    static func agy() -> ProviderUsage {
        var u = ProviderUsage(provider: .agy)
        guard let exe = which("agy") else { u.error = "agy not found in PATH"; return u }
        do {
            let out = stripANSI(try run(exe, ["-p", "/usage"]))
            u.windows = UsageParsers.parseAgy(out)
            u.asOf = Date()
            if u.windows.isEmpty { u.error = String(out.prefix(200)) }
        } catch { u.error = "\(error)" }
        return u
    }

    /// Last `size` bytes of a file, split into lines (newest last).
    static func tailLines(_ path: String, size: Int = 1 << 20) -> [String] {
        guard let h = FileHandle(forReadingAtPath: path) else { return [] }
        defer { try? h.close() }
        let end = h.seekToEndOfFile()
        h.seek(toFileOffset: end > UInt64(size) ? end - UInt64(size) : 0)
        return String(decoding: h.readDataToEndOfFile(), as: UTF8.self).components(separatedBy: "\n")
    }

    static func codex() -> ProviderUsage {
        var u = ProviderUsage(provider: .codex)
        let root = URL(fileURLWithPath: "\(home)/.codex/sessions")
        let keys: [URLResourceKey] = [.contentModificationDateKey]
        guard let it = FileManager.default.enumerator(at: root, includingPropertiesForKeys: keys) else {
            u.error = "~/.codex/sessions not found"; return u
        }
        var files: [(URL, Date)] = []
        for case let f as URL in it where f.pathExtension == "jsonl" {
            let d = (try? f.resourceValues(forKeys: Set(keys)))?.contentModificationDate ?? .distantPast
            files.append((f, d))
        }
        // The newest few rollouts; keep the most recent rate_limits event across them.
        for (f, _) in files.sorted(by: { $0.1 > $1.1 }).prefix(8) {
            for line in tailLines(f.path).reversed() where line.contains("\"rate_limits\"") {
                guard let r = UsageParsers.parseCodexLine(line) else { continue }
                if u.asOf == nil || (r.asOf ?? .distantPast) > u.asOf! {
                    u.windows = r.windows; u.asOf = r.asOf
                }
                break
            }
        }
        if u.windows.isEmpty { u.error = "no rate_limits yet — run any Codex turn once" }
        return u
    }

    static func fetch(_ p: Provider) -> ProviderUsage {
        switch p {
        case .claude: return claude()
        case .codex: return codex()
        case .agy: return agy()
        }
    }

    // MARK: cache (~/.youchat/mac/usage.json) so the menu shows numbers right after launch
    static var cacheURL: URL { URL(fileURLWithPath: "\(workDir)/usage.json") }

    static func loadCache() -> [ProviderUsage] {
        guard let d = try? Data(contentsOf: cacheURL) else { return [] }
        let dec = JSONDecoder()
        dec.dateDecodingStrategy = .secondsSince1970
        return (try? dec.decode([ProviderUsage].self, from: d)) ?? []
    }

    static func saveCache(_ rows: [ProviderUsage]) {
        let enc = JSONEncoder()
        enc.dateEncodingStrategy = .secondsSince1970
        enc.outputFormatting = [.prettyPrinted, .sortedKeys]
        if let d = try? enc.encode(rows) { try? d.write(to: cacheURL, options: .atomic) }
    }
}
