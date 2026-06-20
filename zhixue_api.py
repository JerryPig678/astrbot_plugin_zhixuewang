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
import time
import uuid
from dataclasses import dataclass, field
from typing import List, Optional, Union

from curl_cffi import requests

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
        self._set_exam_rank(mark)
        return mark

    def _set_exam_rank(self, mark: Mark):
        try:
            year = mark.exam.academic_year
            if not year.begin_time:
                return
            r = self._session.get(
                _GET_EXAM_LEVEL_TREND_URL,
                params={
                    "examId": mark.exam.id,
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
                params={"examId": mark.exam.id},
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
            pass  # rank info is best-effort


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
