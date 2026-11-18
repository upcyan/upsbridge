
import socket, sys
def probe(with_banner):
    s = socket.create_connection(("192.168.68.53", 3493), 8)
    s.settimeout(8)
    def rl():
        buf = b""
        while not buf.endswith(b"\n"):
            ch = s.recv(1)
            if not ch: return None
            buf += ch
        return buf.decode(errors="replace").rstrip()
    if with_banner:
        try: print("banner:", repr(rl()))
        except Exception as e: print("banner fail:", e)
    s.sendall(b"USERNAME nut\n"); print("USERNAME ->", repr(rl()))
    s.sendall(b"PASSWORD nut\n"); print("PASSWORD ->", repr(rl()))
    s.sendall(b"LOGIN ups0\n");    print("LOGIN ->", repr(rl()))
    s.sendall(b"LIST VAR ups0\n")
    vars = {}
    while True:
        line = rl()
        if line is None or line.startswith("END LIST"): break
        if line.startswith("VAR "):
            p = line.split(" ", 3)
            if len(p) == 4: vars[p[2]] = p[3].strip('"')
    print("vars:", len(vars))
    for k in ["ups.status","battery.charge","battery.runtime","battery.voltage","battery.charge.low","battery.date","battery.mfr.date","driver.flag.ignorelb","ups.test.result","ups.delay.shutdown"]:
        if k in vars: print("  %-24s = %s" % (k, vars[k]))
    s.close()
probe(False)
