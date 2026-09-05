"""Cookie 解析与 api_user 自动提取的回归测试。

对应两个修复：
1. 只粘贴 session 值（含 base64 末尾的 `=` 填充）也要能解析出 session；
2. 从 session 值里自动提取 api_user（与官方生成器逻辑一致），留空也能保存。
"""
import base64

from checkin import parse_cookies
from webui import extract_api_user, validate_account


def _make_session(payload: str = 'xx12345xx') -> str:
	"""构造符合 anyrouter session 格式的值：
	base64("a|" + base64(payload) + "|sig")，第 2 段载荷含 5 位数字用户 id。
	"""
	payload_b64 = base64.b64encode(payload.encode()).decode()
	outer = base64.b64encode(f'a|{payload_b64}|sig'.encode()).decode()
	return outer


def test_parse_standard_cookie_string():
	assert parse_cookies('session=abc; acw_tc=123') == {'session': 'abc', 'acw_tc': '123'}


def test_parse_cookie_header_prefix():
	assert parse_cookies('Cookie: session=abc; acw_tc=123') == {'session': 'abc', 'acw_tc': '123'}


def test_parse_dict_and_json():
	assert parse_cookies({'session': 'abc'}) == {'session': 'abc'}
	assert parse_cookies('{"session": "abc"}') == {'session': 'abc'}
	assert parse_cookies('{"cookies": {"session": "abc"}}') == {'session': 'abc'}


def test_parse_bare_session_no_padding():
	# 无 = 的裸 session 值（旧逻辑已支持）
	assert parse_cookies('eyJhbGciOiJIUzI1NiJ9.abc') == {'session': 'eyJhbGciOiJIUzI1NiJ9.abc'}


def test_parse_bare_session_with_padding():
	# 回归：带 = 填充的 base64 session 值，之前会被误当成 k=v 导致 400
	value = _make_session(payload='12345')  # 该组合的外层 base64 有 = 填充
	assert '=' in value
	assert parse_cookies(value) == {'session': value}


def test_parse_multiple_pairs_without_session():
	# 多段 k=v 且没有 session → 不应把整段当 session
	result = parse_cookies('acw_tc=123; cdn_sec_tc=456')
	assert 'session' not in result
	assert result == {'acw_tc': '123', 'cdn_sec_tc': '456'}


def test_parse_empty():
	assert parse_cookies('') == {}
	assert parse_cookies('   ') == {}


def test_extract_api_user():
	value = _make_session(payload='{"user_id":12345}')
	assert extract_api_user(value) == '12345'


def test_extract_api_user_other_digits():
	value = _make_session(payload='abc 9876543 def')
	assert extract_api_user(value) == '9876543'


def test_extract_api_user_invalid():
	assert extract_api_user('not-a-session') is None
	assert extract_api_user('') is None


def test_validate_account_paste_only_session():
	# 只粘 session（含 = 填充、不带 api_user）→ 保存成功且自动提取出 api_user
	value = _make_session(payload='{"user_id":12345}')
	ok, err, account = validate_account({'cookies': value, 'api_user': ''})
	assert ok, err
	assert account['api_user'] == '12345'
	assert account['cookies'] == {'session': value}


def test_validate_account_still_requires_api_user_when_extract_fails():
	ok, err, _ = validate_account({'cookies': 'plain-value-without-id', 'api_user': ''})
	assert not ok
	assert 'api_user' in err


def test_validate_account_email_password_only():
	ok, err, account = validate_account({'email': 'a@b.com', 'password': 'pw'})
	assert ok, err
	assert account['email'] == 'a@b.com'
	assert account['password'] == 'pw'
	assert 'cookies' not in account
	assert 'api_user' not in account


def test_validate_account_email_without_password():
	ok, err, _ = validate_account({'email': 'a@b.com'})
	assert not ok
	assert '成对' in err


def test_validate_account_password_without_email():
	ok, err, _ = validate_account({'password': 'pw'})
	assert not ok
	assert '成对' in err


def test_validate_account_no_credentials():
	ok, err, _ = validate_account({'provider': 'agentrouter'})
	assert not ok
	assert '缺少凭据' in err


def test_validate_account_email_password_with_session():
	value = _make_session(payload='{"user_id":12345}')
	ok, err, account = validate_account({'email': 'a@b.com', 'password': 'pw', 'cookies': value})
	assert ok, err
	assert account['email'] == 'a@b.com'
	assert account['cookies'] == {'session': value}
	assert account['api_user'] == '12345'  # 邮箱密码在场时 api_user 仍自动提取备用
