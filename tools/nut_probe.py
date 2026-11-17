#!/usr/bin/env python3
"""最小的 NUT 探针：登录上游 upsd，列出可写变量与支持的指令。"""
import socket, sys

host = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1"
upsname = sys.argv[2] if len(sys.argv) > 2 else "ups0"

s = socket.create_connection((host, 3493), timeout=5)
f = s.makefile("rwb")

def cmd(c):
    f.write((c + "\n").encode())
    f.flush()
    out = []
    while True:
        line = f.readline().decode("utf-8", "replace").strip()
        if not line:
            break
        out.append(line)
        if line.startswith("END LIST") or line.startswith("ERR"):
            break
        if line.startswith("OK") and not c.startswith("LIST"):
            break
    return out

print("LOGIN:", cmd("LOGIN " + upsname))
for label, c in (("RW", "LIST RW " + upsname), ("CMD", "LIST CMD " + upsname)):
    print("--- LIST " + label + " ---")
    for line in cmd(c):
        print("  " + line)
s.close()
