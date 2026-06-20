import asyncio
import json
import os
import hashlib
import time

from zhixuewang import login_cookie

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
BINDINGS_FILE = os.path.join(DATA_DIR, "bindings.json")
SCORES_FILE = os.path.join(DATA_DIR, "scores.json")
COOKIES_DIR = os.path.join(DATA_DIR, "cookies")


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


def _http_login(username: str, password: str, print_fn=None) -> dict:
    """Pure-HTTP login via plugin_login (no Playwright)."""
    from .plugin_login import http_login

    def _log(msg: str) -> None:
        if print_fn:
            print_fn(msg)

    _log(f"    [HTTP] 开始纯HTTP登录 {username}...")
    try:
        cookies = http_login(username, password, print_fn=print_fn)
        _log(f"    [HTTP] 登录成功, 获取 {len(cookies)} 个 cookie")
        return cookies
    except Exception as e:
        _log(f"    [HTTP] 登录失败: {e}")
        raise RuntimeError(f"纯HTTP登录失败: {e}") from e


class ZhiXueManager:
    def __init__(self, log_fn=None):
        self.log = log_fn or print
        self._login_lock = asyncio.Lock()

    def _find_username(self, qq_id: str) -> str | None:
        bindings = load_bindings()
        return bindings.get(qq_id)

    def _find_credential(self, username: str, config_users: list) -> tuple | None:
        for u in config_users:
            if u.get("username") == username:
                return username, u.get("password", "")
        return None

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

        self.log(f"    用户 {qq_id}({username}) 启动纯HTTP登录...")
        async with self._login_lock:
            new_cookies = await asyncio.to_thread(
                _http_login, username, password, print_fn=self.log
            )
            save_cookies_for(username, new_cookies)
            account = login_cookie(new_cookies)
            return account, "http"

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
