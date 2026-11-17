"""可控假 upsd：状态从文件读，便于脚本编排时序。

用法：
  python3 mockupsd2.py <port> <statusfile>
  echo "OL" > statusfile    # 改状态
文件里第一行是 ups.status；若写 FSD 则额外返回 FSD 标记。
"""
import socket, sys, threading

port = int(sys.argv[1])
sf = sys.argv[2]


def cur():
    try:
        with open(sf) as f:
            return f.read().strip()
    except Exception:
        return "OL"


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
            if cmd[0] in ("USERNAME", "PASSWORD", "LOGIN"):
                send("OK")
            elif cmd[0] == "LIST" and len(cmd) > 2 and cmd[1] == "VAR":
                st = cur()
                if "FSD" in st.split():
                    send("FSD")
                send('VAR ups0 ups.status "%s"' % st)
                send('VAR ups0 battery.charge "97"')
                send('VAR ups0 device.mfr "APC"')
                send('VAR ups0 device.model "BK650M2-CH"')
                send("END LIST VAR ups0")
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
s.bind(("127.0.0.1", port)); s.listen(8)
print("mock on", port, "status file", sf, flush=True)
while True:
    c, _ = s.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
