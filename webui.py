#!/usr/bin/env python3
"""
AnyRouter 签到 Web UI：账号管理（cookie 登录）+ 手动触发签到 + 运行日志。

纯标准库实现（http.server + ThreadingHTTPServer），无第三方依赖。
账号存到 CHECKIN_ACCOUNTS_FILE（默认 data/accounts.json），与 checkin.py 共用；
触发签到时用 flock 文件锁与 entrypoint 的定时循环互斥。

本地运行：  uv run webui.py            （默认 http://localhost:8080）
容器内运行： 由 docker/entrypoint.sh 在后台启动
"""

import base64
import json
import os
import re
import shlex
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from checkin import parse_cookies
from utils.notify import NotificationKit

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CHECKIN_SCRIPT = os.path.join(APP_DIR, 'checkin.py')
PROVIDER_CHOICES = ('anyrouter', 'agentrouter')


def get_accounts_file() -> str:
	"""账号文件路径，由 CHECKIN_ACCOUNTS_FILE 覆盖，默认 data/accounts.json（相对运行目录）。

	启动后一律以绝对路径为准：触发签到等子进程场景会以其它 cwd 运行，
	相对路径会二次解析导致找不到文件（LXC 等丢失环境变量的部署曾踩过坑）。
	"""
	p = os.getenv('CHECKIN_ACCOUNTS_FILE', 'data/accounts.json')
	return os.path.abspath(p)


def get_data_dir() -> str:
	return os.path.dirname(get_accounts_file()) or '.'


def load_accounts() -> list:
	"""读取账号列表；文件不存在或损坏时返回空列表。"""
	path = get_accounts_file()
	try:
		with open(path, 'r', encoding='utf-8') as f:
			data = json.load(f)
		if isinstance(data, list):
			return data
	except (FileNotFoundError, json.JSONDecodeError):
		pass
	return []


def save_accounts(accounts: list) -> None:
	"""原子写入账号文件，权限 0600（含敏感 cookie）。"""
	path = get_accounts_file()
	os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
	tmp = f'{path}.tmp'
	with open(tmp, 'w', encoding='utf-8') as f:
		json.dump(accounts, f, ensure_ascii=False, indent=2)
	os.chmod(tmp, 0o600)
	os.replace(tmp, path)


def get_lock_file() -> str:
	return os.path.join(get_data_dir(), '.checkin.lock')


def get_log_file() -> str:
	return os.path.join(get_data_dir(), 'last_run.log')


def get_settings_file() -> str:
	"""设置文件路径：默认与账号文件同目录（webui_settings.json），CHECKIN_WEBUI_SETTINGS_FILE 覆盖。

	与 get_accounts_file 一样固定为绝对路径（参见其注释）。
	"""
	p = os.getenv(
		'CHECKIN_WEBUI_SETTINGS_FILE',
		os.path.join(os.path.dirname(get_accounts_file()) or '.', 'webui_settings.json'),
	)
	return os.path.abspath(p)


def load_settings() -> dict:
	"""读取 Web UI 设置（Bark 等）；文件不存在或损坏时返回空字典。"""
	path = get_settings_file()
	try:
		with open(path, 'r', encoding='utf-8') as f:
			data = json.load(f)
		return data if isinstance(data, dict) else {}
	except (FileNotFoundError, json.JSONDecodeError):
		return {}


def save_settings(settings: dict) -> None:
	"""原子写入设置文件，权限 0600（Bark Key 是敏感凭证）。"""
	path = get_settings_file()
	os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
	tmp = f'{path}.tmp'
	with open(tmp, 'w', encoding='utf-8') as f:
		json.dump(settings, f, ensure_ascii=False, indent=2)
	os.chmod(tmp, 0o600)
	os.replace(tmp, path)


def mask_key(key: str) -> str:
	return (key[:6] + '***') if len(key) > 6 else '***'


def public_settings(settings: dict) -> dict:
	key = settings.get('bark_key') or ''
	return {
		'bark_key_masked': mask_key(str(key)) if key else None,
		'has_bark': bool(key),
		'bark_server': settings.get('bark_server') or 'https://api.day.app',
	}


def _pad_b64(s: str) -> str:
	"""补 base64 的 = 填充。"""
	return s + '=' * ((4 - len(s) % 4) % 4)


def extract_api_user(session: str) -> str | None:
	"""从 session 值里提取 api_user，与官方生成器逻辑一致。

	anyrouter 的 session 是 base64：解码后按 | 分成多段，第 2 段是载荷
	（仍是 base64），其中包含 5~10 位数字的用户 id。解析失败返回 None。
	"""
	try:
		outer = base64.b64decode(
			_pad_b64(session.replace('-', '+').replace('_', '/'))
		).decode('utf-8', 'replace')
		parts = outer.split('|')
		if len(parts) < 3:
			return None
		payload_b64 = parts[1].replace('-', '+').replace('_', '/')
		payload = base64.b64decode(_pad_b64(payload_b64)).decode('utf-8', 'replace')
		m = re.search(r'\d{5,10}', payload)
		return m.group(0) if m else None
	except Exception:
		return None


def validate_account(data: dict) -> tuple[bool, str, dict | None]:
	"""校验并规范化单条账号（cookie 登录）。

	api_user 可留空：会尝试从 session 值里自动提取（与官方生成器一致），
	提取失败才要求手动填写。返回 (是否通过, 错误信息, 规范化后的账号字典)。
	"""
	provider = (data.get('provider') or 'anyrouter').strip()
	if not provider:
		return False, 'provider 不能为空', None

	cookies = data.get('cookies')
	if isinstance(cookies, str):
		cookies = parse_cookies(cookies)
	if not isinstance(cookies, dict) or not cookies.get('session'):
		return False, 'cookie 里没解析出 session。可直接粘贴整段请求头（如 session=xxx; acw_tc=yyy），或只粘 session 的值', None

	api_user = (data.get('api_user') or '').strip()
	if not api_user:
		api_user = extract_api_user(str(cookies['session'])) or ''
		if not api_user:
			return False, 'api_user 不能为空（未能从 session 自动提取，请手动填写）', None

	account = {'provider': provider, 'api_user': api_user, 'cookies': cookies}
	if data.get('name'):
		account['name'] = data['name'].strip()
	return True, '', account


def mask_session(cookies) -> str:
	"""对 session 值脱敏，仅用于列表展示。"""
	if isinstance(cookies, dict) and cookies.get('session'):
		value = str(cookies['session'])
		return value[:8] + '***' if len(value) > 8 else value + '***'
	return '(未设置)'


def public_account(idx: int, acc: dict) -> dict:
	return {
		'index': idx,
		'name': acc.get('name'),
		'provider': acc.get('provider', 'anyrouter'),
		'api_user': acc.get('api_user'),
		'session_masked': mask_session(acc.get('cookies')),
		'has_email': bool(acc.get('email') and acc.get('password')),
	}


def is_checkin_running() -> bool:
	"""锁被占用则视为签到正在运行。"""
	try:
		proc = subprocess.run(['flock', '-n', get_lock_file(), 'true'])
		return proc.returncode != 0
	except FileNotFoundError:
		return False


def trigger_checkin() -> tuple[str, str]:
	"""触发签到：后台进程抢 flock 锁运行 checkin.py，输出写入 last_run.log。

	返回 (状态, 说明)：'started' / 'busy' / 'error'。
	"""
	data_dir = get_data_dir()
	os.makedirs(data_dir, exist_ok=True)

	# 先探测锁；被占用则本次不启动
	probe = subprocess.run(['flock', '-n', get_lock_file(), 'true'])
	if probe.returncode != 0:
		return 'busy', '已有签到正在运行（定时任务或手动触发），请稍后再试'

	# 把账号/设置文件以绝对路径传给子进程：checkin.py 按自身 cwd 解析相对路径，
	# 若不带这两个环境变量，会和这里（cwd=data_dir）解析出不一致的文件
	env = dict(os.environ)
	env['CHECKIN_ACCOUNTS_FILE'] = get_accounts_file()
	env['CHECKIN_WEBUI_SETTINGS_FILE'] = get_settings_file()

	cmd = f'{shlex.quote(sys.executable)} {shlex.quote(CHECKIN_SCRIPT)} >> {shlex.quote(get_log_file())} 2>&1'
	subprocess.Popen(
		['flock', '-n', get_lock_file(), '-c', cmd],
		stdout=subprocess.DEVNULL,
		stderr=subprocess.DEVNULL,
		cwd=data_dir,
		env=env,
	)
	return 'started', '签到已触发，请稍后查看运行日志'


def read_log_tail(max_lines: int = 200) -> str:
	"""返回 last_run.log 尾部内容。"""
	path = get_log_file()
	if not os.path.isfile(path):
		return '(暂无运行日志)'
	try:
		with open(path, 'r', encoding='utf-8', errors='replace') as f:
			lines = f.read().splitlines()
		return '\n'.join(lines[-max_lines:]) if lines else '(日志为空)'
	except Exception as e:
		return f'(读取日志失败: {e})'


class Handler(BaseHTTPRequestHandler):
	server_version = 'AnyRouterWebUI/1.0'

	# ---------- 工具 ----------

	def _send_json(self, status: int, payload) -> None:
		body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
		self.send_response(status)
		self.send_header('Content-Type', 'application/json; charset=utf-8')
		self.send_header('Content-Length', str(len(body)))
		self.send_header('Cache-Control', 'no-store')
		self.end_headers()
		self.wfile.write(body)

	def _read_json_body(self) -> dict:
		length = int(self.headers.get('Content-Length') or 0)
		if length <= 0:
			return {}
		raw = self.rfile.read(length).decode('utf-8')
		try:
			data = json.loads(raw)
			return data if isinstance(data, dict) else {}
		except json.JSONDecodeError:
			return {}

	def _index_of(self) -> int | None:
		parts = self.path.split('/')
		# /api/accounts/0
		if len(parts) < 4:
			return None
		try:
			return int(parts[3])
		except ValueError:
			return None

	# ---------- 路由 ----------

	def do_GET(self) -> None:  # noqa: N802
		parsed = urlparse(self.path)
		path = parsed.path

		if path == '/':
			body = INDEX_HTML.encode('utf-8')
			self.send_response(200)
			self.send_header('Content-Type', 'text/html; charset=utf-8')
			self.send_header('Content-Length', str(len(body)))
			self.end_headers()
			self.wfile.write(body)
			return

		if path == '/api/accounts':
			accounts = load_accounts()
			self._send_json(200, [public_account(i, a) for i, a in enumerate(accounts)])
			return

		if path == '/api/status':
			self._send_json(200, {
				'running': is_checkin_running(),
				'log_mtime': os.path.getmtime(get_log_file()) if os.path.isfile(get_log_file()) else None,
				'accounts': len(load_accounts()),
			})
			return

		if path == '/api/settings':
			self._send_json(200, public_settings(load_settings()))
			return

		if path == '/api/logs':
			self._send_json(200, {'log': read_log_tail()})
			return

		self._send_json(404, {'error': 'not found'})

	def do_POST(self) -> None:  # noqa: N802
		parsed = urlparse(self.path)
		path = parsed.path

		if path == '/api/accounts':
			body = self._read_json_body()
			ok, err, account = validate_account(body)
			if not ok:
				self._send_json(400, {'error': err})
				return
			accounts = load_accounts()
			accounts.append(account)
			save_accounts(accounts)
			self._send_json(200, public_account(len(accounts) - 1, account))
			return

		if path == '/api/run':
			status, message = trigger_checkin()
			code = 409 if status == 'busy' else (500 if status == 'error' else 200)
			self._send_json(code, {'status': status, 'message': message})
			return

		if path == '/api/test_notify':
			# 用当前保存的设置（新建实例，读到最新 Bark key）发送一条测试推送
			try:
				kit = NotificationKit()
				kit.send_bark(
					'AnyRouter 测试推送',
					f'这是一条测试推送，签到通知已生效。\n发送时间: {time.strftime("%Y-%m-%d %H:%M:%S")}',
				)
				self._send_json(200, {'ok': True, 'message': '测试推送已发送，请查看手机'})
			except Exception as e:
				self._send_json(500, {'ok': False, 'error': f'发送失败：{e}'})
			return

		self._send_json(404, {'error': 'not found'})

	def do_PUT(self) -> None:  # noqa: N802
		parsed = urlparse(self.path)
		path = parsed.path

		if path == '/api/settings':
			body = self._read_json_body()
			settings = load_settings()
			if body.get('clear_bark'):
				settings.pop('bark_key', None)
			elif body.get('bark_key'):
				settings['bark_key'] = str(body['bark_key']).strip()
			if body.get('bark_server') is not None:
				server = str(body['bark_server']).strip()
				settings['bark_server'] = server or 'https://api.day.app'
			save_settings(settings)
			self._send_json(200, public_settings(settings))
			return

		if path.startswith('/api/accounts/'):
			idx = self._index_of()
			accounts = load_accounts()
			if idx is None or not 0 <= idx < len(accounts):
				self._send_json(404, {'error': '账号不存在'})
				return

			body = self._read_json_body()
			current = dict(accounts[idx])

			# cookie 未提交时保留原值；提交则校验
			if body.get('cookies') is not None and str(body['cookies']).strip():
				candidate = dict(current)
				candidate['cookies'] = body['cookies']
				candidate['api_user'] = body.get('api_user', current.get('api_user'))
				candidate['provider'] = body.get('provider', current.get('provider'))
				ok, err, normalized = validate_account(candidate)
				if not ok:
					self._send_json(400, {'error': err})
					return
				current['cookies'] = normalized['cookies']

			if body.get('name') is not None:
				if not body['name'].strip():
					self._send_json(400, {'error': 'name 不能为空'})
					return
				current['name'] = body['name'].strip()
			if body.get('provider'):
				current['provider'] = body['provider']
			if body.get('api_user'):
				current['api_user'] = body['api_user'].strip()

			# 校验整体（含可能未变更的字段），并把自动提取出的 api_user 落库
			ok, err, normalized = validate_account(current)
			if not ok:
				self._send_json(400, {'error': err})
				return
			if normalized and normalized.get('api_user'):
				current['api_user'] = normalized['api_user']
			accounts[idx] = current
			save_accounts(accounts)
			self._send_json(200, public_account(idx, current))
			return

		self._send_json(404, {'error': 'not found'})

	def do_DELETE(self) -> None:  # noqa: N802
		parsed = urlparse(self.path)
		path = parsed.path

		if path.startswith('/api/accounts/'):
			idx = self._index_of()
			accounts = load_accounts()
			if idx is None or not 0 <= idx < len(accounts):
				self._send_json(404, {'error': '账号不存在'})
				return
			removed = accounts.pop(idx)
			save_accounts(accounts)
			self._send_json(200, {'deleted': public_account(idx, removed)})
			return

		self._send_json(404, {'error': 'not found'})

	def log_message(self, fmt: str, *args) -> None:  # noqa: A003
		# 精简访问日志，避免刷屏
		sys.stderr.write('[webui] %s - %s\n' % (self.address_string(), fmt % args))


INDEX_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AnyRouter 签到管理</title>
<style>
  :root {
    --bg: #0f1420; --card: #1a2130; --border: #2a3448; --text: #e6ecf5;
    --muted: #93a0b8; --accent: #4f8cff; --ok: #3fb950; --warn: #d29922;
    --danger: #f85149;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 24px; background: var(--bg); color: var(--text);
    font: 14px/1.6 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif;
  }
  .wrap { max-width: 760px; margin: 0 auto; }
  h1 { font-size: 20px; margin: 0 0 4px; }
  .sub { color: var(--muted); margin: 0 0 20px; font-size: 13px; }
  .card {
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 16px; margin-bottom: 16px;
  }
  .card h2 { font-size: 14px; margin: 0 0 12px; color: var(--muted); font-weight: 600; }
  label { display: block; font-size: 12px; color: var(--muted); margin: 10px 0 4px; }
  input, select, textarea {
    width: 100%; background: var(--bg); color: var(--text);
    border: 1px solid var(--border); border-radius: 6px; padding: 8px 10px;
    font: inherit;
  }
  textarea { resize: vertical; min-height: 60px; font-family: ui-monospace, monospace; }
  .row { display: flex; gap: 12px; }
  .row > div { flex: 1; }
  button {
    border: 0; border-radius: 6px; padding: 9px 16px; font: inherit; cursor: pointer;
    background: var(--accent); color: #fff;
  }
  button.secondary { background: #2a3448; color: var(--text); }
  button.danger { background: transparent; color: var(--danger); border: 1px solid var(--danger); }
  button:disabled { opacity: .5; cursor: not-allowed; }
  .hint { font-size: 12px; color: var(--muted); margin-top: 6px; }
  .msg { margin-top: 10px; font-size: 13px; min-height: 18px; }
  .msg.err { color: var(--danger); }
  .msg.ok { color: var(--ok); }
  .acct {
    display: flex; align-items: center; justify-content: space-between;
    padding: 10px 0; border-bottom: 1px solid var(--border);
  }
  .acct:last-child { border-bottom: 0; }
  .acct .meta { font-size: 13px; }
  .acct .name { font-weight: 600; }
  .acct .tag {
    display: inline-block; font-size: 11px; color: var(--muted);
    border: 1px solid var(--border); border-radius: 4px; padding: 0 6px; margin-left: 6px;
  }
  .acct .detail { font-size: 12px; color: var(--muted); margin-top: 2px; }
  .acct .ops { display: flex; gap: 8px; flex-shrink: 0; }
  .acct .ops button { padding: 4px 10px; font-size: 12px; }
  .runbar { display: flex; align-items: center; gap: 12px; }
  .status { font-size: 13px; }
  .status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; margin-right: 6px; }
  .status.running .dot { background: var(--warn); }
  .status.idle .dot { background: var(--ok); }
  pre {
    background: #0a0e16; border: 1px solid var(--border); border-radius: 8px;
    padding: 12px; font: 12px/1.5 ui-monospace, monospace; overflow: auto;
    max-height: 420px; white-space: pre-wrap; word-break: break-all; margin: 12px 0 0;
  }
  .empty { color: var(--muted); font-size: 13px; padding: 8px 0; }
</style>
</head>
<body>
<div class="wrap">
  <h1>AnyRouter 签到管理</h1>
  <p class="sub">账号以 session cookie 登录，保存到 data/accounts.json，供签到脚本自动读取。</p>

  <div class="card">
    <h2 id="formTitle">添加账号</h2>
    <form id="accForm" autocomplete="off">
      <div class="row">
        <div>
          <label>平台 Provider</label>
          <select id="fProvider">
            <option value="anyrouter">anyrouter</option>
            <option value="agentrouter">agentrouter</option>
          </select>
        </div>
        <div>
          <label>名称（可选）</label>
          <input id="fName" placeholder="例如：主账号">
        </div>
      </div>
      <label>Cookie（可直接粘贴整段请求头，如 <code>session=xxx; acw_tc=yyy</code>，或只粘 session 值）</label>
      <textarea id="fCookies" placeholder="session=你的session值"></textarea>
      <label>api_user（new-api-user 请求头值，<b>可留空</b>：保存时会尝试从 session 自动提取）</label>
      <input id="fApiUser" placeholder="留空则自动提取">
      <div class="msg" id="formMsg"></div>
      <div style="margin-top:12px; display:flex; gap:10px;">
        <button type="submit" id="submitBtn">保存账号</button>
        <button type="button" class="secondary" id="cancelEdit" style="display:none;">取消编辑</button>
      </div>
    </form>
  </div>

  <div class="card">
    <h2>账号列表</h2>
    <div id="acctList"><div class="empty">加载中…</div></div>
  </div>

  <div class="card">
    <h2>手动签到</h2>
    <div class="runbar">
      <button id="runBtn">立即签到</button>
      <span class="status idle" id="runStatus"><span class="dot"></span>空闲</span>
    </div>
    <pre id="logView">(暂无运行日志)</pre>
  </div>

  <div class="card">
    <h2>Bark 推送</h2>
    <label>Bark Key（设备 key）</label>
    <input id="sBarkKey" autocomplete="off">
    <label>Bark 服务器（可选，默认 https://api.day.app）</label>
    <input id="sBarkServer" placeholder="https://api.day.app">
    <div class="hint">签到成功或失败都会推送到手机；Key 存到设置文件，只显示前几位。</div>
    <div class="msg" id="settingsMsg"></div>
    <div style="margin-top:12px; display:flex; gap:10px; align-items:center;">
      <button id="saveSettings">保存设置</button>
      <button id="testNotify" class="secondary">测试推送</button>
      <button id="clearBark" class="danger" style="display:none;">清除 Key</button>
      <span class="hint" id="barkState" style="margin:0;"></span>
    </div>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let accounts = [];
let editing = -1;
let running = false;

function fmtSession(s) { return s || '(未设置)'; }

function renderAccounts() {
  const box = $('acctList');
  if (!accounts.length) { box.innerHTML = '<div class="empty">还没有账号，先在上方添加。</div>'; return; }
  box.innerHTML = accounts.map(a => `
    <div class="acct">
      <div class="meta">
        <span class="name">${esc(a.name || ('账号 ' + (a.index + 1)))}</span>
        <span class="tag">${esc(a.provider)}</span>
        ${a.has_email ? '<span class="tag">邮箱登录</span>' : ''}
        <div class="detail">api_user: ${esc(a.api_user)} · session: ${esc(a.session_masked)}</div>
      </div>
      <div class="ops">
        <button class="secondary" onclick="editAccount(${a.index})">编辑</button>
        <button class="danger" onclick="deleteAccount(${a.index})">删除</button>
      </div>
    </div>`).join('');
}
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

async function api(path, opts) {
  const res = await fetch(path, Object.assign({ headers: {'Content-Type': 'application/json'} }, opts));
  const data = await res.json().catch(() => ({}));
  if (!res.ok && !data.message) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

function setFormMsg(text, ok) {
  const m = $('formMsg');
  m.textContent = text || '';
  m.className = 'msg ' + (ok ? 'ok' : (text ? 'err' : ''));
}

async function refreshAccounts() {
  try { accounts = await api('/api/accounts'); renderAccounts(); }
  catch (e) { console.error(e); }
}

$('accForm').addEventListener('submit', async (e) => {
  e.preventDefault();
  const body = {
    provider: $('fProvider').value,
    name: $('fName').value.trim() || undefined,
    cookies: $('fCookies').value.trim(),
    api_user: $('fApiUser').value.trim(),
  };
  if (editing >= 0 && !body.cookies) { delete body.cookies; } // 编辑时留空 cookie 表示不变
  try {
    if (editing >= 0) {
      const acc = await api('/api/accounts/' + editing, { method: 'PUT', body: JSON.stringify(body) });
      setFormMsg('已更新账号（api_user: ' + (acc.api_user || '-') + '）', true);
    } else {
      const acc = await api('/api/accounts', { method: 'POST', body: JSON.stringify(body) });
      setFormMsg('已添加账号（api_user: ' + (acc.api_user || '-') + '）', true);
    }
    cancelEdit();
    $('accForm').reset();
    refreshAccounts();
  } catch (err) { setFormMsg('保存失败：' + err.message); }
});

function editAccount(i) {
  editing = i;
  const a = accounts[i];
  $('formTitle').textContent = '编辑账号 #' + (i + 1);
  $('fProvider').value = a.provider;
  $('fName').value = a.name || '';
  $('fCookies').value = '';           // 重新粘贴；留空表示不修改
  $('fCookies').placeholder = '留空表示保持原 cookie 不变';
  $('fApiUser').value = a.api_user || '';
  $('cancelEdit').style.display = '';
  setFormMsg('编辑模式：cookie 留空则保持不变');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}
function cancelEdit() {
  editing = -1;
  $('formTitle').textContent = '添加账号';
  $('fCookies').placeholder = 'session=你的session值';
  $('cancelEdit').style.display = 'none';
  setFormMsg('');
}
$('cancelEdit').addEventListener('click', () => { cancelEdit(); $('accForm').reset(); });

async function deleteAccount(i) {
  if (!confirm('确认删除账号 ' + (accounts[i].name || ('#' + (i + 1))) + ' ？')) return;
  try { await api('/api/accounts/' + i, { method: 'DELETE' }); refreshAccounts(); }
  catch (e) { alert('删除失败：' + e.message); }
}

$('runBtn').addEventListener('click', async () => {
  $('runBtn').disabled = true;
  try {
    const r = await api('/api/run', { method: 'POST', body: '{}' });
    setStatus(r.status === 'busy');
    if (r.message) alert(r.message);
  } catch (e) { alert('触发失败：' + e.message); }
  $('runBtn').disabled = false;
  poll();
});

function setStatus(r) {
  running = r;
  const el = $('runStatus');
  el.className = 'status ' + (r ? 'running' : 'idle');
  el.innerHTML = '<span class="dot"></span>' + (r ? '签到运行中…' : '空闲');
  $('runBtn').disabled = r;
}

async function poll() {
  try {
    const s = await api('/api/status');
    setStatus(!!s.running);
    const logs = await api('/api/logs');
    const view = $('logView');
    const stick = view.scrollTop + view.clientHeight >= view.scrollHeight - 40;
    view.textContent = logs.log;
    if (stick || s.running) view.scrollTop = view.scrollHeight;
  } catch (e) { console.error(e); }
}

// ---------- Bark 推送设置 ----------
function setSettingsMsg(text, ok) {
  const m = $('settingsMsg');
  m.textContent = text || '';
  m.className = 'msg ' + (ok ? 'ok' : (text ? 'err' : ''));
}
async function loadSettings() {
  try {
    const s = await api('/api/settings');
    $('sBarkServer').value = s.bark_server || 'https://api.day.app';
    $('sBarkKey').value = '';
    $('sBarkKey').placeholder = s.has_bark ? ('当前：' + s.bark_key_masked + '（留空不修改）') : '输入 Bark Key';
    $('clearBark').style.display = s.has_bark ? '' : 'none';
    $('barkState').textContent = s.has_bark ? '已设置（' + s.bark_key_masked + '）' : '未设置';
  } catch (e) { console.error(e); }
}
$('saveSettings').addEventListener('click', async () => {
  const body = { bark_server: $('sBarkServer').value.trim() };
  const k = $('sBarkKey').value.trim();
  if (k) body.bark_key = k;
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify(body) });
    setSettingsMsg('已保存', true);
    loadSettings();
  } catch (e) { setSettingsMsg('保存失败：' + e.message); }
});
$('testNotify').addEventListener('click', async () => {
  $('testNotify').disabled = true;
  try {
    const r = await api('/api/test_notify', { method: 'POST', body: '{}' });
    setSettingsMsg(r.message || '已发送', true);
  } catch (e) { setSettingsMsg(e.message || '发送失败'); }
  $('testNotify').disabled = false;
});
$('clearBark').addEventListener('click', async () => {
  if (!confirm('清除 Bark Key？')) return;
  try {
    await api('/api/settings', { method: 'PUT', body: JSON.stringify({ clear_bark: true }) });
    setSettingsMsg('已清除', true);
    loadSettings();
  } catch (e) { setSettingsMsg('清除失败：' + e.message); }
});

setInterval(poll, 2000);
refreshAccounts();
loadSettings();
</script>
</body>
</html>
"""


def main() -> None:
	port = int(os.getenv('CHECKIN_WEBUI_PORT', '8090'))
	server = ThreadingHTTPServer(('0.0.0.0', port), Handler)
	print(f'[webui] AnyRouter Web UI listening on http://0.0.0.0:{port}')
	print(f'[webui] accounts file: {get_accounts_file()}')
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		pass


if __name__ == '__main__':
	main()
