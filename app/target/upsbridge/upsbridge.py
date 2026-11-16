#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""upsbridge - 把局域网里任意 NUT Server 的断电事件，变成这台 NAS 的关机动作。

不依赖也不修改 /etc/nut：自己实现 NUT 协议客户端，配置全部在本应用目录里。
"""
import argparse, json, os, socket, sys, threading, time, subprocess, re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "1.0.0"

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
        "battery_charge_low_override": 0,
        "deadtime": 30,
        "poll_interval": 5,
        "shutdown_cmd": "/sbin/shutdown -h +0",
    },
    "notify": {"wall": True},
}


def deep_merge(base, patch):
    out = dict(base)
    for k, v in (patch or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Log:
    def __init__(self, path, cap=400):
        self.path = path
        self.cap = cap
        self.buf = []
        self.lock = threading.Lock()

    def add(self, msg):
        line = time.strftime("%Y-%m-%d %H:%M:%S") + "  " + msg
        with self.lock:
            self.buf.append(line)
            if len(self.buf) > self.cap:
                del self.buf[:len(self.buf) - self.cap]
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass

    def tail(self, n=120):
        with self.lock:
            return list(self.buf[-n:])


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
                    log.add("与上游恢复通信 " + (up.get("host") or ""))
                st["stale"] = False
                self.update_state(variables, fsd, cfg, st, log)
            except Exception as e:
                st["failures"] = st.get("failures", 0) + 1
                self.client.close()
                deadtime = max(1, int(rules.get("deadtime", 30)))
                st["last_error"] = str(e)
                if st["failures"] * interval >= deadtime and not st.get("stale"):
                    st["stale"] = True
                    log.add("与上游失联超过 %ds：%s（不触发关机）" % (deadtime, e))
                time.sleep(min(interval, 3))
                continue
            time.sleep(interval)

    def update_state(self, v, fsd, cfg, st, log):
        rules = cfg["rules"]
        status = v.get("ups.status", "").upper()
        if "FSD" in status.split():
            fsd = True
        charge = None
        try:
            charge = float(v.get("battery.charge", ""))
        except Exception:
            pass
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

        on_batt = ("OB" in status) or ("OB DISCHRG" in status)
        lb = "LB" in status
        if low_override > 0 and charge is not None and charge <= low_override:
            lb = True

        # FSD 加固：上游可能残留陈旧的 FSD 标记（NUT 无复位命令，只能重启 upsd 清）。
        # 因此要求「本次运行期间曾观测到市电中断」，才承认 FSD 是本次断电的强制关机信号。
        if fsd and not st.get("seen_ob"):
            if not st.get("fsd_orphan_logged"):
                st["fsd_orphan_logged"] = True
                log.add("上游有 FSD 标记，但本次运行未观测到断电，视为陈旧标记，不动作")
            fsd = False

        # FSD 需持续一段时间才认
        if fsd:
            if not st.get("fsd_since"):
                st["fsd_since"] = now
                log.add("上游出现 FSD 标记，开始确认")
        else:
            st["fsd_since"] = None
        fsd_grace = float(rules.get("fsd_grace_seconds", 15) or 0)
        if fsd and st.get("fsd_since") and (now - st["fsd_since"]) < fsd_grace:
            if not st.get("fsd_warned"):
                st["fsd_warned"] = True
                log.add("上游有 FSD 标记，需持续 %.0fs 才动作（防上游残留状态误触发）" % fsd_grace)
            fsd = False
        elif fsd:
            st["fsd_warned"] = False

        if st.get("shutdown_issued"):
            # 已触发过，保持终态，不再翻回 ONLINE/ONBATTERY
            return

        if not on_batt and not lb and not fsd:
            if st.get("ob_since"):
                log.add("市电恢复，取消计时（本次断电 %ds）" % int(now - st["ob_since"]))
            st["ob_since"] = None
            st["mode"] = "ONLINE"
            st["reason"] = ""
            return

        if on_batt and not st.get("ob_since"):
            st["ob_since"] = now
            st["seen_ob"] = True
            log.add("检测到市电中断，开始计时（阈值 %ds）" % int(rules.get("shutdown_on_battery_seconds", 0)))

        elapsed = int(now - st["ob_since"]) if st.get("ob_since") else 0
        reason = ""
        if fsd and rules.get("shutdown_on_fsd", True):
            reason = "上游广播 FSD（强制关机）"
        elif lb and rules.get("shutdown_on_lowbattery", True):
            reason = "UPS 报低电量"
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
            log.add("【演练模式】触发条件满足：" + reason + " —— 本应执行关机，未执行")
            return
        cmd = rules.get("shutdown_cmd", "/sbin/shutdown -h +0")
        st["mode"] = "SHUTTING_DOWN"
        log.add("触发关机：" + reason + " —— 执行 " + cmd)
        try:
            subprocess.Popen(cmd, shell=True)
        except Exception as e:
            log.add("关机命令执行失败：" + str(e))


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
            p = self.path.split("?")[0]
            if p in ("/", "/index.html"):
                return self._text(PAGE)
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
                })
            if p == "/api/config":
                return self._json(ctx.config)
            if p == "/api/log":
                return self._json({"lines": ctx.log.tail(200)})
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
                ctx.log.add("配置已保存")
                return self._json({"ok": True})

            if p == "/api/reset_shutdown":
                ctx.state["shutdown_issued"] = False
                ctx.state["shutdown_reason"] = ""
                ctx.state["mode"] = "INIT"
                ctx.state["ob_since"] = None
                ctx.log.add("手动复位已触发标记（用于再次演练）")
                return self._json({"ok": True})

            return self._json({"ok": False, "error": "unknown"}, 404)

    return Handler


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
  button:disabled { opacity:.5; cursor:default; }
  pre { background:#0b0e13; border:1px solid var(--line); border-radius:8px; padding:12px;
        max-height:260px; overflow:auto; font-size:12px; line-height:1.5; color:var(--dim); }
  .hint { color:var(--dim); font-size:12px; margin-top:6px; }
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

  <div class="card">
    <div class="k" style="margin-bottom:10px">运行日志</div>
    <pre id="log">—</pre>
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
  else if (d.mode === 'LOWBATT') badge(v, '低电量', 'b-warn');
  else if (d.mode === 'ONLINE') badge(v, '市电正常', 'b-ok');
  else badge(v, d.mode, 'b-dim');
  q('errline').textContent = d.last_error ? '最近错误：' + d.last_error : '';
  if (d.ups && d.ups.model) q('errline').textContent += (q('errline').textContent ? '  ·  ' : '') + d.ups.model;
}

function tick(){
  fetch('/api/status').then(function(r){ return r.json(); }).then(renderStatus).catch(function(){});
  fetch('/api/log').then(function(r){ return r.json(); }).then(function(d){
    var el = q('log');
    var atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 20;
    el.textContent = d.lines.join('\n') || '—';
    if (atBottom) el.scrollTop = el.scrollHeight;
  }).catch(function(){});
}

function fill(c){
  q('host').value = c.upstream.host; q('port').value = c.upstream.port;
  q('upsname').value = c.upstream.upsname; q('username').value = c.upstream.username;
  q('password').value = c.upstream.password;
  q('rule_secs').value = c.rules.shutdown_on_battery_seconds;
  q('rule_lb').checked = !!c.rules.shutdown_on_lowbattery;
  q('rule_fsd').checked = !!c.rules.shutdown_on_fsd;
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
        VERSION, up0.get("host") or "(未配置)", up0.get("port"), up0.get("upsname") or "(未配置)"))
    if not up0.get("host"):
        ctx.log.add("尚未配置上游地址，请打开管理界面填写")
    if cfg["rules"].get("dry_run", True):
        ctx.log.add("演练模式开启：满足关机条件时只记录，不执行")

    Monitor(ctx).start()

    srv = ThreadingHTTPServer(("0.0.0.0", a.port), make_handler(ctx))
    ctx.log.add("管理界面监听 :%d" % a.port)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        ctx.stop.set()
        ctx.log.add("upsbridge 退出")


if __name__ == "__main__":
    main()

