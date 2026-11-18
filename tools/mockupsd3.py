"""可控假 upsd：变量表从 JSON 文件读，用于演练误报时序。

用法：
  python3 mockupsd3.py <port> <statefile.json>
statefile 例：
  {"ups.status": "OL LB", "battery.charge": 100,
   "battery.runtime": 2403, "battery.charge.low": 15, "battery.runtime.low": 120,
   "device.mfr": "UGREEN", "device.model": "US3000"}
改文件即改上游表现；文件里出现 "FSD" token 或 "fsd": true 时额外回 FSD。
"""
import json, socket, sys, threading

port = int(sys.argv[1])
sf = sys.argv[2]


def cur():
    try:
        with open(sf, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"ups.status": "OL"}


def handle(c):
    f = c.makefile("rwb")

    def send(s):
        f.write((s + "\n").encode()); f.flush()
    try:
        for line in f:
            line = line.decode().strip()
            if not line:
                continue
            cmd = line.split()
            k = cmd[0]
            if k in ("USERNAME", "PASSWORD", "LOGIN"):
                send("OK")
            elif k == "LIST" and len(cmd) > 2 and cmd[1] == "VAR":
                v = cur()
                ups = cmd[2]
                if v.get("fsd") or "FSD" in str(v.get("ups.status", "")).split():
                    send("FSD")
                for key, val in v.items():
                    if key == "fsd":
                        continue
                    send('VAR %s %s "%s"' % (ups, key, val))
                send("END LIST VAR " + ups)
            else:
                send("ERR UNKNOWN-COMMAND")
    except Exception:
        pass
    finally:
        try:
            c.close()
        except Exception:
            pass


s = socket.socket()
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", port)); s.listen(16)
print("mock on", port, "state", sf, flush=True)
while True:
    c, _ = s.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
