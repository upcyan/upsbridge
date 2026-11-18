#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LB 误报防护的时序演练。全部在 127.0.0.1 上用假 upsd 跑，不碰真机。"""
import json, os, socket, subprocess, sys, tempfile, time, shutil

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app", "target", "upsbridge", "upsbridge.py")
MOCK = os.path.join(HERE, "mockupsd3.py")
def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def main():
    global PORT
    PORT = free_port()
    HTTP = free_port()
    tmp = tempfile.mkdtemp(prefix="upsbridge-lbtest-")
    statef = os.path.join(tmp, "state.json")
    logf = os.path.join(tmp, "app.log")
    conff = os.path.join(tmp, "config.json")
    cfg = {
        "upstream": {"host": "127.0.0.1", "port": PORT, "upsname": "ups0",
                     "username": "nut", "password": "nut"},
        "rules": {"dry_run": True, "shutdown_on_battery_seconds": 300,
                  "shutdown_on_lowbattery": True, "shutdown_on_fsd": True,
                  "fsd_grace_seconds": 5, "lb_grace_seconds": 3,
                  "battery_charge_low_override": 0, "deadtime": 60,
                  "poll_interval": 1, "shutdown_cmd": "/bin/true"},
        "notify": {"wall": False},
    }
    with open(conff, "w") as f:
        json.dump(cfg, f)

    def setstate(**kw):
        with open(statef, "w") as f:
            json.dump(kw, f)

    base_ok = dict({"ups.status": "OL", "battery.charge": 100,
                    "battery.runtime": 2403, "battery.charge.low": 15,
                    "battery.runtime.low": 120,
                    "device.mfr": "UGREEN", "device.model": "US3000"})
    setstate(**base_ok)

    mock = subprocess.Popen([sys.executable, MOCK, str(PORT), statef],
                            stdout=subprocess.DEVNULL)
    # 等假 upsd 真的在监听，否则应用连不上、所有用例变成假通过
    for _ in range(60):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.3).close()
            break
        except OSError:
            time.sleep(0.1)
    else:
        raise RuntimeError("mock upsd 没起来")
    app = subprocess.Popen([sys.executable, APP, "--config", conff,
                            "--port", str(HTTP), "--log", logf],
                           stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(2.5)

    def logtext():
        # 走应用自己的 /api/log，避免"文件有没有落盘"干扰判定
        import urllib.request
        try:
            raw = urllib.request.urlopen(
                "http://127.0.0.1:%d/api/log?limit=100000" % HTTP, timeout=5).read()
            d = json.loads(raw.decode("utf-8"))
            return "\n".join(d.get("lines") or [])
        except Exception:
            return ""

    def mark():
        return len(logtext())

    def since(n):
        return logtext()[n:]

    def wait_contains(sub, timeout=12):
        end = time.time() + timeout
        while time.time() < end:
            if sub in logtext():
                return True
            time.sleep(0.2)
        return False

    results = []

    def case(name, ok, detail=""):
        results.append((name, ok, detail))
        print(("  PASS  " if ok else "  FAIL  ") + name + (("  — " + detail) if detail else ""))

    try:
        print("[1] 市电正常时的瞬时 LB（绿联 03:16 那种）")
        n = mark()
        setstate(**dict(base_ok, **{"ups.status": "OL LB"}))
        time.sleep(4)
        seg = since(n)
        case("OL LB 不触发关机", "触发条件满足" not in seg, seg.strip().replace("\n", " | ")[:160])
        case("OL LB 写明原因", "市电正常" in seg or "不在电池供电" in seg)

        print("[2] 电池供电但数值不支持 LB")
        setstate(**dict(base_ok, **{"ups.status": "OB LB"}))
        n = mark()
        time.sleep(4)
        seg = since(n)
        case("OB LB 数值不支持时不触发", "触发条件满足" not in seg, seg.strip().replace("\n", " | ")[:160])
        case("写明数值核对", "数值不支持" in seg)

        print("[3] 真低电量：OB + LB + 电量 10% < 阈值 15%")
        n = mark()
        setstate(**dict(base_ok, **{"ups.status": "OB LB", "battery.charge": 10,
                                    "battery.runtime": 90}))
        # 注意：启动日志里本身就有"演练模式"字样，必须等真正的触发行
        ok = wait_contains("触发条件满足", 15)
        seg = since(n)
        case("真低电量触发（演练）", ok, seg.strip().replace("\n", " | ")[:200])
        case("触发原因带确认时长", "已确认" in seg, seg.strip().replace("\n", " | ")[-260:])

        print("[4] 复位后：LB 抖一下就消失")
        subprocess.run(["python3", "-c", "import urllib.request;urllib.request.urlopen("
                        "urllib.request.Request('http://127.0.0.1:%d/api/reset_shutdown',"
                        "data=b'{}',method='POST'))" % HTTP], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        setstate(**dict(base_ok, **{"ups.status": "OB LB", "battery.charge": 10}))
        time.sleep(1.5)
        n = mark()
        setstate(**dict(base_ok, **{"ups.status": "OB", "battery.charge": 60}))
        time.sleep(4)
        seg = since(n)
        case("LB 消失后不触发", "触发条件满足" not in seg, seg.strip().replace("\n", " | ")[:160])
        case("写明取消确认", "取消确认" in seg)

        print("[5] 断电时长规则仍然有效")
        subprocess.run(["python3", "-c", "import urllib.request;urllib.request.urlopen("
                        "urllib.request.Request('http://127.0.0.1:%d/api/reset_shutdown',"
                        "data=b'{}',method='POST'))" % HTTP], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import urllib.request
        patch = json.dumps({"rules": {"shutdown_on_battery_seconds": 3,
                                      "lb_grace_seconds": 30}}).encode()
        urllib.request.urlopen(urllib.request.Request(
            "http://127.0.0.1:%d/api/config" % HTTP, data=patch,
            method="POST", headers={"Content-Type": "application/json"}), timeout=5).read()
        n = mark()
        setstate(**dict(base_ok, **{"ups.status": "OB", "battery.charge": 80}))
        ok = wait_contains("断电已持续", 15)
        case("按断电时长触发仍然正常", ok, since(n).strip().replace("\n", " | ")[:160])
    finally:
        for p in (app, mock):
            try:
                p.terminate(); p.wait(timeout=5)
            except Exception:
                try: p.kill()
                except Exception: pass
        shutil.rmtree(tmp, ignore_errors=True)

    bad = [r for r in results if not r[1]]
    print("\n%d/%d 通过" % (len(results) - len(bad), len(results)))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(main())
