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

    func deleteTeam(_ name: String) throws {
        do {
            _ = try call("team_delete", ["name": name])
            return
        } catch let HubError.rejected(msg) where !msg.contains("unknown op") {
            throw HubError.rejected(msg)
        } catch {}
        let py = "import os,sqlite3,sys;n=sys.argv[1];p=os.path.expanduser(os.environ.get('YOUCHAT_HOME') or '~/.youchat')+'/hub.db';c=sqlite3.connect(p,timeout=5.0,isolation_level=None);c.execute('DELETE FROM members WHERE team=?',(n,));c.execute('DELETE FROM teams WHERE name=?',(n,));print('YOUCHAT_DEL_OK')"
        let out = try UsageProviders.run("/usr/bin/python3", ["-c", py, name], timeout: 5)
        if !out.contains("YOUCHAT_DEL_OK") {
            throw HubError.rejected(out.trimmingCharacters(in: .whitespacesAndNewlines))
        }
    }

    static func defaultRemoteHosts() -> [String] {
        if let env = ProcessInfo.processInfo.environment["YOUCHAT_REMOTE_HOSTS"], !env.isEmpty {
            return env.split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty }
        }
        let cfg = "\(UsageProviders.home)/.ssh/config"
        if let txt = try? String(contentsOfFile: cfg, encoding: .utf8),
           txt.range(of: #"(?m)^\s*Host\s+.*\bmacmini\b"#, options: .regularExpression) != nil {
            return ["macmini"]
        }
        return []
    }

    private static let remoteDumpPy = #"""
import json,os,socket,sqlite3
sock=os.environ.get('YOUCHAT_SOCK') or ('/tmp/youchat-%d/hub.sock'%os.getuid())
def call(op,a={}):
 s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(4);s.connect(sock)
 s.sendall((json.dumps({'id':1,'op':op,'args':a})+'\n').encode());b=b''
 while b'\n' not in b:
  c=s.recv(65536)
  if not c:break
  b+=c
 s.close();r=json.loads(b.split(b'\n',1)[0])
 if not r.get('ok'):raise RuntimeError(r.get('error','err'))
 return r['result']
try:
 ts,ms=call('teams'),call('log',{'limit':500})
except Exception:
 p=os.path.expanduser(os.environ.get('YOUCHAT_HOME') or '~/.youchat')+'/hub.db'
 if not os.path.exists(p):ts,ms=[],[]
 else:
  c=sqlite3.connect(p);c.row_factory=sqlite3.Row;ts=[]
  for t in c.execute('SELECT * FROM teams ORDER BY created_at'):
   d=dict(t);d['members']=[dict(m,online=False) for m in c.execute('SELECT * FROM members WHERE team=? ORDER BY joined_at',(d['name'],))]
   ts.append(d)
  ms=[dict(r) for r in c.execute('SELECT * FROM (SELECT * FROM messages ORDER BY seq DESC LIMIT 500) ORDER BY seq')]
print('YOUCHAT_JSON:'+json.dumps({'teams':ts,'messages':ms}))
"""#

    static func fetchRemote(host: String, timeout: TimeInterval = 8) throws -> RemoteSnapshot {
        let b64 = Data(remoteDumpPy.utf8).base64EncodedString()
        let cmd = "export PATH=\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH\"; youchat hub --daemon >/dev/null 2>&1 || true; python3 -c \"import base64;exec(base64.b64decode('\(b64)'))\""
        let out = try UsageProviders.run("/usr/bin/ssh", ["-o", "BatchMode=yes", "-o", "ConnectTimeout=4", host, cmd], timeout: timeout)
        guard let line = out.components(separatedBy: .newlines).first(where: { $0.hasPrefix("YOUCHAT_JSON:") }) else {
            let err = out.trimmingCharacters(in: .whitespacesAndNewlines)
            throw HubError.unavailable(err.isEmpty ? "ssh \(host) failed" : String(err.prefix(160)))
        }
        let jsonStr = String(line.dropFirst("YOUCHAT_JSON:".count))
        var snap = try JSONDecoder().decode(RemoteSnapshot.self, from: Data(jsonStr.utf8))
        snap.teams = snap.teams.map { t in var copy = t; copy.sshHost = host; return copy }
        return snap
    }

    static func deleteRemoteTeam(host: String, name: String, timeout: TimeInterval = 8) throws {
        let py = #"""
import json,os,socket,sqlite3,sys
n=sys.argv[1]
sock=os.environ.get('YOUCHAT_SOCK') or ('/tmp/youchat-%d/hub.sock'%os.getuid())
done=False
try:
 s=socket.socket(socket.AF_UNIX,socket.SOCK_STREAM);s.settimeout(4);s.connect(sock)
 s.sendall((json.dumps({'id':1,'op':'team_delete','args':{'name':n}})+'\n').encode());b=b''
 while b'\n' not in b:
  c=s.recv(65536)
  if not c:break
  b+=c
 s.close();r=json.loads(b.split(b'\n',1)[0])
 if r.get('ok'):done=True
 elif 'unknown op' not in str(r.get('error','')):sys.exit(r.get('error','err'))
except SystemExit:raise
except Exception:pass
if not done:
 p=os.path.expanduser(os.environ.get('YOUCHAT_HOME') or '~/.youchat')+'/hub.db'
 c=sqlite3.connect(p,timeout=5.0,isolation_level=None)
 c.execute('DELETE FROM members WHERE team=?',(n,))
 c.execute('DELETE FROM teams WHERE name=?',(n,))
print('YOUCHAT_DEL_OK')
"""#
        let b64 = Data(py.utf8).base64EncodedString()
        let nameB64 = Data(name.utf8).base64EncodedString()
        let cmd = "export PATH=\"$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH\"; python3 -c \"import base64,sys;sys.argv=['',base64.b64decode('\(nameB64)').decode()];exec(base64.b64decode('\(b64)'))\""
        let out = try UsageProviders.run("/usr/bin/ssh", ["-o", "BatchMode=yes", "-o", "ConnectTimeout=4", host, cmd], timeout: timeout)
        if !out.contains("YOUCHAT_DEL_OK") {
            throw HubError.unavailable(out.trimmingCharacters(in: .whitespacesAndNewlines))
        }
    }
}
