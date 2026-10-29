// Minimal read-only client for the youchat hub (Unix socket, one JSON object per line).
// It never sends "hello", so it has no identity and cannot affect wakes or deliveries.
import Foundation

enum HubError: Error, CustomStringConvertible {
    case unavailable(String)
    case rejected(String)
    var description: String {
        switch self {
        case .unavailable(let s): return "hub unavailable: " + s
        case .rejected(let s): return "hub error: " + s
        }
    }
}

struct HubClient {
    var path: String = HubClient.defaultPath()

    static func defaultPath() -> String {
        if let p = ProcessInfo.processInfo.environment["YOUCHAT_SOCK"], !p.isEmpty {
            return (p as NSString).expandingTildeInPath
        }
        return "/tmp/youchat-\(getuid())/hub.sock"
    }

    /// Open a connection, send one request, return the raw `result` JSON bytes.
    func call(_ op: String, _ args: [String: Any] = [:], timeout: Int = 10) throws -> Data {
        let fd = socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { throw HubError.unavailable("socket() failed") }
        defer { close(fd) }
        var tv = timeval(tv_sec: timeout, tv_usec: 0)
        setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let bytes = Array(path.utf8)
        let cap = MemoryLayout.size(ofValue: addr.sun_path) - 1
        guard bytes.count <= cap else { throw HubError.unavailable("socket path too long") }
        withUnsafeMutableBytes(of: &addr.sun_path) { buf in
            for (i, b) in bytes.enumerated() { buf[i] = b }
        }
        let ok = withUnsafePointer(to: &addr) {
            $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard ok == 0 else {
            throw HubError.unavailable("\(path): \(String(cString: strerror(errno)))")
        }

        let req: [String: Any] = ["id": 1, "op": op, "args": args]
        var line = try JSONSerialization.data(withJSONObject: req)
        line.append(0x0A)
        let sent = line.withUnsafeBytes { send(fd, $0.baseAddress, line.count, 0) }
        guard sent == line.count else { throw HubError.unavailable("send failed") }

        var buf = Data()
        var chunk = [UInt8](repeating: 0, count: 65536)
        while !buf.contains(0x0A) {
            let n = recv(fd, &chunk, chunk.count, 0)
            if n <= 0 { throw HubError.unavailable("no reply from hub") }
            buf.append(chunk, count: n)
        }
        let first = buf.prefix(upTo: buf.firstIndex(of: 0x0A)!)
        guard let resp = try JSONSerialization.jsonObject(with: first) as? [String: Any] else {
            throw HubError.rejected("bad reply")
        }
        guard (resp["ok"] as? Bool) == true else {
            throw HubError.rejected(resp["error"] as? String ?? "unknown error")
        }
        return try JSONSerialization.data(withJSONObject: resp["result"] ?? NSNull(),
                                          options: [.fragmentsAllowed])
    }

    func teams() throws -> [Team] {
        try JSONDecoder().decode([Team].self, from: call("teams"))
    }

    func recentMessages(limit: Int = 500) throws -> [Message] {
        try JSONDecoder().decode([Message].self, from: call("log", ["limit": limit]))
    }
}
