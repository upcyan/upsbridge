
import socket, threading, sys, time

MODE = {"v": "OL", "fsd": False}
PORT = 13493

def handle(c):
    f = c.makefile("rwb")
    def send(s):
        f.write((s+"\n").encode()); f.flush()
    try:
        for line in f:
            line = line.decode().strip()
            if not line: continue
            cmd = line.split()
            if cmd[0] == "USERNAME": send("OK")
            elif cmd[0] == "PASSWORD": send("OK")
            elif cmd[0] == "LOGIN": send("OK")
            elif cmd[0] == "LIST" and len(cmd) > 2 and cmd[1] == "VAR":
                if MODE["fsd"]: send("FSD")
                send('VAR ups0 ups.status "%s"' % MODE["v"])
                send('VAR ups0 battery.charge "97"')
                send('VAR ups0 device.mfr "APC"')
                send('VAR ups0 device.model "BK650M2-CH"')
                send('VAR ups0 battery.runtime "1200"')
                send("END LIST VAR ups0")
            else: send("ERR UNKNOWN-COMMAND")
    except Exception:
        pass
    finally:
        try: c.close()
        except Exception: pass

s = socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
s.bind(("127.0.0.1", PORT)); s.listen(8)
print("mock upsd on", PORT, flush=True)

def ctl():
    for line in sys.stdin:
        k, _, v = line.strip().partition("=")
        if k == "status": MODE["v"] = v
        if k == "fsd": MODE["fsd"] = (v == "1")
        print("mock:", MODE, flush=True)

threading.Thread(target=ctl, daemon=True).start()
while True:
    c, _ = s.accept()
    threading.Thread(target=handle, args=(c,), daemon=True).start()
