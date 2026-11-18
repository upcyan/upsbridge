#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""upsbridge - 把局域网里任意 NUT Server 的断电事件，变成这台 NAS 的关机动作。

不依赖也不修改 /etc/nut：自己实现 NUT 协议客户端，配置全部在本应用目录里。
"""
import argparse, json, os, socket, sys, threading, time, subprocess, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

VERSION = "1.1.0"

DEFAULT_CONFIG = {
    # 上游地址留空：首次使用必须由用户在界面里填写，仓库里不写死任何 IP / 账号 / 密码。
    "upstream": {"host": "", "port": 3493, "upsname": "", 
                 "username": "", "password": ""},
    "rules": {
        "dry_run": True,
        "shutdown_on_battery_seconds": 300,
        "shutdown_on_lowbattery": True,
        "shutdown_on_fsd": True,
        "fsd_grace_seconds": 15,
        "lb_grace_seconds": 30,
        "battery_charge_low_override": 0,
        "deadtime": 30,
        "poll_interval": 5,
        "shutdown_cmd": "/sbin/shutdown -h +0",
    },
    "notify": {"wall": True},
}


def fmt_num(x, unit=""):
    """日志里展示用：None -> "?"，整数去掉小数点。"""
    if x is None:
        return "?"
    try:
        f = float(x)
    except Exception:
        return str(x)
    return (("%d" % f) if f == int(f) else ("%.1f" % f)) + unit


def deep_merge(base, patch):
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


LOG_LEVELS = ("info", "ok", "warn", "error")
LEVEL_TAG = {"info": "INFO", "ok": "OK", "warn": "WARN", "error": "ERROR"}
LEVEL_LABEL = {"info": "信息", "ok": "正常", "warn": "注意", "error": "严重"}
_LINE_RE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)\s+(?:\[(\w+)\]\s+)?(.*)$")


def guess_level(msg):
    """给老格式（无级别标记）的日志行推断级别，用于读回历史文件。"""
    if msg.startswith("触发关机：") or "关机命令执行失败" in msg:
        return "error"
    for w in ("失败", "失联", "中断", "未配置", "未填写", "陈旧", "更换电池", "FSD",
              "演练", "低电量", "复位", "清空"):
        if w in msg:
            return "warn"
    for w in ("市电恢复", "恢复通信", "启动", "监听", "已保存"):
        if w in msg:
            return "ok"
    return "info"


class Log:
    """结构化运行日志。

    - 内存保留最近 cap 条（默认 2000），每条带 seq，页面按 seq 增量拉取；
    - 同时追加写盘，进程重启后能把历史读回来（旧格式行按关键词补级别）；
    - 文件超过 max_bytes 时只重写最后 400 条，避免无限增长。
    """

    def __init__(self, path, cap=2000, max_bytes=1024 * 1024):
        self.path = path
        self.cap = cap
        self.max_bytes = max_bytes
        self.entries = []
        self._seq = 0
        self._bytes = 0
        self.lock = threading.Lock()
        self._load()

    def _load(self):
        try:
            self._bytes = os.path.getsize(self.path)
        except OSError:
            self._bytes = 0
        if not self._bytes:
            return
        try:
            with open(self.path, encoding="utf-8", errors="replace") as f:
                lines = f.readlines()[-self.cap:]
        except Exception:
            return
        parsed = []
        for line in lines:
            e = self._parse(line)
            if e:
                self._seq += 1
                e["seq"] = self._seq
                parsed.append(e)
        self.entries = parsed

    def _parse(self, line):
        m = _LINE_RE.match(line.rstrip("\n"))
        if not m:
            return None
        stamp, level, msg = m.group(1), (m.group(2) or "").lower(), m.group(3)
        if level not in LOG_LEVELS:
            level = guess_level(msg)
        return {"date": stamp[:10], "t": stamp[11:], "stamp": stamp,
                "level": level, "msg": msg}

    @staticmethod
    def render(e):
        return "%s  [%s]  %s" % (e["stamp"], LEVEL_TAG.get(e["level"], "INFO"), e["msg"])

    def add(self, msg, level="info"):
        if level not in LOG_LEVELS:
            level = "info"
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = {"date": stamp[:10], "t": stamp[11:], "stamp": stamp,
                 "level": level, "msg": msg}
        line = self.render(entry)
        rewrite = None
        with self.lock:
            self._seq += 1
            entry["seq"] = self._seq
            self.entries.append(entry)
            if len(self.entries) > self.cap:
                del self.entries[:len(self.entries) - self.cap]
            self._bytes += len(line.encode("utf-8")) + 1
            if self._bytes > self.max_bytes:
                rewrite = list(self.entries[-400:])
                self._bytes = sum(len(self.render(e).encode("utf-8")) + 1 for e in rewrite)
        try:
            if rewrite is not None:
                with open(self.path, "w", encoding="utf-8") as f:
                    for e in rewrite:
                        f.write(self.render(e) + "\n")
            else:
                with open(self.path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except Exception:
            pass

    def tail(self, n=200, level=None, q=None):
        items = self._filter(level, q)
        return [dict(e) for e in items[-n:]] if n > 0 else []

    def _filter(self, level=None, q=None):
        with self.lock:
            items = list(self.entries)
        if level:
            items = [e for e in items if e["level"] == level]
        if q:
            needle = q.lower()
            items = [e for e in items
                     if needle in e["msg"].lower() or needle in e["t"]]
        return items

    def since(self, seq, limit=1000):
        """增量：返回 seq 之后最新的 limit 条（保证游标只前进）。"""
        with self.lock:
            items = [e for e in self.entries if e["seq"] > seq]
        return [dict(e) for e in items[-limit:]] if limit > 0 else []

    def counts(self):
        with self.lock:
            by = dict((k, 0) for k in LOG_LEVELS)
            for e in self.entries:
                by[e["level"]] = by.get(e["level"], 0) + 1
            total = len(self.entries)
            last = self._seq
        return {"total": total, "byLevel": by, "lastSeq": last}

    def export_text(self):
        with self.lock:
            return "\n".join(self.render(e) for e in self.entries)

    def clear(self):
        with self.lock:
            self.entries = []
        try:
            open(self.path, "w").close()
            self._bytes = 0
        except Exception:
            pass


class NutClient:
    """最小可用的 NUT upsd 客户端：登录、拉变量、接 FSD。"""

    def __init__(self, state, log):
        self.state = state
        self.log = log
        self.sock = None
        self.f = None
        self.creds = None

    def _send(self, line):
        self.sock.sendall((line + "\n").encode())

    def _readline(self, timeout):
        self.sock.settimeout(timeout)
        data = b""
        while not data.endswith(b"\n"):
            chunk = self.sock.recv(1)
            if not chunk:
                raise ConnectionError("upstream closed")
            data += chunk
            if len(data) > 8192:
                raise ConnectionError("line too long")
        return data.decode("utf-8", "replace").strip()

    def _expect_ok(self, cmd, timeout=5):
        self._send(cmd)
        while True:
            resp = self._readline(timeout)
            if resp.startswith("OK"):
                return True
            if resp.startswith("ERR"):
                raise RuntimeError(cmd.split(" ")[0] + " -> " + resp)

    def connect(self, up):
        self.close()
        host = (up.get("host") or "").strip()
        upsname = (up.get("upsname") or "").strip()
        if not host:
            raise RuntimeError("上游地址未填写（请在界面里填写 NUT Server 的 IP 或主机名）")
        if not upsname:
            raise RuntimeError("UPS 名称未填写（绿联/群晖通常是 ups0 或 ups）")
        s = socket.create_connection((host, int(up["port"])), timeout=5)
        self.sock = s
        self.creds = (up.get("username", ""), up.get("password", ""))
        if self.creds[0]:
            self._expect_ok("USERNAME " + self.creds[0])
            self._expect_ok("PASSWORD " + self.creds[1])
        self._expect_ok("LOGIN " + upsname)

    def close(self):
        try:
            if self.sock:
                self.sock.close()
        except Exception:
            pass
        self.sock = None

    def poll(self, up, timeout=5):
        if not self.sock:
            self.connect(up)
        self._send("LIST VAR " + (up.get("upsname") or "").strip())
        variables = {}
        fsd = False
        deadline = time.time() + timeout
        while True:
            left = max(0.1, deadline - time.time())
            line = self._readline(left)
            if line == "FSD":
                fsd = True
                continue
            if line.startswith("VAR "):
                m = re.match(r'^VAR\s+\S+\s+(\S+)\s+"(.*)"$', line)
                if m:
                    variables[m.group(1)] = m.group(2)
                continue
            if line.startswith("END LIST VAR"):
                break
            if line.startswith("ERR"):
                raise RuntimeError(line)
        return variables, fsd


# ups.status 里只表示"正在充电"的标记。APC Back-UPS 接市电时会 OL <-> OL CHRG
# 反复跳（实测 60 秒内切换 26 次），若把这种抖动也记一行，日志会被刷满，
# 真正需要复盘的 RB / OB / LB / OVER 反而被淹没。所以变化检测忽略 CHRG，
# 但记录下来的原始状态仍然完整。
CHATTER_TOKENS = {"CHRG"}


def status_signature(status):
    return " ".join(sorted(t for t in status.split() if t not in CHATTER_TOKENS))


def note_status_change(status, st, log):
    """只在 ups.status 发生「有意义」变化时记一行。"""
    sig = status_signature(status)
    if sig == st.get("status_sig"):
        return
    old = st.get("status_sig")
    st["status_sig"] = sig
    if old is None:
        log.add("UPS 状态：" + (status or "(空)"))
    else:
        log.add("UPS 状态变化：" + (old or "(空)") + " → " + (status or "(空)"))
    if "RB" in status.split() and not st.get("rb_warned"):
        st["rb_warned"] = True
        log.add("上游报告 RB（建议更换电池）：本机不据此关机，仅记录", "warn")


class Monitor(threading.Thread):
    def __init__(self, ctx):
        super().__init__(daemon=True)
        self.ctx = ctx
        self.client = NutClient(ctx["state"], ctx["log"])

    def run(self):
        ctx = self.ctx
        st = ctx["state"]
        log = ctx["log"]
        while not ctx["stop"].is_set():
            cfg = ctx["config"]
            up = cfg["upstream"]
            rules = cfg["rules"]
            interval = max(1, int(rules.get("poll_interval", 5)))
            try:
                variables, fsd = self.client.poll(up)
                st["failures"] = 0
                if st.get("stale"):
                    log.add("与上游恢复通信 " + (up.get("host") or ""), "ok")
                st["stale"] = False
                self.update_state(variables, fsd, cfg, st, log)
            except Exception as e:
                st["failures"] = st.get("failures", 0) + 1
                self.client.close()
                deadtime = max(1, int(rules.get("deadtime", 30)))
                st["last_error"] = str(e)
                if st["failures"] * interval >= deadtime and not st.get("stale"):
                    st["stale"] = True
                    log.add("与上游失联超过 %ds：%s（不触发关机）" % (deadtime, e), "warn")
                time.sleep(min(interval, 3))
                continue
            time.sleep(interval)

    def confirm_low_battery(self, raw, on_batt, charge, runtime,
                            charge_low_up, runtime_low, rules, st, log, now):
        """把上游的 LB 标记变成"可信的低电量"。

        2026-11-18 03:16 误关机事故后新增。上游（绿联 US3000）固件会在市电正常时
        瞬时置 LB 位，与 RB 抖动同源：那一刻 battery.charge=100%、battery.runtime=2403s、
        battery.charge.low=15，数值上根本不是低电量，但 1.0.1 见到 LB 就立刻关机。

        三道闸（与 FSD 同级的防护）：
          1. 必须在电池供电（on_batt）。市电正常时的 LB 一律不认。
          2. 若上游给出了电量 / 续航数值，必须至少有一个真的低于阈值；
             数值全部不支持时，判为瞬时抖动，不认。
          3. 认了之后还要持续 lb_grace_seconds 秒（默认 30s）才动作。
        任一道闸挡下都会在日志里写清原因，且只在原因变化时写一次，不刷屏。
        """
        grace = float(rules.get("lb_grace_seconds", 30) or 0)

        if not raw:
            if st.get("lb_since"):
                log.add("低电量标记消失，取消确认（已持续 %ds，未达动作条件）"
                        % int(now - st["lb_since"]), "ok")
            st["lb_since"] = None
            st["lb_note"] = ""
            st["lb_block"] = ""
            return False

        note, block = "上游报低电量", ""

        # 闸 1：不在电池供电时的 LB 不可信
        if not on_batt:
            block = "但市电正常（当前不在电池供电）"

        # 闸 2：数值核对
        if not block:
            signals, detail = [], []
            if charge is not None and charge_low_up is not None:
                signals.append(charge <= charge_low_up)
                detail.append("电量 %s%%（阈值 %s%%）"
                              % (fmt_num(charge), fmt_num(charge_low_up)))
            if runtime is not None and runtime_low is not None:
                signals.append(runtime <= runtime_low)
                detail.append("续航 %ss（阈值 %ss）"
                              % (fmt_num(runtime), fmt_num(runtime_low)))
            if signals and not any(signals):
                block = "但数值不支持（" + "、".join(detail) + "）"

        if block:
            st["lb_since"] = None
            st["lb_note"] = ""
            if st.get("lb_block") != block:
                st["lb_block"] = block
                log.add("上游报低电量，" + block + "，判为瞬时抖动，不动作", "warn")
            return False

        st["lb_block"] = ""
        if not st.get("lb_since"):
            st["lb_since"] = now
            log.add("上游报低电量，开始确认（需持续 %.0fs）" % grace, "warn")

        if (now - st["lb_since"]) < grace:
            st["lb_note"] = "确认中（%ds / %.0fs）" % (int(now - st["lb_since"]), grace)
            return False

        st["lb_note"] = "已持续 %ds" % int(now - st["lb_since"])
        return True

    def update_state(self, v, fsd, cfg, st, log):
        rules = cfg["rules"]
        status = v.get("ups.status", "").upper()
        tokens = set(status.split())
        note_status_change(status, st, log)
        if "FSD" in tokens:
            fsd = True
        def num(key):
            try:
                return float(v.get(key, ""))
            except Exception:
                return None

        charge = num("battery.charge")
        runtime = num("battery.runtime")
        charge_low_up = num("battery.charge.low")
        runtime_low = num("battery.runtime.low")
        low_override = float(rules.get("battery_charge_low_override") or 0)
        now = time.time()

        st["ups"] = {
            "status": status,
            "charge": charge,
            "runtime": v.get("battery.runtime"),
            "model": (v.get("device.mfr", "") + " " + v.get("device.model", "")).strip(),
            "battery_date": v.get("battery.date", ""),
        }
        st["last_poll"] = now

        # token 精确匹配：子串匹配会被 "OL RB" 之类的组合状态误命中
        on_batt = "OB" in tokens
        lb_raw = "LB" in tokens
        if low_override > 0 and charge is not None and charge <= low_override:
            lb_raw = True

        # FSD 加固：上游可能残留陈旧的 FSD 标记（NUT 无复位命令，只能重启 upsd 清）。
        # 因此要求「本次运行期间曾观测到市电中断」，才承认 FSD 是本次断电的强制关机信号。
        if fsd and not st.get("seen_ob"):
            if not st.get("fsd_orphan_logged"):
                st["fsd_orphan_logged"] = True
                log.add("上游有 FSD 标记，但本次运行未观测到断电，视为陈旧标记，不动作", "warn")
            fsd = False

        # FSD 需持续一段时间才认
        if fsd:
            if not st.get("fsd_since"):
                st["fsd_since"] = now
                log.add("上游出现 FSD 标记，开始确认", "warn")
        else:
            st["fsd_since"] = None
        fsd_grace = float(rules.get("fsd_grace_seconds", 15) or 0)
        if fsd and st.get("fsd_since") and (now - st["fsd_since"]) < fsd_grace:
            if not st.get("fsd_warned"):
                st["fsd_warned"] = True
                log.add("上游有 FSD 标记，需持续 %.0fs 才动作（防上游残留状态误触发）" % fsd_grace, "warn")
            fsd = False
        elif fsd:
            st["fsd_warned"] = False

        # LB 加固：上游会瞬时置 LB 位，必须过三级确认才算数。
        # 关掉低电量规则时仍然调用一次，但强制 raw=False，让确认计时被清理干净。
        lb = self.confirm_low_battery(
            lb_raw and rules.get("shutdown_on_lowbattery", True),
            on_batt, charge, runtime, charge_low_up, runtime_low,
            rules, st, log, now)
        if lb_raw and not rules.get("shutdown_on_lowbattery", True):
            st["lb_note"] = "低电量关机规则已关闭"

        if st.get("shutdown_issued"):
            # 已触发过，保持终态，不再翻回 ONLINE/ONBATTERY
            return

        if not on_batt and not lb and not fsd:
            if st.get("ob_since"):
                log.add("市电恢复，取消计时（本次断电 %ds）" % int(now - st["ob_since"]), "ok")
            st["ob_since"] = None
            st["mode"] = "ONLINE"
            st["reason"] = ""
            return

        if on_batt and not st.get("ob_since"):
            st["ob_since"] = now
            st["seen_ob"] = True
            log.add("检测到市电中断，开始计时（阈值 %ds）" % int(rules.get("shutdown_on_battery_seconds", 0)), "warn")

        elapsed = int(now - st["ob_since"]) if st.get("ob_since") else 0
        reason = ""
        if fsd and rules.get("shutdown_on_fsd", True):
            reason = "上游广播 FSD（强制关机）"
        elif lb:
            reason = "UPS 报低电量（已确认 %ds）" % int(now - st["lb_since"])
        else:
            thr = int(rules.get("shutdown_on_battery_seconds", 0) or 0)
            if on_batt and thr > 0 and elapsed >= thr:
                reason = "断电已持续 %ds（阈值 %ds）" % (elapsed, thr)

        st["mode"] = "ONBATTERY" if on_batt else ("LOWBATT" if (lb or fsd) else st.get("mode", "ONLINE"))
        if not reason:
            st["reason"] = ""
            return
        self.trigger_shutdown(reason, cfg, st, log)

    def trigger_shutdown(self, reason, cfg, st, log):
        rules = cfg["rules"]
        st["shutdown_issued"] = True
        st["shutdown_reason"] = reason
        st["shutdown_at"] = time.time()
        if rules.get("dry_run", True):
            st["mode"] = "DRY_RUN"
            log.add("【演练模式】触发条件满足：" + reason + " —— 本应执行关机，未执行", "warn")
            return
        cmd = rules.get("shutdown_cmd", "/sbin/shutdown -h +0")
        st["mode"] = "SHUTTING_DOWN"
        log.add("触发关机：" + reason + " —— 执行 " + cmd, "error")
        try:
            subprocess.Popen(cmd, shell=True)
        except Exception as e:
            log.add("关机命令执行失败：" + str(e), "error")


class Ctx(dict):
    """同时支持 ctx["state"] 与 ctx.state 两种访问。"""

    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)

    def __setattr__(self, k, v):
        self[k] = v


def make_handler(ctx):
    class Handler(BaseHTTPRequestHandler):
        server_version = "upsbridge/" + VERSION

        def log_message(self, fmt, *args):
            pass

        def _json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _text(self, s, code=200, ctype="text/html; charset=utf-8"):
            body = s.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parsed = urlparse(self.path)
            p = parsed.path
            qs = parse_qs(parsed.query)
            if p in ("/", "/index.html"):
                return self._text(PAGE)
            if p in ("/log", "/log/", "/log.html"):
                return self._text(LOG_PAGE)
            if p == "/api/health":
                return self._json({"ok": True, "version": VERSION})
            if p == "/api/status":
                st = ctx.state
                return self._json({
                    "ups": st.get("ups") or {},
                    "mode": st.get("mode", "INIT"),
                    "reason": st.get("reason", ""),
                    "on_battery_seconds": int(time.time() - st["ob_since"]) if st.get("ob_since") else 0,
                    "stale": bool(st.get("stale")),
                    "last_error": st.get("last_error", ""),
                    "shutdown_issued": bool(st.get("shutdown_issued")),
                    "shutdown_reason": st.get("shutdown_reason", ""),
                    "lb_note": st.get("lb_note", ""),
                    "lb_confirm_seconds": int(time.time() - st["lb_since"]) if st.get("lb_since") else 0,
                })
            if p == "/api/config":
                return self._json(ctx.config)
            if p == "/api/log":
                # 兼容老页面：lines 是纯文本，entries 是结构化数据
                limit = int((qs.get("limit") or ["200"])[0] or 200)
                level = (qs.get("level") or [""])[0] or None
                q = (qs.get("q") or [""])[0] or None
                items = ctx.log.tail(limit, level, q)
                return self._json({"lines": [ctx.log.render(e) for e in items],
                                   "entries": items, "counts": ctx.log.counts()})
            if p == "/api/logs":
                seq = int((qs.get("seq") or ["0"])[0] or 0)
                limit = int((qs.get("limit") or ["600"])[0] or 600)
                items = ctx.log.since(seq, limit)
                return self._json({"entries": items, "counts": ctx.log.counts(),
                                   "server_time": time.strftime("%Y-%m-%d %H:%M:%S")})
            if p == "/api/logs/export":
                body = ctx.log.export_text().encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition",
                                 'attachment; filename="upsbridge-%s.log"'
                                 % time.strftime("%Y%m%d-%H%M%S"))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                return self.wfile.write(body)
            return self._text("not found", 404, "text/plain")

        def do_POST(self):
            p = self.path.split("?")[0]
            n = int(self.headers.get("Content-Length", "0") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                return self._json({"ok": False, "error": "JSON 解析失败"}, 400)

            if p == "/api/test":
                up = deep_merge(ctx.config["upstream"], data.get("upstream") or {})
                c = NutClient({}, ctx.log)
                try:
                    c.connect(up)
                    variables, fsd = c.poll(up)
                    c.close()
                    return self._json({"ok": True, "vars": {
                        "status": variables.get("ups.status", ""),
                        "charge": variables.get("battery.charge", ""),
                        "mfr": variables.get("device.mfr", ""),
                        "model": variables.get("device.model", ""),
                        "runtime": variables.get("battery.runtime", ""),
                    }, "count": len(variables)})
                except Exception as e:
                    c.close()
                    return self._json({"ok": False, "error": str(e)})

            if p == "/api/config":
                new = deep_merge(ctx.config, data)
                ctx.config.clear()
                ctx.config.update(new)
                try:
                    with open(ctx.conf_path, "w", encoding="utf-8") as f:
                        json.dump(ctx.config, f, ensure_ascii=False, indent=2)
                except Exception as e:
                    return self._json({"ok": False, "error": "写入失败：" + str(e)}, 500)
                ctx.log.add("配置已保存", "ok")
                return self._json({"ok": True})

            if p == "/api/logs/clear":
                n = len(ctx.log.entries)
                ctx.log.clear()
                ctx.log.add("运行日志已清空（清掉 %d 条）" % n, "warn")
                return self._json({"ok": True, "cleared": n})

            if p == "/api/reset_shutdown":
                ctx.state["shutdown_issued"] = False
                ctx.state["shutdown_reason"] = ""
                ctx.state["mode"] = "INIT"
                ctx.state["ob_since"] = None
                ctx.log.add("手动复位已触发标记（用于再次演练）", "ok")
                return self._json({"ok": True})

            return self._json({"ok": False, "error": "unknown"}, 404)

    return Handler


LOG_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>运行日志 · UPS 断电联动</title>
<style>
  :root { --bg:#0e1116; --card:#161b23; --card2:#1b222c; --line:#242c38; --fg:#e6edf3; --dim:#8b98a9;
          --ok:#3fb950; --warn:#d29922; --bad:#f85149; --acc:#4c8dff;
          --info-c:#5b9bff; --ok-c:#3fb950; --warn-c:#d29922; --error-c:#f85149; }
  * { box-sizing:border-box; }
  html,body { height:100%; }
  body { margin:0; background:var(--bg); color:var(--fg);
         font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif;
         display:flex; flex-direction:column; }
  a { color:var(--acc); text-decoration:none; }
  button { font:inherit; cursor:pointer; }

  .top { display:flex; align-items:center; gap:14px; padding:14px 22px;
         border-bottom:1px solid var(--line); background:rgba(14,17,22,.92);
         position:sticky; top:0; z-index:20; backdrop-filter:blur(8px); }
  .back { display:inline-flex; align-items:center; gap:6px; color:var(--dim); font-size:13px;
          padding:6px 10px; border:1px solid var(--line); border-radius:8px; }
  .back:hover { color:var(--fg); border-color:#33405a; }
  .title { font-size:16px; font-weight:600; }
  .title small { color:var(--dim); font-weight:400; font-size:12px; margin-left:8px; }
  .live { margin-left:auto; display:flex; align-items:center; gap:8px; color:var(--dim); font-size:12px; }
  .dot { width:7px; height:7px; border-radius:50%; background:var(--ok); box-shadow:0 0 0 3px rgba(63,185,80,.15); }
  .dot.off { background:var(--bad); box-shadow:0 0 0 3px rgba(248,81,73,.15); }

  .strip { display:grid; grid-template-columns:repeat(5,minmax(0,1fr)); gap:1px;
           background:var(--line); border-bottom:1px solid var(--line); }
  .strip .cell { background:var(--bg); padding:12px 22px; min-width:0; }
  .strip .k { color:var(--dim); font-size:11px; letter-spacing:.03em; }
  .strip .v { font-size:15px; font-weight:600; margin-top:2px; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .badge { display:inline-block; padding:2px 9px; border-radius:20px; font-size:11px; font-weight:600; }
  .b-ok{background:rgba(63,185,80,.15);color:var(--ok)}
  .b-warn{background:rgba(210,153,34,.16);color:var(--warn)}
  .b-bad{background:rgba(248,81,73,.15);color:var(--bad)}
  .b-dim{background:rgba(139,152,169,.15);color:var(--dim)}

  .tools { display:flex; align-items:center; gap:10px; flex-wrap:wrap;
           padding:12px 22px; border-bottom:1px solid var(--line); background:var(--bg); }
  .search { position:relative; flex:1 1 240px; max-width:380px; }
  .search input { width:100%; padding:8px 30px 8px 32px; background:var(--card); color:var(--fg);
                  border:1px solid var(--line); border-radius:8px; font:inherit; font-size:13px; }
  .search input:focus { outline:none; border-color:#3a5486; box-shadow:0 0 0 3px rgba(76,141,255,.12); }
  .search .ic { position:absolute; left:10px; top:50%; transform:translateY(-50%); color:var(--dim); font-size:12px; }
  .search .kbd { position:absolute; right:8px; top:50%; transform:translateY(-50%); color:var(--dim);
                 font-size:11px; border:1px solid var(--line); border-radius:4px; padding:0 5px; }
  .chips { display:flex; gap:6px; flex-wrap:wrap; }
  .chip { background:transparent; border:1px solid var(--line); color:var(--dim);
          border-radius:20px; padding:5px 12px; font-size:12px; display:inline-flex; align-items:center; gap:6px; }
  .chip .n { font-variant-numeric:tabular-nums; color:#6d7a8c; }
  .chip:hover { color:var(--fg); border-color:#33405a; }
  .chip.on { color:var(--fg); background:var(--card2); border-color:#3a5486; }
  .chip .sw { width:7px; height:7px; border-radius:50%; }
  .spacer { flex:1; }
  .acts { display:flex; gap:10px; flex-wrap:wrap; margin-left:auto; }
  .btn { background:transparent; border:1px solid var(--line); color:var(--fg);
         border-radius:8px; padding:7px 13px; font-size:13px; }
  .btn:hover { border-color:#33405a; background:var(--card2); }
  .btn.on { border-color:#3a5486; color:#cfe0ff; background:rgba(76,141,255,.1); }
  .btn.danger:hover { border-color:rgba(248,81,73,.5); color:var(--bad); }

  .logwrap { flex:1; min-height:0; display:flex; }
  .logcol { flex:1; min-width:0; overflow:auto; padding:0 22px 40px; }
  .day { position:sticky; top:0; z-index:5; display:flex; align-items:center; gap:10px;
         padding:14px 0 8px; background:var(--bg); box-shadow:0 10px 14px -6px var(--bg);
         color:var(--dim); font-size:12px; letter-spacing:.04em; }
  .day::after { content:""; flex:1; height:1px; background:var(--line); }
  .day .cnt { color:#5f6c7e; }

  .row { display:grid; grid-template-columns:86px 16px 1fr; gap:0 12px; align-items:flex-start;
         padding:5px 8px 5px 0; border-radius:8px; }
  .row:hover { background:rgba(255,255,255,.025); }
  .row.sel { background:rgba(76,141,255,.08); }
  .row .t { color:#6d7a8c; font-size:12px; font-variant-numeric:tabular-nums;
            font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace; padding-top:2px; }
  .row .t .gap { display:block; color:#6d7a8c; font-size:11px; margin-top:1px; opacity:.85; }
  .rail { position:relative; align-self:stretch; }
  .rail::before { content:""; position:absolute; left:50%; top:-5px; bottom:-5px; width:1px;
                  background:var(--line); transform:translateX(-.5px); }
  .node { position:absolute; left:50%; top:7px; width:9px; height:9px; border-radius:50%;
          transform:translate(-50%,0); background:#2a3442; border:2px solid var(--bg); }
  .row.lv-info .node { background:var(--info-c); }
  .row.lv-ok .node { background:var(--ok-c); }
  .row.lv-warn .node { background:var(--warn-c); box-shadow:0 0 0 3px rgba(210,153,34,.12); }
  .row.lv-error .node { background:var(--error-c); box-shadow:0 0 0 3px rgba(248,81,73,.15); }
  .body { min-width:0; display:flex; gap:9px; align-items:baseline; flex-wrap:wrap; }
  .lv { flex:0 0 auto; font-size:11px; font-weight:700; letter-spacing:.04em;
        padding:1px 6px; border-radius:4px; transform:translateY(-1px); }
  .lv.lv-info { color:#8fb8ff; background:rgba(91,155,255,.12); }
  .lv.lv-ok { color:var(--ok); background:rgba(63,185,80,.12); }
  .lv.lv-warn { color:var(--warn); background:rgba(210,153,34,.13); }
  .lv.lv-error { color:var(--bad); background:rgba(248,81,73,.13); }
  .msg { flex:1 1 320px; min-width:0; word-break:break-word; }
  .row.lv-error .msg { color:#ffb4ae; }
  .row.lv-warn .msg { color:#f0d69a; }
  .row.lv-ok .msg { color:#c9ebd0; }
  .msg mark { background:rgba(76,141,255,.28); color:#dce9ff; border-radius:3px; padding:0 1px; }
  .row.clip .msg { display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; overflow:hidden; }

  .empty { color:var(--dim); text-align:center; padding:80px 0; font-size:13px; }
  .empty b { display:block; color:#5f6c7e; font-weight:500; margin-top:6px; font-size:12px; }

  .foot { display:flex; align-items:center; gap:14px; padding:8px 22px;
          border-top:1px solid var(--line); color:var(--dim); font-size:12px; background:var(--bg); }
  .foot .kv { display:inline-flex; gap:5px; }
  .foot b { color:#a9b6c6; font-weight:600; font-variant-numeric:tabular-nums; }
  .jump { margin-left:auto; display:none; }
  .jump.show { display:inline-block; }

  .detail { width:330px; border-left:1px solid var(--line); overflow:auto; padding:16px 18px;
            background:var(--card); display:none; }
  .detail.show { display:block; }
  .detail h3 { margin:0 0 10px; font-size:12px; color:var(--dim); font-weight:500; letter-spacing:.06em; }
  .detail .dtime { font-family:ui-monospace,Menlo,Consolas,monospace; font-size:13px; }
  .detail .dmsg { margin-top:12px; line-height:1.7; word-break:break-word; }
  .detail .dmeta { margin-top:16px; display:grid; gap:6px; font-size:12px; color:var(--dim); }
  .detail .dmeta span { color:#a9b6c6; }
  .detail .btn { width:100%; margin-top:14px; text-align:center; }

  @media (max-width:900px) {
    .strip { grid-template-columns:repeat(2,minmax(0,1fr)); }
    .strip .cell { padding:10px 14px; }
    .detail { display:none !important; }
    .logcol { padding:6px 14px 40px; }
    .tools,.top,.foot { padding-left:14px; padding-right:14px; }
    .search { flex:1 1 100%; max-width:none; }
    .chips { flex-wrap:nowrap; overflow-x:auto; scrollbar-width:none; padding-bottom:1px;
             -webkit-mask-image:linear-gradient(90deg,#000 88%,transparent);
             mask-image:linear-gradient(90deg,#000 88%,transparent); }
    .chips::-webkit-scrollbar { display:none; }
    .chip { flex:0 0 auto; }
    .spacer { display:none; }
    .acts { flex:1 1 100%; margin-left:0; justify-content:flex-end; }
    .row { grid-template-columns:62px 14px minmax(0,1fr); gap:0 8px; }
    .body { flex-wrap:nowrap; gap:7px; }
    .msg { flex:1 1 0; }
    .live span:last-child { display:none; }
  }
</style>
</head>
<body>
  <div class="top">
    <a class="back" href="/">‹ 返回</a>
    <div class="title">运行日志<small id="verline">UPS 断电联动</small></div>
    <div class="live"><span class="dot" id="liveDot"></span><span id="liveTxt">连接中…</span></div>
  </div>

  <div class="strip">
    <div class="cell"><div class="k">当前状态</div><div class="v" id="sMode"><span class="badge b-dim">读取中…</span></div></div>
    <div class="cell"><div class="k">UPS 状态</div><div class="v" id="sUps">—</div></div>
    <div class="cell"><div class="k">电量</div><div class="v" id="sCharge">—</div></div>
    <div class="cell"><div class="k">断电计时</div><div class="v" id="sTimer">—</div></div>
    <div class="cell"><div class="k">设备 / 最近错误</div><div class="v" id="sExtra" style="font-size:12px;font-weight:400;color:#8b98a9">—</div></div>
  </div>

  <div class="tools">
    <div class="search">
      <span class="ic">⌕</span>
      <input id="q" type="text" placeholder="搜索日志内容或时间" autocomplete="off">
      <span class="kbd">/</span>
    </div>
    <div class="chips" id="chips"></div>
    <div class="acts">
      <button class="btn on" id="btnAuto">自动滚动</button>
      <a class="btn" href="/api/logs/export" download>导出</a>
      <button class="btn danger" id="btnClear">清空</button>
    </div>
  </div>

  <div class="logwrap">
    <div class="logcol" id="logcol"></div>
    <aside class="detail" id="detail"></aside>
  </div>

  <div class="foot">
    <span class="kv">本页显示 <b id="fShown">0</b> 条</span>
    <span class="kv">内存共 <b id="fTotal">0</b> 条</span>
    <span class="kv">最近更新 <b id="fTime">—</b></span>
    <button class="btn jump" id="btnJump">有新日志 ↓</button>
  </div>

<script>
var LEVELS = [
  {id:'info',  label:'信息', color:'var(--info-c)'},
  {id:'ok',    label:'正常', color:'var(--ok-c)'},
  {id:'warn',  label:'注意', color:'var(--warn-c)'},
  {id:'error', label:'严重', color:'var(--error-c)'}
];
var LV = {};
LEVELS.forEach(function(l){ LV[l.id] = l; });

var state = { entries: [], lastSeq: 0, level: '', q: '', auto: true, sel: null, counts: null };
var col = document.getElementById('logcol');
var detail = document.getElementById('detail');

function q(id){ return document.getElementById(id); }
function esc(s){ return String(s).replace(/[&<>"]/g, function(c){
  return ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'})[c]; }); }

function dayLabel(d){
  var t = new Date();
  var p = function(x){ return (x<10?'0':'')+x; };
  var today = t.getFullYear()+'-'+p(t.getMonth()+1)+'-'+p(t.getDate());
  var y = new Date(t.getTime()-86400000);
  var yest = y.getFullYear()+'-'+p(y.getMonth()+1)+'-'+p(y.getDate());
  if (d === today) return '今天 · ' + d;
  if (d === yest) return '昨天 · ' + d;
  return d;
}

function highlight(text){
  var s = esc(text);
  if (!state.q) return s;
  var needle = state.q.replace(/[.*+?^$()|[\]\\]/g, '\\$&');
  try { return s.replace(new RegExp(needle,'gi'), function(m){ return '<mark>'+m+'</mark>'; }); }
  catch(e){ return s; }
}

function visible(){
  var n = state.q.toLowerCase();
  return state.entries.filter(function(e){
    if (state.level && e.level !== state.level) return false;
    if (n && e.msg.toLowerCase().indexOf(n) < 0 && e.t.indexOf(n) < 0) return false;
    return true;
  });
}

function ts(e){
  var m = /(\d{4})-(\d\d)-(\d\d) (\d\d):(\d\d):(\d\d)/.exec(e.stamp || '');
  if (!m) return NaN;
  return new Date(+m[1], +m[2]-1, +m[3], +m[4], +m[5], +m[6]).getTime();
}
function gapText(ms){
  if (!isFinite(ms) || ms < 1000) return '';
  var s = Math.round(ms/1000);
  if (s < 60) return '+' + s + 's';
  if (s < 3600) return '+' + Math.floor(s/60) + 'm';
  if (s < 86400) return '+' + Math.floor(s/3600) + 'h';
  return '+' + Math.floor(s/86400) + 'd';
}

function render(){
  var list = visible();
  var dayCount = {};
  list.forEach(function(e){ dayCount[e.date] = (dayCount[e.date]||0)+1; });
  var html = '';
  var lastDay = null;
  var prevTs = NaN;
  list.forEach(function(e){
    if (e.date !== lastDay) {
      lastDay = e.date;
      html += '<div class="day"><span>'+esc(dayLabel(e.date))+'</span><span class="cnt">'+dayCount[e.date]+' 条</span></div>';
      prevTs = NaN;
    }
    var lv = LV[e.level] || LV.info;
    var longmsg = e.msg.length > 120;
    var now = ts(e);
    var gap = gapText(now - prevTs);
    prevTs = now;
    html += '<div class="row lv-'+lv.id + (longmsg?' clip':'') + (state.sel===e.seq?' sel':'')+'" data-seq="'+e.seq+'">'
         +  '<div class="t">'+esc(e.t)+(gap?'<span class="gap">'+gap+'</span>':'')+'</div>'
         +  '<div class="rail"><span class="node"></span></div>'
         +  '<div class="body"><span class="lv lv-'+lv.id+'">'+lv.label+'</span>'
         +  '<span class="msg">'+highlight(e.msg)+'</span></div>'
         +  '</div>';
  });
  col.innerHTML = html || '<div class="empty">没有匹配的日志<b>换个关键词，或把级别筛选切回「全部」</b></div>';
  q('fShown').textContent = list.length;
  q('fTotal').textContent = state.counts ? state.counts.total : state.entries.length;
  updateChips();
}

function updateChips(){
  var by = (state.counts && state.counts.byLevel) || {};
  var total = state.counts ? state.counts.total : state.entries.length;
  var html = '<button class="chip'+(state.level===''?' on':'')+'" data-lv="">全部 <span class="n">'+total+'</span></button>';
  LEVELS.forEach(function(l){
    html += '<button class="chip'+(state.level===l.id?' on':'')+'" data-lv="'+l.id+'">'
          + '<span class="sw" style="background:'+l.color+'"></span>'+l.label
          + ' <span class="n">'+(by[l.id]||0)+'</span></button>';
  });
  q('chips').innerHTML = html;
}

function atBottom(){ return col.scrollTop + col.clientHeight >= col.scrollHeight - 40; }
function toBottom(){ col.scrollTop = col.scrollHeight; q('btnJump').classList.remove('show'); }

function merge(d){
  var last = d.counts && d.counts.lastSeq;
  if (last != null && last < state.lastSeq) {
    // 服务重启后 seq 从头计数，本地游标作废，整页重新载入
    state.entries = []; state.lastSeq = 0; state.sel = null;
    detail.classList.remove('show');
    return poll();
  }
  if (!d.entries || !d.entries.length) { state.counts = d.counts || state.counts; updateChips(); return; }
  var known = {};
  state.entries.forEach(function(e){ known[e.seq] = 1; });
  d.entries.forEach(function(e){ if (!known[e.seq]) { state.entries.push(e); known[e.seq] = 1; } });
  state.entries.sort(function(a,b){ return a.seq-b.seq; });
  if (state.entries.length > 4000) state.entries = state.entries.slice(-4000);
  state.lastSeq = d.entries[d.entries.length-1].seq;
  state.counts = d.counts || state.counts;
  q('fTime').textContent = (d.server_time||'').slice(11) || '—';
  var stick = state.auto && atBottom();
  render();
  if (stick) toBottom();
  else if (!state.auto) q('btnJump').classList.add('show');
}

function poll(){
  fetch('/api/logs?seq='+state.lastSeq+'&limit=800')
    .then(function(r){ return r.json(); })
    .then(function(d){
      q('liveDot').className = 'dot';
      q('liveTxt').textContent = '自动刷新中 · 每 2 秒';
      merge(d);
    })
    .catch(function(){
      q('liveDot').className = 'dot off';
      q('liveTxt').textContent = '连接中断，正在重试…';
    });
}

function renderStatus(d){
  var names = {ONLINE:'市电正常', ONBATTERY:'电池供电', LOWBATT:'低电量',
    DRY_RUN:'演练触发', SHUTTING_DOWN:'关机中', INIT:'初始化'};
  var m = q('sMode');
  if (d.stale) m.innerHTML = '<span class="badge b-bad">与上游失联</span>';
  else if (d.shutdown_issued) m.innerHTML = '<span class="badge b-warn">已触发关机</span>';
  else if (d.mode === 'ONBATTERY') m.innerHTML = '<span class="badge b-warn">电池供电</span>';
  else if (d.mode === 'LOWBATT') m.innerHTML = '<span class="badge b-warn">低电量</span>';
  else if (d.mode === 'ONLINE') m.innerHTML = '<span class="badge b-ok">市电正常</span>';
  else m.innerHTML = '<span class="badge b-dim">'+esc(names[d.mode]||d.mode||'—')+'</span>';
  var ups = d.ups || {};
  q('sUps').textContent = ups.status || '—';
  q('sCharge').textContent = (ups.charge != null) ? ups.charge + '%' : '—';
  q('sTimer').textContent = d.on_battery_seconds ? d.on_battery_seconds + 's' : '—';
  var extra = [];
  if (ups.model) extra.push(ups.model);
  if (d.last_error) extra.push('错误：' + d.last_error);
  q('sExtra').textContent = extra.join(' · ') || '—';
  q('sExtra').style.color = (d.stale || d.last_error) ? 'var(--warn)' : '#8b98a9';
}

function statusPoll(){
  fetch('/api/status').then(function(r){ return r.json(); }).then(renderStatus).catch(function(){});
}

q('chips').addEventListener('click', function(ev){
  var b = ev.target.closest('.chip'); if (!b) return;
  state.level = b.getAttribute('data-lv') || '';
  render();
});

var qt = null;
q('q').addEventListener('input', function(){
  clearTimeout(qt);
  qt = setTimeout(function(){ state.q = q('q').value.trim(); render(); }, 120);
});

col.addEventListener('click', function(ev){
  var row = ev.target.closest('.row'); if (!row) return;
  var seq = Number(row.getAttribute('data-seq'));
  var e = state.entries.filter(function(x){ return x.seq === seq; })[0];
  if (!e) return;
  state.sel = seq;
  Array.prototype.forEach.call(col.querySelectorAll('.row.sel'), function(r){ r.classList.remove('sel'); });
  row.classList.add('sel');
  row.classList.remove('clip');
  var lv = LV[e.level] || LV.info;
  detail.classList.add('show');
  detail.innerHTML = '<h3>事件详情</h3>'
    + '<div class="dtime">'+esc(e.stamp)+'</div>'
    + '<div style="margin-top:10px"><span class="lv lv-'+lv.id+'">'+lv.label+'</span></div>'
    + '<div class="dmsg">'+highlight(e.msg)+'</div>'
    + '<div class="dmeta"><div>序号 <span>#'+e.seq+'</span></div>'
    + '<div>日期 <span>'+esc(dayLabel(e.date))+'</span></div></div>'
    + '<button class="btn" id="btnCopy">复制这一行</button>';
  q('btnCopy').onclick = function(){
    var line = e.stamp + '  [' + (e.level||'info').toUpperCase() + ']  ' + e.msg;
    if (navigator.clipboard) navigator.clipboard.writeText(line);
    q('btnCopy').textContent = '已复制';
    setTimeout(function(){ q('btnCopy').textContent = '复制这一行'; }, 1200);
  };
});

q('btnAuto').onclick = function(){
  state.auto = !state.auto;
  q('btnAuto').classList.toggle('on', state.auto);
  if (state.auto) toBottom();
};

q('btnJump').onclick = function(){ toBottom(); };
col.addEventListener('scroll', function(){ if (atBottom()) q('btnJump').classList.remove('show'); });

q('btnClear').onclick = function(){
  if (!confirm('清空全部运行日志？此操作不可撤销。')) return;
  fetch('/api/logs/clear', {method:'POST'}).then(function(){
    state.entries = []; state.lastSeq = 0; state.sel = null; state.counts = null;
    detail.classList.remove('show');
    poll(); statusPoll();
  });
};

document.addEventListener('keydown', function(ev){
  if (ev.key === '/' && document.activeElement !== q('q')) { ev.preventDefault(); q('q').focus(); }
  else if (ev.key === 'Escape') { q('q').value=''; state.q=''; render(); q('q').blur(); }
});

fetch('/api/health').then(function(r){ return r.json(); }).then(function(d){
  q('verline').textContent = 'upsbridge ' + d.version;
}).catch(function(){});
statusPoll(); poll();
setInterval(poll, 2000);
setInterval(statusPoll, 2000);
</script>
</body>
</html>
"""


PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>UPS 断电联动</title>
<style>
  :root { --bg:#0e1116; --card:#161b23; --line:#242c38; --fg:#e6edf3; --dim:#8b98a9;
          --ok:#3fb950; --warn:#d29922; --bad:#f85149; --acc:#4c8dff; }
  * { box-sizing:border-box; }
  body { margin:0; padding:20px; background:var(--bg); color:var(--fg);
         font:14px/1.6 -apple-system,"PingFang SC","Microsoft YaHei",sans-serif; }
  .wrap { max-width:860px; margin:0 auto; }
  h1 { font-size:18px; margin:0 0 4px; font-weight:600; }
  .sub { color:var(--dim); font-size:12px; margin-bottom:20px; }
  .card { background:var(--card); border:1px solid var(--line); border-radius:12px;
          padding:18px; margin-bottom:14px; }
  .row { display:flex; gap:14px; flex-wrap:wrap; }
  .stat { flex:1; min-width:130px; }
  .k { color:var(--dim); font-size:12px; }
  .v { font-size:20px; font-weight:600; margin-top:2px; }
  .badge { display:inline-block; padding:3px 10px; border-radius:20px; font-size:12px;
           font-weight:600; }
  .b-ok{background:rgba(63,185,80,.15);color:var(--ok)}
  .b-warn{background:rgba(210,153,34,.15);color:var(--warn)}
  .b-bad{background:rgba(248,81,73,.15);color:var(--bad)}
  .b-dim{background:rgba(139,152,169,.15);color:var(--dim)}
  label { display:block; color:var(--dim); font-size:12px; margin:10px 0 4px; }
  input[type=text],input[type=number],input[type=password] {
    width:100%; padding:9px 11px; background:#0e1116; color:var(--fg);
    border:1px solid var(--line); border-radius:8px; font:inherit; }
  input[type=checkbox]{ width:16px;height:16px;vertical-align:-2px;margin-right:6px; }
  .grid { display:grid; grid-template-columns:1fr 1fr; gap:0 16px; }
  button { background:var(--acc); color:#fff; border:0; border-radius:8px;
           padding:9px 18px; font:inherit; font-weight:600; cursor:pointer; }
  button.ghost { background:transparent; border:1px solid var(--line); color:var(--fg); }
  a.btn { display:inline-block; text-decoration:none; background:transparent;
          border:1px solid var(--line); color:var(--fg); border-radius:8px; padding:9px 16px;
          font-weight:600; }
  a.btn:hover { border-color:#33405a; }
  button:disabled { opacity:.5; cursor:default; }
  pre { background:#0b0e13; border:1px solid var(--line); border-radius:8px; padding:12px;
        max-height:260px; overflow:auto; font-size:12px; line-height:1.5; color:var(--dim); }
  .hint { color:var(--dim); font-size:12px; margin-top:6px; }
  .peek { display:flex; align-items:center; gap:8px; min-width:0; }
  .peek .pd { flex:0 0 auto; width:7px; height:7px; border-radius:50%; background:#3a4453; }
  .peek .pd.info { background:#6c9dff; } .peek .pd.ok { background:var(--ok); }
  .peek .pd.warn { background:var(--warn); } .peek .pd.error { background:var(--bad); }
  .peek .pt { flex:1; min-width:0; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;
              font-variant-numeric:tabular-nums; }
  .peek .pt b { color:#a9b6c6; font-weight:400; margin-right:6px;
                font-family:ui-monospace,Menlo,Consolas,monospace; }
  .warnbox { border-color:rgba(210,153,34,.4); background:rgba(210,153,34,.08); }
  .msg { margin-top:10px; font-size:13px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>UPS 断电联动</h1>
  <div class="sub">接入局域网内任意 NUT Server · 断电后按规则自动关机 · 不修改 /etc/nut</div>

  <div class="card" id="statusCard">
    <div class="row">
      <div class="stat"><div class="k">状态</div><div class="v" id="mode">—</div></div>
      <div class="stat"><div class="k">UPS</div><div class="v" id="upsstatus">—</div></div>
      <div class="stat"><div class="k">电量</div><div class="v" id="charge">—</div></div>
      <div class="stat"><div class="k">断电计时</div><div class="v" id="timer">—</div></div>
    </div>
    <div style="margin-top:12px"><span class="badge b-dim" id="verdict">正在读取…</span></div>
    <div class="hint" id="errline"></div>
  </div>

  <div class="card">
    <div class="k" style="margin-bottom:10px">上游 NUT Server</div>
    <div class="grid">
      <div><label>地址</label><input id="host" type="text" placeholder="上游主机名或 IP"></div>
      <div><label>端口</label><input id="port" type="number" value="3493"></div>
      <div><label>UPS 名称</label><input id="upsname" type="text" placeholder="upsname（见下表）"></div>
      <div><label>账号</label><input id="username" type="text" placeholder="见下表"></div>
      <div><label>密码</label><input id="password" type="password" placeholder="见下表"></div>
    </div>
    <div class="hint">
      各家 NUT Server 的默认标识（以自己设备上实际配置为准，可用 <code>upsc -l &lt;主机&gt;</code> 查）：
      绿联 NAS <code>ups0</code> / <code>nut</code> / <code>nut</code>；
      群晖 DSM <code>ups</code> / <code>monuser</code> / <code>secret</code>；
      威联通 QNAP <code>qnapups</code> / <code>admin</code> / <code>123456</code>；飞牛 fnOS <code>ups</code> / <code>monuser</code> / <code>secret</code>。
    </div>
    <div style="margin-top:12px">
      <button class="ghost" id="btnTest">测试连接</button>
      <span class="msg" id="testMsg"></span>
    </div>
  </div>

  <div class="card" id="ruleCard">
    <div class="k" style="margin-bottom:10px">关机规则</div>
    <label>断电多少秒后关机（0 = 不按时间触发）</label>
    <input id="rule_secs" type="number" value="300">
    <div class="grid">
      <div><label><input id="rule_lb" type="checkbox"> UPS 报低电量时关机</label></div>
      <div><label><input id="rule_fsd" type="checkbox"> 收到上游 FSD 时关机</label></div>
      <div><label>低电量确认秒数（须持续这么久才关机）</label><input id="rule_lbgrace" type="number" value="30"></div>
      <div><label>电量低于此值时视为低电量（0 = 不启用）</label><input id="rule_charge" type="number" value="0"></div>
      <div><label>通信中断判定秒数（中断不触发关机）</label><input id="rule_dead" type="number" value="30"></div>
      <div><label>轮询间隔（秒）</label><input id="rule_poll" type="number" value="5"></div>
      <div><label>关机命令</label><input id="rule_cmd" type="text" value="/sbin/shutdown -h +0"></div>
    </div>
    <div class="hint" id="dryHint"></div>
  </div>

  <div class="card warnbox">
    <label style="margin-top:0"><input id="rule_dry" type="checkbox"> 演练模式（只记录不关机）</label>
    <div class="hint">强烈建议先保持开启，拔电看日志确认判定正确，再关掉它。</div>
  </div>

  <div style="margin-bottom:14px">
    <button id="btnSave">保存配置</button>
    <button class="ghost" id="btnReset">复位触发标记</button>
    <span class="msg" id="saveMsg"></span>
  </div>

  <div class="card" style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
    <div style="flex:1;min-width:220px">
      <div class="k">运行日志</div>
      <div class="hint peek" id="logPeek"><span class="pd"></span><span class="pt">—</span></div>
    </div>
    <a class="btn" href="/log">打开日志页 ›（可搜索 / 筛选 / 导出）</a>
  </div>
</div>
<script>
var S = null;
function q(id){ return document.getElementById(id); }
function badge(el, text, cls){ el.className = 'badge ' + cls; el.textContent = text; }

function renderStatus(d){
  S = d;
  q('mode').textContent = ({ONLINE:'市电正常', ONBATTERY:'电池供电', LOWBATT:'低电量',
    DRY_RUN:'演练触发', SHUTTING_DOWN:'关机中', INIT:'初始化', STALE:'通信中断'})[d.mode] || d.mode;
  q('upsstatus').textContent = (d.ups && d.ups.status) || '—';
  q('charge').textContent = (d.ups && d.ups.charge != null) ? d.ups.charge + '%' : '—';
  q('timer').textContent = d.on_battery_seconds ? d.on_battery_seconds + 's' : '—';
  var v = q('verdict');
  if (d.stale) badge(v, '与上游失联（不会触发关机）', 'b-bad');
  else if (d.shutdown_issued) badge(v, '已触发：' + (d.shutdown_reason||''), 'b-warn');
  else if (d.mode === 'ONBATTERY') badge(v, '市电中断，计时中', 'b-warn');
  else if (d.lb_note) badge(v, '低电量' + d.lb_note, 'b-warn');
  else if (d.mode === 'LOWBATT') badge(v, '低电量', 'b-warn');
  else if (d.mode === 'ONLINE') badge(v, '市电正常', 'b-ok');
  else badge(v, d.mode, 'b-dim');
  q('errline').textContent = d.last_error ? '最近错误：' + d.last_error : '';
  if (d.ups && d.ups.model) q('errline').textContent += (q('errline').textContent ? '  ·  ' : '') + d.ups.model;
}

function tick(){
  fetch('/api/status').then(function(r){ return r.json(); }).then(renderStatus).catch(function(){});
  fetch('/api/log?limit=1').then(function(r){ return r.json(); }).then(function(d){
    var e = (d.entries || [])[0];
    var peek = q('logPeek');
    var dot = peek.querySelector('.pd'), txt = peek.querySelector('.pt');
    if (!e) { dot.className = 'pd'; txt.textContent = '暂无日志'; return; }
    dot.className = 'pd ' + (e.level || 'info');
    txt.innerHTML = '<b>' + e.t + '</b>' + e.msg.replace(/[&<>]/g, function(c){
      return ({'&':'&amp;','<':'&lt;','>':'&gt;'})[c]; });
  }).catch(function(){});
}

function fill(c){
  q('host').value = c.upstream.host; q('port').value = c.upstream.port;
  q('upsname').value = c.upstream.upsname; q('username').value = c.upstream.username;
  q('password').value = c.upstream.password;
  q('rule_secs').value = c.rules.shutdown_on_battery_seconds;
  q('rule_lb').checked = !!c.rules.shutdown_on_lowbattery;
  q('rule_fsd').checked = !!c.rules.shutdown_on_fsd;
  q('rule_lbgrace').value = c.rules.lb_grace_seconds;
  q('rule_charge').value = c.rules.battery_charge_low_override;
  q('rule_dead').value = c.rules.deadtime;
  q('rule_poll').value = c.rules.poll_interval;
  q('rule_cmd').value = c.rules.shutdown_cmd;
  q('rule_dry').checked = !!c.rules.dry_run;
  q('dryHint').textContent = c.rules.dry_run ? '当前为演练模式，满足条件时只记日志。' : '⚠️ 演练模式已关闭，满足条件会真的关机。';
}

function collect(){
  return {
    upstream: { host:q('host').value.trim(), port:Number(q('port').value)||3493,
      upsname:q('upsname').value.trim(), username:q('username').value,
      password:q('password').value },
    rules: { shutdown_on_battery_seconds:Number(q('rule_secs').value)||0,
      shutdown_on_lowbattery:q('rule_lb').checked, shutdown_on_fsd:q('rule_fsd').checked,
      lb_grace_seconds:Number(q('rule_lbgrace').value)||0,
      battery_charge_low_override:Number(q('rule_charge').value)||0,
      deadtime:Number(q('rule_dead').value)||30,
      poll_interval:Number(q('rule_poll').value)||5,
      shutdown_cmd:q('rule_cmd').value, dry_run:q('rule_dry').checked }
  };
}

q('btnTest').onclick = function(){
  q('testMsg').textContent = '连接中…'; q('testMsg').style.color = 'var(--dim)';
  fetch('/api/test', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({upstream: collect().upstream})})
  .then(function(r){ return r.json(); })
  .then(function(d){
    if (d.ok) {
      q('testMsg').textContent = '连接成功：' + (d.vars.mfr||'') + ' ' + (d.vars.model||'')
        + ' · 状态 ' + (d.vars.status||'') + ' · 电量 ' + (d.vars.charge||'') + '% · ' + d.count + ' 个变量';
      q('testMsg').style.color = 'var(--ok)';
    } else {
      q('testMsg').textContent = '失败：' + d.error; q('testMsg').style.color = 'var(--bad)';
    }
  }).catch(function(e){ q('testMsg').textContent = '请求失败'; });
};

q('btnSave').onclick = function(){
  q('saveMsg').textContent = '保存中…';
  fetch('/api/config', {method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(collect())})
  .then(function(r){ return r.json(); }).then(function(d){
    q('saveMsg').textContent = d.ok ? '已保存，规则立即生效' : ('失败：' + d.error);
    q('saveMsg').style.color = d.ok ? 'var(--ok)' : 'var(--bad)';
    fetch('/api/config').then(function(r){return r.json();}).then(fill);
  });
};

q('btnReset').onclick = function(){
  fetch('/api/reset_shutdown', {method:'POST'}).then(function(){
    q('saveMsg').textContent = '已复位'; q('saveMsg').style.color = 'var(--ok)';
  });
};

fetch('/api/config').then(function(r){ return r.json(); }).then(fill).catch(function(){});
tick(); setInterval(tick, 2000);
</script>
</body>
</html>
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--port", type=int, default=28190)
    ap.add_argument("--log", default=None)
    a = ap.parse_args()

    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        with open(a.config, encoding="utf-8") as f:
            cfg = deep_merge(cfg, json.load(f))
    except Exception:
        pass

    ctx = Ctx()
    ctx.config = cfg
    ctx.conf_path = a.config
    ctx.state = {"failures": 0, "mode": "INIT", "ob_since": None, "stale": False}
    here = os.path.dirname(os.path.abspath(a.config))
    ctx.log = Log(a.log or (here + "/../var/log/app.log"))
    ctx.stop = threading.Event()
    up0 = cfg["upstream"]
    ctx.log.add("upsbridge %s 启动，上游 %s:%s/%s" % (
        VERSION, up0.get("host") or "(未配置)", up0.get("port"), up0.get("upsname") or "(未配置)"), "ok")
    if not up0.get("host"):
        ctx.log.add("尚未配置上游地址，请打开管理界面填写", "warn")
    if cfg["rules"].get("dry_run", True):
        ctx.log.add("演练模式开启：满足关机条件时只记录，不执行", "warn")

    Monitor(ctx).start()

    srv = ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(ctx))
    ctx.log.add("管理界面监听 :%d" % a.port, "ok")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctx.stop.set()
        ctx.log.add("upsbridge 退出", "warn")


if __name__ == "__main__":
    main()

