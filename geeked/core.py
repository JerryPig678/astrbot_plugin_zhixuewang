from uuid import uuid4
from curl_cffi import requests
import random, time, json
from .sign import Signer


class Geeked:
    def __init__(self, captcha_id: str, risk_type: str = None, base_url: str = "https://gcaptcha4.geevisit.com", **kwargs):
        self.pass_token = None
        self.lot_number = None
        self.captcha_id = captcha_id
        self.challenge = str(uuid4())
        self.risk_type = risk_type
        self.callback = Geeked.random()
        self.session = requests.Session(impersonate="chrome124", **kwargs)
        self.session.headers = {
            "connection": "keep-alive",
            "sec-ch-ua-platform": "\"Windows\"",
            "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36",
            "sec-ch-ua-mobile": "?0",
            "accept": "*/*",
            "sec-fetch-site": "same-origin",
            "sec-fetch-mode": "no-cors",
            "sec-fetch-dest": "script",
            "accept-encoding": "gzip, deflate, br, zstd",
            "accept-language": "en-US,en;q=0.9"
        }
        self.session.base_url = base_url.rstrip("/")

    @staticmethod
    def random() -> str:
        return f"geetest_{int(random.random() * 10000) + int(time.time() * 1000)}"

    def format_response(self, response: str) -> dict:
        return json.loads(response.split(f"{self.callback}(")[1][:-1])["data"]

    def load_captcha(self):
        params = {
            "captcha_id": self.captcha_id,
            "challenge": self.challenge,
            "client_type": "web",
            "risk_type": self.risk_type,
            "lang": "eng",
            "callback": self.callback,
        }
        res = self.session.get("/load", params=params)
        return self.format_response(res.text)

    def submit_captcha(self, data: dict) -> dict:
        self.callback = Geeked.random()

        params = {
            "callback": self.callback,
            "captcha_id": self.captcha_id,
            "client_type": "web",
            "lot_number": self.lot_number,
            "risk_type": self.risk_type,
            "payload": data["payload"],
            "process_token": data["process_token"],
            "payload_protocol": "1",
            "pt": "1",
            "w": Signer.generate_w(data, self.captcha_id, self.risk_type),
        }
        res = self.session.get("/verify", params=params).text
        res = self.format_response(res)

        if res.get("seccode") is None:
            raise Exception(f"Failed to submit captcha: {res}")

        return res["seccode"]

    @staticmethod
    def _detect_type(data: dict) -> str:
        if "bg" in data:
            return "slide"
        if "imgs" in data:
            return "icon"
        if isinstance(data.get("ques"), list) and data["ques"] and isinstance(data["ques"][0], list):
            return "gobang"
        return "ai"

    def solve(self) -> dict:
        if self.risk_type:
            data = self.load_captcha()
            self.lot_number = data["lot_number"]
            return self.submit_captcha(data)

        for try_type in ("slide", "icon", "gobang", "ai"):
            self.risk_type = try_type
            self.challenge = str(uuid4())
            self.callback = Geeked.random()
            try:
                data = self.load_captcha()
                detected = self._detect_type(data)
                if detected != try_type:
                    self.risk_type = detected
                self.lot_number = data["lot_number"]
                return self.submit_captcha(data)
            except Exception:
                continue
        raise Exception("Could not auto-detect risk_type; try specifying it manually")
