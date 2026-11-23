#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""upsbridge 特权助手 —— 只以 root 运行。

前台服务降权到应用用户跑，需要特权动作时通过 unix socket 找这里。
只提供白名单里的两件事，别的一概不认：

  {"cmd": "ping", "host": "192.168.1.1", "timeout": 2}
  {"cmd": "poweroff"}

防护：
  - socket 权限 0660，属主 root、属组为应用组；
  - 连接时用 SO_PEERCRED 核对对端 uid，只认 root 和应用用户；
  - ping 目标做严格校验，禁止 - 开头（防参数注入）；
  - 单连接只处理一行，请求体上限 4KB。
"""
import ipaddress, json, os, re, socket, socketserver, struct, subprocess, sys, time

MAX_LINE = 4096
CONN_TIMEOUT = 60
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,62}[A-Za-z0-9])?$")
ALLOW_UIDS = {0}

SHUTDOWN_BINS = ["/usr/bin/systemctl", "/sbin/poweroff", "/sbin/shutdown"]


def valid_host(value):
    t = str(value or "").strip()
    if not t or len(t) > 64 or t.startswith("-"):
        return None
    try:
        ipaddress.ip_address(t)
        return t
    except ValueError:
        pass
    if not HOST_RE.match(t):
        return None
    for label in t.split("."):
        if not label or len(label) > 63 or label.startswith("-") or label.endswith("-"):
            return None
    return t


def peer_uid(conn):
    try:
        raw = conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", raw)
        return uid
    except (OSError, AttributeError, ValueError):
        return None


def ping_bin():
    for c in ("/usr/bin/ping", "/bin/ping", "/sbin/ping", "/usr/sbin/ping"):
        if os.path.exists(c):
            return c
    return "/usr/bin/ping"


def do_ping(host, timeout):
    t0 = time.time()
    cmd = [ping_bin(), "-n", "-c", "1"]
    cmd += (["-W", str(timeout * 1000)] if sys.platform == "darwin" else ["-W", str(timeout)])
    cmd.append(host)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 5)
    except Exception as exc:
        return {"ok": False, "rtt_ms": None, "error": str(exc)[:120],
                "cost_ms": int((time.time() - t0) * 1000)}
    if p.returncode != 0:
        low = (p.stdout + "\n" + p.stderr).lower()
        if "destination host unreachable" in low:
            err = "目标主机不可达"
        elif "100% packet loss" in low:
            err = "超时无响应"
        else:
            err = "无响应"
        return {"ok": False, "rtt_ms": None, "error": err,
                "cost_ms": int((time.time() - t0) * 1000)}
    rtt = None
    m = re.search(r"time[=<]([0-9.]+)\s*ms", p.stdout)
    if m:
        rtt = float(m.group(1))
    return {"ok": True, "rtt_ms": rtt, "error": "",
            "cost_ms": int((time.time() - t0) * 1000)}


def do_poweroff():
    try:
        subprocess.run(["sync"], timeout=30)
    except Exception:
        pass
    for cmd in (["/usr/bin/systemctl", "poweroff"],
                ["/sbin/poweroff"],
                ["/sbin/shutdown", "-h", "now"]):
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
            if p.returncode == 0:
                return {"ok": True, "error": ""}
        except Exception:
            continue
    return {"ok": False, "error": "关机命令执行失败"}


def handle(raw):
    try:
        req = json.loads(raw)
    except ValueError:
        return {"ok": False, "error": "非法请求"}
    if not isinstance(req, dict):
        return {"ok": False, "error": "非法请求"}
    cmd = req.get("cmd")
    if cmd == "ping":
        host = valid_host(req.get("host", ""))
        if not host:
            return {"ok": False, "error": "目标地址不合法"}
        try:
            timeout = int(req.get("timeout", 2))
        except (TypeError, ValueError):
            timeout = 2
        return do_ping(host, max(1, min(30, timeout)))
    if cmd == "poweroff":
        return do_poweroff()
    return {"ok": False, "error": "未知命令"}


class Handler(socketserver.StreamRequestHandler):
    timeout = CONN_TIMEOUT

    def handle(self):
        try:
            uid = peer_uid(self.connection)
            if uid is not None and uid not in ALLOW_UIDS:
                self.wfile.write(b'{"ok": false, "error": "forbidden"}\n')
                return
            line = self.rfile.readline(MAX_LINE)
            if not line:
                return
            resp = handle(line.decode("utf-8", "replace"))
            self.wfile.write((json.dumps(resp, ensure_ascii=False) + "\n").encode("utf-8"))
        except Exception:
            pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 16

    def get_request(self):
        req, _addr = super().get_request()
        return req, ("127.0.0.1", 0)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="upsbridge privileged helper")
    ap.add_argument("--socket", required=True)
    ap.add_argument("--group", default="")
    ap.add_argument("--allow-uid", type=int, default=None)
    args = ap.parse_args()

    if args.allow_uid is not None:
        ALLOW_UIDS.add(args.allow_uid)

    try:
        if os.path.exists(args.socket):
            os.unlink(args.socket)
    except OSError:
        pass

    srv = Server(args.socket, Handler)
    try:
        os.chmod(args.socket, 0o660)
        if args.group:
            import grp
            try:
                os.chown(args.socket, 0, grp.getgrnam(args.group).gr_gid)
            except (KeyError, OSError):
                pass
    except OSError:
        pass
    srv.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
