"""Pure-HTTP zhixuewang login with Geetest v4 slide-only captcha bypass.

Self-contained module for AstrBot plugin — no dependency on src/ project.
Combines login flow (custom_provider) + encryption (crypto) into one file.

Login chain (8 steps, 2 captcha solves):
  1. GET  /login/getServiceUrl                         → casUrl, serviceUrl
  2. Solve Geetest v4 slide captcha #1 (fixed captcha_id)
  3. POST /edition/login?from=wap_login                → userId, captchaId2
  4. Solve Geetest v4 slide captcha #2 (fixed captcha_id)
  5. GET  {casUrl}/v1/getSingleAt (JSONP)              → at, service
  6a. GET  {service} (open.changyan.com/sso/atLogin)   → CASTGC cookie
  6b. GET  open.changyan.com/sso/login?...             → st (service ticket)
  6c. POST {serviceUrl} action=login&ticket={st}       → session cookies
  7. POST /loginSuccess/ {userId}                      → complete local login
"""

from __future__ import annotations

import json
import random as _random
import re
import time
import uuid
from typing import Dict
from urllib.parse import urlencode, quote

from curl_cffi import requests as curl_requests

# ---------------------------------------------------------------------------
# constants
# ---------------------------------------------------------------------------

ZHIXUE_BASE_URL = "https://www.zhixue.com"

APP_ID = "zx-container-client"
CLIENT = "web"
CAPTCHA_TYPE = "third"
ACCOUNT_VERSION = "v2"
LOGIN_TYPE = "loginByNormal"
_ENCODE_TYPE = "R2/P"

_GEETEST_BASE_URL = "https://xunfei.geetest.com"
_FIXED_CAPTCHA_ID = "a6474422e78e5bb048082ec77d141068"

_MAX_SLIDE_RETRIES = 30
_RETRY_DELAY_SEC = 0.5

_VALIDATE_KEYS = {"captcha_id", "lot_number", "pass_token", "captcha_output", "gen_time"}

# ---------------------------------------------------------------------------
# RC4 encryption (matches rc4.js exactly)
# ---------------------------------------------------------------------------

_RC4_SECRET = "iflytzhixueweb"


def _rc4(data: str, key: str) -> str:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + ord(key[i % len(key)])) % 256
        s[i], s[j] = s[j], s[i]
    i = j = 0
    out: list[str] = []
    for ch in data:
        i = (i + 1) % 256
        j = (j + s[i]) % 256
        s[i], s[j] = s[j], s[i]
        out.append(chr(ord(ch) ^ s[(s[i] + s[j]) % 256]))
    return "".join(out)


def _to_hex(s: str) -> str:
    return "".join(f"{ord(c):02x}" for c in s)


def rc4_encrypt_password(password: str) -> str:
    """Encrypt plaintext password to hex(RC4(password, zxlogin_secret))."""
    return _to_hex(_rc4(password, _RC4_SECRET))


# ---------------------------------------------------------------------------
# RSA R2/P encryption (matches sso.all.min.js encryptedString)
# ---------------------------------------------------------------------------

_RSA_MODULUS_HEX = (
    "00ccd806a03c7391ee8f884f5902102d95f6d534d597ac42219dd8a79b1465e"
    "186c0162a6771b55e7be7422c4af494ba0112ede4eb00fc751723f2c235ca419"
    "876e7103ea904c29522b72d754f66ff1958098396f17c6cd2c9446e8c2bb5f40"
    "00a9c1c6577236a57e270bef07e7fe7bbec1f0e8993734c8bd4750e01feb21b6dc9"
)
_RSA_EXPONENT_HEX = "010001"


def _bigint_from_hex(hex_str: str) -> int:
    return int(hex_str, 16)


def _bigint_high_index(n: int) -> int:
    if n == 0:
        return 0
    return (n.bit_length() - 1) // 16


def _bi_to_hex(val: int) -> str:
    digits: list[int] = []
    temp = val
    while temp > 0:
        digits.append(temp & 0xFFFF)
        temp >>= 16
    if not digits:
        return "0000"
    return "".join(f"{d:04x}" for d in reversed(digits))


def rsa_encrypt_r2p(plaintext: str) -> str:
    """Encrypt plaintext using the SSO RSA R2/P scheme.

    Matches sso.all.min.js encryptedString (NOT security.js):
    digitSize = 2 * biHighIndex(modulus) + 2 (= 128)
    chunkSize = digitSize - 11 (= 117)
    Block: [plaintext] [0x00] [random padding] [0x02] [0x00]
    """
    modulus = _bigint_from_hex(_RSA_MODULUS_HEX)
    exponent = _bigint_from_hex(_RSA_EXPONENT_HEX)

    digit_size = 2 * _bigint_high_index(modulus) + 2  # = 128
    chunk_size = digit_size - 11  # = 117

    chars = [ord(c) for c in plaintext]
    total_len = len(chars)

    blocks: list[str] = []
    offset = 0
    while offset < total_len:
        t = total_len - offset if offset + chunk_size > total_len else chunk_size

        B: list[int] = []
        for u in range(t):
            B.append(chars[offset + u])
        B.append(0)

        r = max(8, digit_size - 3 - t)
        for _ in range(r):
            B.append(_random.randint(1, 254))

        while len(B) < digit_size - 2:
            B.append(0)
        B.append(2)
        B.append(0)

        val = 0
        j = 0
        k = 0
        while k < digit_size:
            low = B[k]
            k += 1
            high = B[k] if k < digit_size else 0
            k += 1
            val |= (low + (high << 8)) << (j * 16)
            j += 1

        encrypted = pow(val, exponent, modulus)
        blocks.append(_bi_to_hex(encrypted))
        offset += chunk_size

    blocks.reverse()
    return "".join(blocks)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class LoginError(RuntimeError):
    """Raised when the login flow cannot complete."""


def _jsonp_parse(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass

    start = text.find("(")
    end = text.rfind(")")
    if start != -1 and end != -1 and end > start:
        inner = text[start + 1 : end]
        try:
            return json.loads(inner)
        except (json.JSONDecodeError, ValueError):
            pass

    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end != -1 and brace_end > brace_start:
        inner = text[brace_start : brace_end + 1]
        inner = inner.replace("\\", "")
        try:
            return json.loads(inner)
        except (json.JSONDecodeError, ValueError):
            pass

    raise ValueError(f"JSONP parse failed: {text[:500]}")


def _jquery_encode(params: dict, prefix: str = "") -> str:
    parts = []
    for k, v in params.items():
        full_key = f"{prefix}[{k}]" if prefix else k
        if isinstance(v, dict):
            parts.append(_jquery_encode(v, full_key))
        else:
            parts.append(f"{quote(full_key)}={quote(str(v))}")
    return "&".join(parts)


def _filter_validate(result: dict) -> dict:
    return {k: v for k, v in result.items() if k in _VALIDATE_KEYS}


# ---------------------------------------------------------------------------
# captcha solver
# ---------------------------------------------------------------------------


def _solve_slide_only(captcha_id: str = "", print_fn=None) -> dict:
    """Get a slide-type captcha solution. Retries until slide appears."""
    from .geeked import Geeked

    _cid = captcha_id or _FIXED_CAPTCHA_ID

    def _log(msg: str) -> None:
        if print_fn:
            print_fn(msg)

    for attempt in range(1, _MAX_SLIDE_RETRIES + 1):
        gk = Geeked(_cid, base_url=_GEETEST_BASE_URL)
        gk.risk_type = "slide"
        gk.challenge = str(uuid.uuid4())
        gk.callback = Geeked.random()
        try:
            data = gk.load_captcha()
            detected = gk._detect_type(data)
            if detected != "slide":
                _log(f"  [captcha] 第{attempt}次 类型={detected}(非slide)，重试...")
                time.sleep(_RETRY_DELAY_SEC)
                continue
            gk.lot_number = data["lot_number"]
            result = gk.submit_captcha(data)
            _log(f"  [captcha] 第{attempt}次 slide 求解成功")
            return result
        except Exception as exc:
            err_msg = str(exc)
            if "dddd_service" in err_msg:
                time.sleep(_RETRY_DELAY_SEC)
                continue
            if "connect_error" in err_msg or "ConnectionError" in err_msg or "RemoteDisconnected" in err_msg:
                _log(f"  [captcha] 第{attempt}次 连接错误，重试...")
                time.sleep(_RETRY_DELAY_SEC * 2)
                continue
            if attempt >= _MAX_SLIDE_RETRIES:
                raise LoginError(f"验证码求解失败(已重试{_MAX_SLIDE_RETRIES}次): {exc}") from exc
            _log(f"  [captcha] 第{attempt}次 错误: {err_msg[:80]}，重试...")
            time.sleep(_RETRY_DELAY_SEC)
    raise LoginError(f"未能获取 slide 类型验证码(已重试{_MAX_SLIDE_RETRIES}次)")


# ---------------------------------------------------------------------------
# main login orchestrator
# ---------------------------------------------------------------------------


def http_login(username: str, password: str, timeout: int = 60, print_fn=None) -> Dict[str, str]:
    """Pure-HTTP login. Returns a cookie dict on success."""

    def _log(msg: str) -> None:
        if print_fn:
            print_fn(msg)

    session = curl_requests.Session(impersonate="chrome124")
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/147.0.0.0 Safari/537.36"
            ),
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        }
    )
    session.timeout = timeout
    device_id = str(uuid.uuid4())

    # 0. Warm up
    _log("[0/8] 预热 zhixue.com 会话...")
    session.get(f"{ZHIXUE_BASE_URL}/wap_login.html")

    # 1. Get SSO URLs
    _log("[1/8] 获取 SSO 地址...")
    svc = session.get(f"{ZHIXUE_BASE_URL}/login/getServiceUrl").json()
    cas_url: str = svc["casUrl"].rstrip("/")
    service_url: str = svc["serviceUrl"]

    # 2. Solve captcha #1
    _log("[2/8] 求解验证码 #1 (预登录)...")
    captcha1 = _solve_slide_only(captcha_id=_FIXED_CAPTCHA_ID, print_fn=print_fn)
    validate1 = _filter_validate(captcha1)

    # 3. Pre-login (jQuery-style bracketed form encoding)
    _log("[3/8] 预登录...")
    rc4_pwd = rc4_encrypt_password(password)
    body = _jquery_encode(
        {
            "loginName": username,
            "password": rc4_pwd,
            "description": "encrypt",
            "appId": APP_ID,
            "captchaType": CAPTCHA_TYPE,
            "deviceName": "web",
            "client": CLIENT,
            "deviceId": device_id,
            "thirdCaptchaExtInfo": validate1,
        }
    )
    resp = session.post(
        f"{ZHIXUE_BASE_URL}/edition/login?from=wap_login",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    pre = resp.json()
    if pre.get("result") != "success":
        raise LoginError(f"预登录失败: {pre.get('message', pre)}")
    user_id: str = pre["data"]["userId"]
    captcha_id2: str = pre["data"]["captchaId"]

    # 4. Solve captcha #2
    _log(f"[4/8] 求解验证码 #2 (SSO)...")
    captcha2 = _solve_slide_only(captcha_id=_FIXED_CAPTCHA_ID, print_fn=print_fn)
    validate2 = _filter_validate(captcha2)

    # 5. SSO getSingleAt
    _log("[5/8] SSO 认证 (getSingleAt)...")
    rsa_pwd = rsa_encrypt_r2p(password)
    third_param = json.dumps(validate2, ensure_ascii=False)

    params = {
        "appId": APP_ID,
        "client": CLIENT,
        "mac": device_id,
        "service": service_url,
        "extInfo": json.dumps({"deviceId": device_id}),
        "type": LOGIN_TYPE,
        "username": username,
        "password": rsa_pwd,
        "encodeType": _ENCODE_TYPE,
        "encode": "true",
        "key": "auto",
        "captchaId": captcha_id2,
        "captchaType": CAPTCHA_TYPE,
        "thirdCaptchaParam": third_param,
        "version": ACCOUNT_VERSION,
    }
    full_url = f"{cas_url}/v1/getSingleAt?{urlencode(params)}"
    resp = session.get(full_url)
    at_data = _jsonp_parse(resp.text)
    if at_data.get("code") != "success" or not at_data.get("data", {}).get("at"):
        raise LoginError(
            f"getSingleAt 失败: {json.dumps(at_data, ensure_ascii=False)[:500]}"
        )
    at_token = at_data["data"]["at"]
    at_service = at_data["data"].get("service", "")

    # 6. SSO ticket exchange
    _log("[6/8] SSO 获取 ST (via atLogin)...")
    session.headers.pop("X-Requested-With", None)
    session.headers["Referer"] = f"{ZHIXUE_BASE_URL}/"
    session.headers["Accept"] = "*/*"

    if at_service:
        resp = session.get(at_service, allow_redirects=True)
    else:
        resp = session.get(
            f"{cas_url}/login?service={quote(service_url, safe='')}",
            allow_redirects=True,
        )

    resp = session.get(
        f"https://open.changyan.com/sso/login?sso_from=zhixuesso&service={quote(service_url, safe='')}",
        allow_redirects=False,
    )
    st_data = _jsonp_parse(resp.text.strip())

    if st_data.get("code") == 1001 and st_data.get("data", {}).get("st"):
        st = st_data["data"]["st"]
        session.headers["X-Requested-With"] = "XMLHttpRequest"
        session.headers["Accept"] = "application/json, text/javascript, */*; q=0.01"
        session.post(service_url, data={"action": "login", "ticket": st})

    session.headers["X-Requested-With"] = "XMLHttpRequest"
    session.headers["Accept"] = "application/json, text/javascript, */*; q=0.01"

    # 7. Finalize
    _log("[7/8] 调用 loginSuccess...")
    session.post(f"{ZHIXUE_BASE_URL}/loginSuccess/", data={"userId": user_id})

    # 8. Build cookie dict
    cookies: Dict[str, str] = {}
    for cookie in session.cookies.jar:
        if cookie.domain and "zhixue.com" in cookie.domain:
            cookies[cookie.name] = cookie.value
    for cookie in session.cookies.jar:
        if cookie.name not in cookies:
            cookies[cookie.name] = cookie.value
    if not cookies.get("loginUserName"):
        cookies["loginUserName"] = username

    _log(f"Cookie 登录成功! 获得 {len(cookies)} 个 cookie")
    return cookies
