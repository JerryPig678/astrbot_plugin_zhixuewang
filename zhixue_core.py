import asyncio
import json
import logging
import hashlib
import os
import time

from .zhixue_api import login_cookie

logger = logging.getLogger(__name__)

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(PLUGIN_DIR, "data")
ACCOUNTS_FILE = os.path.join(DATA_DIR, "accounts.json")
BINDINGS_FILE = os.path.join(DATA_DIR, "bindings.json")  # legacy, for migration
SCORES_FILE = os.path.join(DATA_DIR, "scores.json")
COOKIES_DIR = os.path.join(DATA_DIR, "cookies")


_WATCH_WINDOW = 3  # 监听窗口：最近 3 场考试


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
    """Legacy: load old bindings.json for migration."""
    if os.path.exists(BINDINGS_FILE):
        with open(BINDINGS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def load_accounts(config_users: list = None) -> dict:
    """Load accounts.json. Migrates from bindings.json if needed.

    Filters out usernames not in config_users (if provided).
    """
    if not os.path.exists(ACCOUNTS_FILE):
        # Try migration from legacy bindings.json
        if os.path.exists(BINDINGS_FILE):
            return _migrate_bindings(config_users)
        return {}

    with open(ACCOUNTS_FILE, "r", encoding="utf-8") as f:
        accounts = json.load(f)

    # Filter invalid usernames
    if config_users:
        valid_usernames = {u.get("username") for u in config_users}
        removed = [un for un in accounts if un not in valid_usernames]
        for un in removed:
            logger.warning(f"[accounts] 账号 {un} 不在配置中，已清理绑定")
            del accounts[un]
        if removed:
            save_accounts(accounts)

    return accounts


def _migrate_bindings(config_users: list = None) -> dict:
    """Migrate legacy bindings.json to accounts.json."""
    old = load_bindings()
    if not old:
        return {}

    accounts = {}
    for user_id, username in old.items():
        if isinstance(username, dict):
            username = username.get("username", "")
        if not username:
            continue
        if config_users and username not in {u.get("username") for u in config_users}:
            logger.warning(f"[accounts] 迁移时跳过无效账号: {username}")
            continue
        if username not in accounts:
            accounts[username] = {"bound_ids": []}
        accounts[username]["bound_ids"].append({
            "user_id": user_id,
            "platform_id": "aiocqhttp",  # default for legacy data
        })

    save_accounts(accounts)
    logger.info(f"[accounts] 从 bindings.json 迁移了 {len(accounts)} 个账号")
    return accounts


def save_accounts(accounts: dict):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(ACCOUNTS_FILE, "w", encoding="utf-8") as f:
        json.dump(accounts, f, ensure_ascii=False, indent=2)


def find_username_by_id(accounts: dict, user_id: str) -> str | None:
    """Find which username a user_id is bound to."""
    for username, data in accounts.items():
        for binding in data.get("bound_ids", []):
            if binding.get("user_id") == user_id:
                return username
    return None


def get_bound_ids(accounts: dict, username: str) -> list:
    """Get all bound IDs for a username."""
    return accounts.get(username, {}).get("bound_ids", [])


def bind_user_to_account(accounts: dict, user_id: str, username: str, platform_id: str) -> bool:
    """Bind a user_id to a username. Removes user_id from any other account first."""
    # Remove from existing bindings
    for un, data in accounts.items():
        data["bound_ids"] = [
            b for b in data.get("bound_ids", [])
            if b.get("user_id") != user_id
        ]

    # Add to target account
    if username not in accounts:
        accounts[username] = {"bound_ids": []}
    accounts[username]["bound_ids"].append({
        "user_id": user_id,
        "platform_id": platform_id,
    })
    save_accounts(accounts)
    return True


def unbind_user_from_account(accounts: dict, user_id: str):
    """Remove a user_id from all accounts."""
    for un, data in accounts.items():
        data["bound_ids"] = [
            b for b in data.get("bound_ids", [])
            if b.get("user_id") != user_id
        ]
    save_accounts(accounts)


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
    _EXAMS_CACHE_TTL = 300  # 5 minutes

    def __init__(self, log_fn=None):
        self.log = log_fn or print
        self._login_lock = asyncio.Lock()
        self._exams_cache: dict = {}  # username -> (exams_list, timestamp)

    def _find_username(self, user_id: str, config_users: list = None) -> str | None:
        accounts = load_accounts(config_users)
        return find_username_by_id(accounts, user_id)

    def _find_credential(self, username: str, config_users: list) -> tuple | None:
        for u in config_users:
            if u.get("username") == username:
                return username, u.get("password", "")
        return None

    def bind_user(self, user_id: str, username: str, config_users: list, platform_id: str = "unknown") -> bool:
        for u in config_users:
            if u.get("username") == username:
                accounts = load_accounts(config_users)
                bind_user_to_account(accounts, user_id, username, platform_id)
                return True
        return False

    def unbind_user(self, user_id: str, config_users: list = None):
        accounts = load_accounts(config_users)
        unbind_user_from_account(accounts, user_id)

    async def get_account(self, user_id: str, config_users: list) -> tuple:
        username = self._find_username(user_id, config_users)
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
                self.log(f"    用户 {user_id}({username}) 旧Cookie失效，重新登录...")

        self.log(f"    用户 {user_id}({username}) 启动纯HTTP登录...")
        async with self._login_lock:
            new_cookies = await asyncio.to_thread(
                _http_login, username, password, print_fn=self.log
            )
            save_cookies_for(username, new_cookies)
            account = login_cookie(new_cookies)
            return account, "http"

    def _get_cached_exams(self, account) -> list:
        """Get exams list with 5-minute cache per account name."""
        name = account.name or "default"
        now = time.time()
        if name in self._exams_cache:
            exams, ts = self._exams_cache[name]
            if now - ts < self._EXAMS_CACHE_TTL:
                return exams
        exams_raw = account.get_exams()
        exams = []
        for exam in exams_raw:
            exams.append({
                "id": getattr(exam, "id", ""),
                "name": getattr(exam, "name", str(exam)),
                "is_final": getattr(exam, "is_final", False),
                "grade_code": getattr(exam, "grade_code", ""),
                "_exam_obj": exam,
            })
        self._exams_cache[name] = (exams, now)
        return exams

    async def get_exams(self, user_id: str, config_users: list) -> list:
        account, _ = await self.get_account(user_id, config_users)
        return self._get_cached_exams(account)

    async def get_marks(self, user_id: str, config_users: list, exam_param: str = None) -> tuple:
        account, _ = await self.get_account(user_id, config_users)
        user_name = account.name
        exam = self._resolve_exam_or_latest(account, exam_param)

        papers, total_raw = account.get_report_main(exam)

        subjects = []
        total_info = None
        for p in papers:
            if p["subject_code"] == "99":
                total_info = {
                    "name": p["subject_name"],
                    "score": p["score"],
                    "standard_score": p["standard_score"],
                }
                continue
            subjects.append({
                "subject": p["subject_name"],
                "score": p["score"],
                "standard_score": p["standard_score"],
                "class_rank": 0,
                "grade_rank": 0,
            })

        # Prefer totalScore from API root (more reliable)
        if total_raw:
            total_info = {
                "name": total_raw.get("subjectName", "总分"),
                "score": total_raw["userScore"],
                "standard_score": total_raw["standardScore"],
            }

        if total_info is None and subjects:
            total = sum(s["score"] for s in subjects if s.get("score") is not None)
            total_info = {"name": "总分", "score": total, "standard_score": None}

        # Rank info is best-effort (builds Mark internally, catches failures)
        try:
            from .zhixue_api import Mark, SubjectScore, Subject
            mark = Mark(exam=exam, person_name=user_name)
            for p in papers:
                if p["subject_code"] == "99":
                    continue
                mark.append(SubjectScore(
                    score=p["score"],
                    subject=Subject(
                        id=p["paper_id"], name=p["subject_name"],
                        code=p["subject_code"], standard_score=p["standard_score"],
                        exam_id=exam.id,
                    ),
                ))
            account._set_exam_rank(exam, mark)
            for m in mark:
                for s in subjects:
                    if s["subject"] == m.subject.name:
                        s["class_rank"] = m.class_rank
                        s["grade_rank"] = m.grade_rank
        except Exception:
            logger.debug(f"获取排名信息失败: user={user_id}", exc_info=True)

        exam_info = {"name": exam.name, "id": exam.id}
        return user_name, exam_info, subjects, total_info

    async def get_sheet(self, user_id: str, config_users: list, subject_name: str, exam_param: str = None) -> tuple[str, list[str]]:
        """获取答题卡图片临时文件路径。返回 (sheet_name, [path1, path2, ...])。"""
        account, _ = await self.get_account(user_id, config_users)
        exam = self._resolve_exam_or_latest(account, exam_param)

        papers, _ = account.get_report_main(exam)

        # 匹配学科
        paper_id = None
        sheet_name = ""
        for p in papers:
            if subject_name in p["subject_name"] or p["subject_name"] == subject_name:
                paper_id = p["paper_id"]
                sheet_name = p["subject_name"]
                break

        if not paper_id:
            names = ", ".join(p["subject_name"] for p in papers)
            raise ValueError(f"未找到「{subject_name}」，该考试包含：{names}")

        temp_paths = await asyncio.to_thread(
            account.download_sheet_images, exam.id, paper_id
        )
        if not temp_paths:
            raise ValueError(f"「{sheet_name}」答题卡图片暂未上传")
        return sheet_name, temp_paths

    def _resolve_exam_or_latest(self, account, exam_param: str = None):
        """解析考试参数（序号或名称），返回 Exam 对象。None 时取最新考试。"""
        if exam_param is None:
            exam = account.get_latest_exam()
            if not exam:
                raise ValueError("未找到任何考试")
            return exam
        exams = self._get_cached_exams(account)
        exam_str = str(exam_param)
        if exam_str.isdigit():
            idx = int(exam_str) - 1
            if idx < 0 or idx >= len(exams):
                raise ValueError(f"序号 {exam_param} 超出范围，共 {len(exams)} 场考试")
            return exams[idx].get("_exam_obj") or exams[idx]
        for e in exams:
            en = e.get("name", "")
            if exam_str in en:
                return e.get("_exam_obj") or e
        raise ValueError(f"未找到考试: {exam_param}")

    async def check_new_scores(self, user_id: str, config_users: list) -> list:
        """检查最近 3 场考试的成绩变化。

        检测两种变化：
          - 新考试出现（考试名不在本地记录中）
          - 已有考试有新学科或有学科分数变化（hash 签名变化）

        返回:
          [{"exam": str, "subjects": [{"subject": str, "score": float}], "change_type": "new_exam"|"updated"}]
        """
        account, _ = await self.get_account(user_id, config_users)
        username = self._find_username(user_id, config_users)
        user_key = username or user_id

        # 1. 取最近 3 场考试
        all_exams = account.get_exams()
        recent = all_exams[:_WATCH_WINDOW]
        if not recent:
            return []

        # 2. 取每场的学科成绩快照：exam_name -> {subject_name: score}
        current_snapshot: dict[str, dict[str, float]] = {}
        for exam in recent:
            try:
                marks = account.get_self_mark(exam)
                subjects: dict[str, float] = {}
                for m in marks:
                    code = getattr(m.subject, "code", "") if hasattr(m, "subject") else ""
                    if code == "99":
                        continue  # 跳过总分
                    name = getattr(m.subject, "name", "?")
                    score = getattr(m, "score", None)
                    if score is not None:
                        subjects[name] = score
                current_snapshot[exam.name] = subjects
            except Exception:
                logger.debug(f"[监听] 获取考试「{exam.name}」成绩失败", exc_info=True)

        if not current_snapshot:
            return []

        # 3. 对比本地记录
        all_scores = load_scores()
        saved = all_scores.get(user_key, {})
        new_items: list[dict] = []

        for exam_name, subjects in current_snapshot.items():
            if exam_name not in saved:
                # -- 全新考试 --
                subs = [{"subject": k, "score": v} for k, v in subjects.items()]
                new_items.append({"exam": exam_name, "subjects": subs, "change_type": "new_exam"})
                saved[exam_name] = subjects
            else:
                # -- 已有考试：逐学科对比 --
                old_subs = saved.get(exam_name, {})
                new_subs: list[dict] = []
                for subj, score in subjects.items():
                    if subj not in old_subs or old_subs[subj] != score:
                        new_subs.append({"subject": subj, "score": score})
                if new_subs:
                    new_items.append({"exam": exam_name, "subjects": new_subs, "change_type": "updated"})
                    saved[exam_name] = subjects  # 更新快照

        if new_items:
            all_scores[user_key] = saved
            save_scores(all_scores)

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
    lines.append("发送 /zx marks <序号或考试名> 查询对应成绩")
    return "\n".join(lines)


def format_marks_table(user_name: str, exam_name: str, subjects: list, total_info: dict = None) -> str:
    if not subjects:
        return f"{user_name} 暂无 {exam_name} 成绩数据"
    lines = [f"【{user_name}】 {exam_name}"]
    for s in subjects:
        subj = s.get("subject", "?")
        score = s.get("score")
        score_str = f"{score:.1f}" if score is not None else "-"
        extra = []
        if s.get("class_rank"):
            extra.append(f"班排{s['class_rank']}")
        extra_str = f" ({', '.join(extra)})" if extra else ""
        lines.append(f"  {subj}: {score_str}{extra_str}")
    if total_info and total_info.get("score") is not None:
        lines.append(f"  {total_info.get('name', '总分')}: {total_info['score']:.1f}")
    return "\n".join(lines)
