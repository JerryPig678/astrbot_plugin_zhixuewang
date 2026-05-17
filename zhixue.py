import asyncio
import json
import os
import re
import random
import time
import hashlib

import httpx
from zhixuewang import login_cookie

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
BINDINGS_FILE = os.path.join(DATA_DIR, "bindings.json")
SCORES_FILE = os.path.join(DATA_DIR, "scores.json")
COOKIES_DIR = os.path.join(DATA_DIR, "cookies")

CAPTCHA_ID = "a6474422e78e5bb048082ec77d141068"
MAX_RETRIES = 8


def _safe_name(username: str) -> str:
    return hashlib.md5(username.encode()).hexdigest()[:16]


def _cookie_file(username: str) -> str:
    return os.path.join(COOKIES_DIR, f"{_safe_name(username)}.json")


def load_cookies_for(username: str) -> dict | None:
    cf = _cookie_file(username)
    if os.path.exists(cf):
        with open(cf, "r", encoding="utf-8") as f:
            return json.load(f)
    return None


def save_cookies_for(username: str, cookies: dict):
    os.makedirs(COOKIES_DIR, exist_ok=True)
    with open(_cookie_file(username), "w", encoding="utf-8") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


def load_bindings() -> dict:
    if os.path.exists(BINDINGS_FILE):
        with open(BINDINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_bindings(bindings: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(BINDINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(bindings, f, ensure_ascii=False, indent=2)


def load_scores() -> dict:
    if os.path.exists(SCORES_FILE):
        with open(SCORES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_scores(scores: dict):
    with open(SCORES_FILE, "w", encoding="utf-8") as f:
        json.dump(scores, f, ensure_ascii=False, indent=2)


def _detect_type(data: dict) -> str:
    if "bg" in data:
        return "slide"
    if "imgs" in data:
        return "icon"
    if isinstance(data.get("ques"), list) and data["ques"] and isinstance(data["ques"][0], list):
        return "gobang"
    return "ai"


async def playwright_login(username: str, password: str, print_fn=None) -> dict:
    from playwright.async_api import async_playwright
    from geeked.sign import Signer
    from geeked.slide import SlideSolver
    from urllib.parse import urlparse, parse_qs, urlunparse, urlencode

    def log(msg):
        if print_fn:
            print_fn(msg)

    load_data = None
    captcha_type = None
    browser_captcha_id = None

    async def handle_load(route):
        nonlocal load_data, captcha_type, browser_captcha_id
        url = route.request.url
        if "captcha_id" not in url:
            await route.continue_()
            return
        m = re.search(r"captcha_id=([a-f0-9]+)", url)
        if m:
            browser_captcha_id = m.group(1)
        response = await route.fetch()
        body = await response.text()
        json_match = re.search(r"\((\{.*\})\)\s*$", body, re.DOTALL)
        if json_match:
            jsonp_data = json.loads(json_match.group(1))
            load_data = jsonp_data.get("data", jsonp_data)
            captcha_type = _detect_type(load_data)
        await route.fulfill(response=response)

    async def handle_verify(route):
        nonlocal load_data, captcha_type, browser_captcha_id
        try:
            url = route.request.url
            parsed = urlparse(url)
            params = dict(parse_qs(parsed.query))
            params = {k: v[0] for k, v in params.items()}
            w = Signer.generate_w(load_data, browser_captcha_id, captcha_type)
            params["w"] = w
            new_query = urlencode(params)
            new_url = urlunparse(parsed._replace(query=new_query))
            response = await route.fetch(url=new_url)
            await route.fulfill(response=response)
        except Exception as e:
            log(f"    [verify] 失败: {e}")
            await route.continue_()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--disable-blink-features=AutomationControlled",
                  "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage"],
        )
        context = await browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/147.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
        )
        await context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
        """)
        page = await context.new_page()
        await page.route("**/xunfei.geetest.com/load*", handle_load)
        await page.route("**/xunfei.geetest.com/verify*", handle_verify)

        login_success = False
        for attempt in range(1, MAX_RETRIES + 1):
            load_data = None
            captcha_type = None
            browser_captcha_id = None
            log(f"    第{attempt}/{MAX_RETRIES}次尝试...")
            await page.goto("https://www.zhixue.com/wap_login.html", wait_until="networkidle")
            await page.fill("#txtUserName", username)
            await page.fill("#txtPassword", password)
            await asyncio.sleep(0.5)
            await page.click("#signup_button")
            await asyncio.sleep(3)

            if captcha_type and captcha_type != "slide":
                log(f"    非slide({captcha_type})，重试...")
                continue

            if captcha_type == "slide":
                bg_url = f"https://static.geetest.com/{load_data['bg']}"
                slice_url = f"https://static.geetest.com/{load_data['slice']}"
                bg_img = httpx.get(bg_url, timeout=15).content
                slice_img = httpx.get(slice_url, timeout=15).content
                distance = SlideSolver(slice_img, bg_img).find_puzzle_piece_position()
                log(f"    slide distance={distance:.1f}")

                slider = None
                box = None
                for sel in [".geetest_btn", ".geetest_slider_button",
                            "[class*='slider_button']", "[class*='geetest_slider']"]:
                    try:
                        loc = page.locator(sel).first
                        if await loc.count() > 0:
                            b = await loc.bounding_box()
                            if b and b["width"] > 10 and b["height"] > 10:
                                slider = loc
                                box = b
                                break
                    except Exception:
                        continue

                if slider and box:
                    sx = box["x"] + box["width"] / 2
                    sy = box["y"] + box["height"] / 2
                    await page.mouse.move(sx, sy)
                    await page.mouse.down()
                    steps = random.randint(30, 50)
                    for i in range(1, steps + 1):
                        p = i / steps
                        await page.mouse.move(
                            sx + distance * (p * (2 - p)),
                            sy + random.uniform(-2, 2)
                        )
                        await asyncio.sleep(random.uniform(0.005, 0.015))
                    await page.mouse.move(sx + distance + random.uniform(0, 3),
                                          sy + random.uniform(-1, 1))
                    await page.mouse.up()
                    await asyncio.sleep(3)

            try:
                await page.wait_for_url("https://www.zhixue.com/htm-vessel/**", timeout=90000)
                login_success = True
                break
            except Exception:
                continue

        cookies = await page.context.cookies()
        cookies_dict = {c.get("name"): c.get("value") for c in cookies}
        cookies_dict["loginUserName"] = username
        await browser.close()

        if not login_success:
            raise RuntimeError("Playwright登录失败")

        return cookies_dict


class ZhiXueManager:
    def __init__(self, log_fn=None):
        self.log = log_fn or print
        self._playwright_lock = asyncio.Lock()

    def _get_user_config(self, username: str) -> dict | None:
        raise NotImplementedError

    def _find_username(self, qq_id: str) -> str | None:
        bindings = load_bindings()
        return bindings.get(qq_id)

    def _find_credential(self, username: str, config_users: list) -> tuple | None:
        for u in config_users:
            if u.get("username") == username:
                return username, u.get("password", "")
        return None

    def bound_users_count(self, config_users: list) -> int:
        bindings = load_bindings()
        count = 0
        usernames = {u.get("username") for u in config_users}
        for qq_id, username in bindings.items():
            if username in usernames:
                count += 1
        return count

    def get_bindings_info(self, config_users: list) -> list:
        bindings = load_bindings()
        usernames = {u.get("username"): u for u in config_users}
        result = []
        for qq_id, username in bindings.items():
            result.append((qq_id, username, username in usernames))
        return result

    def bind_user(self, qq_id: str, username: str, config_users: list) -> bool:
        for u in config_users:
            if u.get("username") == username:
                bindings = load_bindings()
                bindings[qq_id] = username
                save_bindings(bindings)
                return True
        return False

    def unbind_user(self, qq_id: str):
        bindings = load_bindings()
        if qq_id in bindings:
            del bindings[qq_id]
            save_bindings(bindings)

    async def get_account(self, qq_id: str, config_users: list) -> tuple:
        username = self._find_username(qq_id)
        if not username:
            raise ValueError("未绑定账号，请先使用 /zx bind <用户名> 绑定")

        cred = self._find_credential(username, config_users)
        if not cred:
            raise ValueError(f"账号 {username} 不在配置列表中，请联系管理员")

        username, password = cred

        cookies = load_cookies_for(username)
        if cookies:
            try:
                account = login_cookie(cookies)
                return account, "cookie"
            except Exception:
                self.log(f"    用户 {qq_id}({username}) 旧Cookie失效，重新登录...")

        self.log(f"    用户 {qq_id}({username}) 启动Playwright登录...")
        async with self._playwright_lock:
            new_cookies = await playwright_login(username, password, print_fn=self.log)
            save_cookies_for(username, new_cookies)
            account = login_cookie(new_cookies)
            return account, "playwright"

    async def get_exams(self, qq_id: str, config_users: list) -> list:
        account, _ = await self.get_account(qq_id, config_users)
        exams = account.get_exams()
        result = []
        for exam in exams:
            result.append({
                "id": getattr(exam, "id", ""),
                "name": getattr(exam, "name", str(exam)),
                "is_final": getattr(exam, "is_final", False),
                "grade_code": getattr(exam, "grade_code", ""),
            })
        return result

    async def get_marks(self, qq_id: str, config_users: list, exam_name: str = None) -> tuple:
        account, _ = await self.get_account(qq_id, config_users)
        user_name = account.name
        if exam_name:
            exam = None
            for e in account.get_exams():
                en = getattr(e, "name", str(e))
                if exam_name in en:
                    exam = e
                    break
            if exam is None:
                raise ValueError(f"未找到考试: {exam_name}")
            marks = account.get_self_mark(exam)
        else:
            marks = account.get_self_mark()
        subjects = []
        if marks:
            for m in marks:
                subjects.append({
                    "subject": getattr(m.subject, "name", getattr(m, "subject_name", "未知")),
                    "score": getattr(m, "score", None),
                    "standard_score": getattr(m, "standard_score", None),
                    "class_rank": getattr(m, "class_rank", None),
                    "grade_rank": getattr(m, "grade_rank", None),
                })
        exam_info = {}
        if marks and len(marks) > 0:
            e = getattr(marks[0], "exam", None)
            if e:
                exam_info = {"name": getattr(e, "name", "未知"), "id": getattr(e, "id", "")}
            else:
                exam_info = exam_name and {"name": exam_name} or {}
        return user_name, exam_info, subjects

    async def check_new_scores(self, qq_id: str, config_users: list) -> list:
        account, _ = await self.get_account(qq_id, config_users)
        username = self._find_username(qq_id)
        all_scores = load_scores()
        user_key = username or qq_id
        user_scores = all_scores.get(user_key, {})

        new_items = []
        try:
            marks = account.get_self_mark()
            if marks:
                e = getattr(marks[0], "exam", None)
                exam_name = getattr(e, "name", "") if e else "latest"
                if exam_name not in user_scores:
                    subs = []
                    for m in marks:
                        subs.append({
                            "subject": getattr(m.subject, "name", "?"),
                            "score": getattr(m, "score", None),
                        })
                    new_items.append({"exam": exam_name, "subjects": subs})
                    user_scores[exam_name] = {"timestamp": time.time()}
                    all_scores[user_key] = user_scores
                    save_scores(all_scores)
        except Exception:
            pass

        return new_items


zhixue_manager = ZhiXueManager()


def format_exams_table(exams: list) -> str:
    if not exams:
        return "暂无考试数据"
    lines = ["考试列表：", ""]
    for i, e in enumerate(exams, 1):
        name = e.get("name", "?")
        is_final = " [期末]" if e.get("is_final") else ""
        lines.append(f"  {i}. {name}{is_final}")
    lines.append("")
    lines.append("发送 /zx marks <考试名> 查询对应成绩")
    return "\n".join(lines)


def format_marks_table(user_name: str, exam_name: str, subjects: list) -> str:
    if not subjects:
        return f"{user_name} 暂无 {exam_name} 成绩数据"
    lines = [f"【{user_name}】 {exam_name}"]
    total = 0
    count = 0
    for s in subjects:
        subj = s.get("subject", "?")
        score = s.get("score")
        score_str = f"{score:.1f}" if score is not None else "-"
        extra = []
        if s.get("class_rank"):
            extra.append(f"班排{s['class_rank']}")
        extra_str = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"  {subj}: {score_str}{extra_str}")
        if score is not None:
            total += score
            count += 1
    if count > 0:
        lines.append(f"  总分: {total:.1f}")
    return "\n".join(lines)