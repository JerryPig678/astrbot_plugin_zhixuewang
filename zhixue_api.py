"""Self-contained zhixuewang API client using curl_cffi.

Replaces the zhixuewang pip package (which depends on playwright).
Only implements the functions the plugin actually uses:
  - login_cookie: create account from cookies
  - get_exams: list exams
  - get_self_mark: get scores for an exam
  - get_latest_exam: get most recent exam
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Union

from curl_cffi import requests

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# URLs
# ---------------------------------------------------------------------------

_BASE = "https://www.zhixue.com"
_INFO_URL = f"{_BASE}/container/container/student/account/"
_XTOKEN_URL = f"{_BASE}/container/app/token/getToken"
_GET_ACADEMIC_YEAR_URL = f"{_BASE}/zhixuebao/base/common/academicYear"
_GET_EXAM_URL = f"{_BASE}/zhixuebao/report/exam/getUserExamList"
_GET_RECENT_EXAM_URL = f"{_BASE}/zhixuebao/report/exam/getRecentExam"
_GET_MARK_URL = f"{_BASE}/zhixuebao/report/exam/getReportMain"
_GET_EXAM_LEVEL_TREND_URL = f"{_BASE}/zhixuebao/report/exam/getLevelTrend"
_GET_PAPER_LEVEL_TREND_URL = f"{_BASE}/zhixuebao/report/paper/getLevelTrend"
_GET_SUBJECT_DIAGNOSIS = f"{_BASE}/zhixuebao/report/exam/getSubjectDiagnosis"
_GET_SHEET_URL = f"{_BASE}/zhixuebao/report/checksheet/"

_MD5_SECRET = "iflytek!@#123student"

# ---------------------------------------------------------------------------
# data models
# ---------------------------------------------------------------------------


@dataclass
class School:
    id: str = ""
    name: str = ""


@dataclass
class Grade:
    code: str = ""
    name: str = ""


@dataclass
class StuClass:
    id: str = ""
    name: str = ""
    school: School = field(default_factory=School)
    grade: Grade = field(default_factory=Grade)


@dataclass
class AcademicYear:
    name: str = ""
    code: str = ""
    begin_time: int = 0
    end_time: int = 0


@dataclass
class Subject:
    id: str = ""
    name: str = ""
    code: str = ""
    standard_score: float = 0
    exam_id: str = ""


@dataclass
class SubjectScore:
    score: float = 0
    subject: Subject = field(default_factory=Subject)
    class_rank: int = 0
    grade_rank: int = 0


@dataclass
class Exam:
    id: str = ""
    name: str = ""
    grade_code: str = ""
    is_final: bool = False
    create_time: float = 0
    class_rank: int = 0
    grade_rank: int = 0
    academic_year: AcademicYear = field(default_factory=AcademicYear)

    def __bool__(self):
        return bool(self.id)


class Mark(list):
    """A list of SubjectScore for one exam."""

    def __init__(self, exam: Optional[Exam] = None, person_name: str = ""):
        super().__init__()
        self.exam = exam or Exam()
        self.person_name = person_name


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# account
# ---------------------------------------------------------------------------


class StudentAccount:
    """Minimal student account that uses curl_cffi for all API calls."""

    def __init__(self, session: requests.Session):
        self._session = session
        self._auth_token: str = ""
        self._auth_ts: float = 0
        self.name: str = ""
        self.id: str = ""

    def _get_auth_header(self) -> dict:
        guid = str(uuid.uuid4())
        ts = str(int(time.time() * 1000))
        auth_token = _md5(guid + ts + _MD5_SECRET)

        if self._auth_token and time.time() - self._auth_ts < 600:
            return {
                "authbizcode": "0001",
                "authguid": guid,
                "authtimestamp": ts,
                "authtoken": auth_token,
                "XToken": self._auth_token,
            }

        r = self._session.get(
            _XTOKEN_URL,
            headers={
                "authbizcode": "0001",
                "authguid": guid,
                "authtimestamp": ts,
                "authtoken": auth_token,
            },
        )
        r.raise_for_status()
        self._auth_token = r.json()["result"]
        self._auth_ts = time.time()
        return self._get_auth_header()

    def _init_base_info(self):
        """Fetch student name, id, etc."""
        r = self._session.get(_INFO_URL)
        r.raise_for_status()
        data = r.json()["student"]
        self.name = data.get("name", "")
        self.id = data.get("id", "")
        return self

    def _get_academic_years(self) -> List[AcademicYear]:
        r = self._session.get(_GET_ACADEMIC_YEAR_URL, headers=self._get_auth_header())
        r.raise_for_status()
        years = []
        for y in r.json()["result"]:
            years.append(AcademicYear(
                name=y["name"], code=y["code"],
                begin_time=y["beginTime"], end_time=y["endTime"],
            ))
        return years

    def _get_latest_valid_year(self) -> AcademicYear:
        for year in self._get_academic_years():
            r = self._session.get(
                _GET_RECENT_EXAM_URL,
                params={"startSchoolYear": year.begin_time, "endSchoolYear": year.end_time},
                headers=self._get_auth_header(),
            )
            if r.json()["result"]:
                return year
        raise ValueError("没有找到有效的学年")

    def get_latest_exam(self) -> Exam:
        year = self._get_latest_valid_year()
        r = self._session.get(
            _GET_RECENT_EXAM_URL,
            params={"startSchoolYear": year.begin_time, "endSchoolYear": year.end_time},
            headers=self._get_auth_header(),
        )
        r.raise_for_status()
        info = r.json()["result"]["examInfo"]
        exam = Exam(
            id=info["examId"],
            name=info["examName"],
            grade_code=r.json()["result"].get("gradeCode", ""),
            is_final=info.get("isFinal", False),
            create_time=info.get("examCreateDateTime", 0),
            academic_year=year,
        )
        return exam

    def get_exams(self) -> List[Exam]:
        years = self._get_academic_years()
        exams: List[Exam] = []
        for year in years:
            page = 1
            has_next = True
            while has_next:
                r = self._session.get(
                    _GET_EXAM_URL,
                    params={
                        "pageIndex": page, "pageSize": 10,
                        "startSchoolYear": year.begin_time,
                        "endSchoolYear": year.end_time,
                    },
                    headers=self._get_auth_header(),
                )
                r.raise_for_status()
                data = r.json()["result"]
                for e in data["examList"]:
                    exam = Exam(
                        id=e["examId"], name=e["examName"],
                        create_time=e.get("examCreateDateTime", 0),
                        academic_year=year,
                    )
                    exams.append(exam)
                has_next = data["hasNextPage"]
                page += 1
        return exams

    def get_self_mark(self, exam: Optional[Exam] = None) -> Mark:
        if exam is None:
            exam = self.get_latest_exam()
        if not exam:
            return Mark()

        mark = Mark(exam=exam, person_name=self.name)
        r = self._session.get(
            _GET_MARK_URL,
            params={"examId": exam.id},
            headers=self._get_auth_header(),
        )
        r.raise_for_status()
        result = r.json()["result"]

        for paper in result["paperList"]:
            ss = SubjectScore(
                score=paper["userScore"],
                subject=Subject(
                    id=paper["paperId"],
                    name=paper["subjectName"],
                    code=paper["subjectCode"],
                    standard_score=paper["standardScore"],
                    exam_id=exam.id,
                ),
            )
            mark.append(ss)

        total = result.get("totalScore")
        if total:
            mark.append(SubjectScore(
                score=total["userScore"],
                subject=Subject(
                    id="", name=total["subjectName"], code="99",
                    standard_score=total["standardScore"], exam_id=exam.id,
                ),
            ))

        # Fetch rank info (best-effort)
        self._set_exam_rank(exam, mark)
        return mark

    def get_report_main(self, exam: Exam) -> tuple[list, dict | None]:
        """从 getReportMain 获取学科列表和总分数据。"""
        r = self._session.get(
            _GET_MARK_URL,
            params={"examId": exam.id},
            headers=self._get_auth_header(),
        )
        r.raise_for_status()
        result = r.json()["result"]
        papers = []
        for p in result["paperList"]:
            papers.append({
                "paper_id": p["paperId"],
                "subject_name": p["subjectName"],
                "subject_code": p["subjectCode"],
                "score": p["userScore"],
                "standard_score": p["standardScore"],
            })
        total_raw = result.get("totalScore")
        return papers, total_raw

    def get_sheet_payload(self, exam_id: str, paper_id: str) -> dict | None:
        """获取答题卡完整数据: 图片 URL + 定位数据 + 分步满分. 无答题卡时返回 None."""
        r = self._session.get(
            _GET_SHEET_URL,
            params={"examId": exam_id, "paperId": paper_id},
            headers=self._get_auth_header(),
        )
        r.raise_for_status()
        result = r.json().get("result")
        if not result:
            return None
        sheet_images_raw = result.get("sheetImages")
        if not sheet_images_raw:
            return None
        sheet_datas = {}
        try:
            sheet_datas = json.loads(result.get("sheetDatas") or "{}")
        except (ValueError, TypeError):
            pass
        return {
            "image_urls": json.loads(sheet_images_raw),
            "sheet_datas": sheet_datas,
            "step_datas": result.get("stepDatas") or [],
            "score": result.get("score"),
            "standard_score": result.get("standardScore"),
        }

    def get_sheet_images(self, exam_id: str, paper_id: str) -> list[str]:
        """获取答题卡图片 URL 列表。"""
        payload = self.get_sheet_payload(exam_id, paper_id)
        return payload["image_urls"] if payload else []

    def download_sheet_images(self, exam_id: str, paper_id: str, title: str = "") -> list[str]:
        """下载答题卡图片(批注渲染+总分栏+拼接长图)到临时文件，返回路径列表。调用方负责清理。"""
        import io
        import tempfile

        from PIL import Image

        from .answer_sheet import add_score_bar, annotate_page, build_score_map

        payload = self.get_sheet_payload(exam_id, paper_id)
        if not payload:
            return []

        raw_pages = []
        for url in payload["image_urls"]:
            img_resp = self._session.get(url)
            img_resp.raise_for_status()
            raw_pages.append(img_resp.content)

        score_map = build_score_map(payload["sheet_datas"], payload.get("step_datas"))
        pages = (payload["sheet_datas"].get("answerSheetLocationDTO") or {}).get("pageSheets") or []

        rendered: list = []
        for idx, raw in enumerate(raw_pages):
            img = Image.open(io.BytesIO(raw)).convert("RGB")
            if idx < len(pages):
                annotate_page(img, pages[idx], score_map)
            rendered.append(img)

        final = [
            add_score_bar(img, payload.get("score"), payload.get("standard_score"), title)
            for img in rendered
        ]

        temp_paths = []
        if final:
            max_w = max(p.width for p in final)
            scaled = []
            for p in final:
                if p.width != max_w:
                    p = p.resize((max_w, int(p.height * max_w / p.width)), Image.LANCZOS)
                scaled.append(p)
            total_h = sum(p.height for p in scaled)
            merged = Image.new("RGB", (max_w, total_h), "white")
            y = 0
            for p in scaled:
                merged.paste(p, (0, y))
                y += p.height
            buf = io.BytesIO()
            merged.save(buf, format="JPEG", quality=90)
            tmp = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
            tmp.write(buf.getvalue())
            tmp.close()
            temp_paths.append(tmp.name)
        return temp_paths

    def _set_exam_rank(self, exam: Exam, mark: Mark):
        """Fetch rank info and populate class_rank on each SubjectScore."""
        try:
            year = exam.academic_year
            if not year.begin_time:
                return
            r = self._session.get(
                _GET_EXAM_LEVEL_TREND_URL,
                params={
                    "examId": exam.id,
                    "pageIndex": 1, "pageSize": 1,
                    "startSchoolYear": year.begin_time,
                    "endSchoolYear": year.end_time,
                },
                headers=self._get_auth_header(),
            )
            data = r.json()
            if data.get("errorCode", -1) != 0:
                return
            total_num = 0
            lst = data.get("result", {}).get("list", [])
            if lst:
                total_num = lst[0]["dataList"][0]["totalNum"]

            r2 = self._session.get(
                _GET_SUBJECT_DIAGNOSIS,
                params={"examId": exam.id},
                headers=self._get_auth_header(),
            )
            data2 = r2.json()
            if data2.get("errorCode", -1) != 0:
                return
            for each in data2["result"]["list"]:
                for ss in mark:
                    if ss.subject.code == each["subjectCode"]:
                        ss.class_rank = round(total_num - (100 - each["myRank"]) / 100 * (total_num - 1))
        except Exception:
            logger.debug(f"获取排名信息失败 (exam={exam.id})", exc_info=True)


# ---------------------------------------------------------------------------
# public API — matches zhixuewang.login_cookie interface
# ---------------------------------------------------------------------------


def login_cookie(cookies: Union[dict, str]) -> StudentAccount:
    """Create a StudentAccount from cookies (replaces zhixuewang.login_cookie)."""
    session = requests.Session(impersonate="chrome124")
    session.headers["User-Agent"] = "Mozilla/5.0 (Windows NT 6.1; rv:2.0.1) Gecko/20100101 Firefox/4.0.1"
    session.trust_env = False

    if isinstance(cookies, str):
        cookies = dict(item.split("=") for item in cookies.split("; "))
    session.cookies.update(cookies)

    import base64
    uname = cookies.get("loginUserName", "")
    session.cookies.set("uname", base64.b64encode(uname.encode()).decode())

    account = StudentAccount(session)
    account._init_base_info()
    return account
