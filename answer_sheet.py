"""答题卡图片批注渲染.

链路: checksheet → sheetImages (OSS 签名 URL) → 下载
渲染: sheetDatas 的 pageSheets 区域坐标 (mm) + stepDatas 每小问满分
      + answerRecordDetails 每小问得分 → PIL 画批注 (复刻网页端效果):
      - 顶部红色总分大字信息栏
      - 错题红框 + 题级得分/总扣分标签
      - 分小问扣分明细 "16(2): -3/3" (两列布局, 与网页一致)
      - 老师逐点批注 (spotMark, 如 +1)

坐标说明: 区域坐标单位为毫米, 答题卡为 A3 横向 (420mm 宽),
px = mm * image_width / 420; branch 坐标有相对/绝对两种模板, 自动判别.
"""
from __future__ import annotations

import io
import json
from typing import Dict, List, Tuple

from PIL import Image, ImageDraw, ImageFont

# 答题卡纸张为 A3 横向
PAPER_W_MM = 420.0

RED = (225, 30, 30)
GREEN = (25, 135, 60)
GRAY = (110, 110, 110)
DARK = (55, 55, 55)

# 长图最大输出宽度
MAX_WIDTH = 1600

_FONT_CANDIDATES = {
    True: [  # bold
        "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ],
    False: [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    ],
}


def _load_font(size: int, bold: bool = False):
    for path in _FONT_CANDIDATES[bold]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_score_map(sheet_datas: dict, step_datas: list | None = None) -> Dict[int, dict]:
    """题号 -> {score, standard_score, is_correct, steps, spot_marks}.

    steps: [(得分, 满分或None)], 满分来自 result.stepDatas[].stepStandardScore
           (按 topicStartNum 题号 + stepNum 匹配); 有满分即可算扣分.
    spot_marks 来自 teacherMarkingRecords[].markingContent (逐点批注, 如 +1).
    """
    step_full: Dict[int, Dict[int, float]] = {}
    for sd in step_datas or []:
        try:
            t = int(sd.get("topicStartNum", 0))
            n = int(sd.get("stepNum", 0))
            step_full.setdefault(t, {})[n] = sd.get("stepStandardScore", 0.0)
        except (ValueError, TypeError, AttributeError):
            continue

    details = (sheet_datas.get("userAnswerRecordDTO") or {}).get("answerRecordDetails") or []
    score_map: Dict[int, dict] = {}
    for d in details:
        try:
            steps: List[Tuple[int, float, float | None]] = []
            spot_marks: List[dict] = []
            topic = int(d["dispTitle"])
            for sub in d.get("subTopics") or []:
                if not sub:
                    continue
                for st in sub.get("stepRecords") or []:
                    if st:
                        n = int(st.get("stepNum", 0))
                        sc = st.get("score", 0.0)
                        steps.append((n, sc, step_full.get(topic, {}).get(n)))
                for m in sub.get("teacherMarkingRecords") or []:
                    mc = m.get("markingContent")
                    if not mc or not isinstance(mc, str) or mc.strip() in ("", "[]"):
                        continue
                    try:
                        items = json.loads(mc)
                        if isinstance(items, str):
                            items = json.loads(items)
                        for it in items or []:
                            if isinstance(it, dict) and it.get("content"):
                                spot_marks.append(it)
                    except (ValueError, TypeError):
                        continue
            steps.sort()
            score_map[topic] = {
                "score": d.get("score", 0.0),
                "standard_score": d.get("standardScore", 0.0),
                "is_correct": d.get("isCorrect", False),
                "steps": [(s, f) for _, s, f in steps],
                "spot_marks": spot_marks,
            }
        except (ValueError, KeyError, TypeError):
            continue
    return score_map


def _draw_label(draw: ImageDraw.Draw, text: str, x: float, y: float, color, fsize: int):
    """在 (x, y) 画红/绿底白字标签, y 为标签顶部."""
    font = _load_font(fsize, bold=True)
    pad = max(4, fsize // 5)
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.rectangle([x, y, x + tw + pad * 2, y + th + pad * 2], fill=color)
    draw.text((x + pad, y + pad - bbox[1]), text, fill="white", font=font)
    return th + pad * 2


def _resolve_branch_mm(sec_pos: dict, br_pos: dict) -> Tuple[float, float]:
    """解析 branch 的绝对 mm 坐标.

    两种卡模板: branch 相对 section (b0=(0,0)) 或 绝对页面坐标
    (b0==section 原点). 判别: 相对解释若出 section 界则为绝对.
    """
    sx, sy = sec_pos.get("left", 0), sec_pos.get("top", 0)
    sw, sh = sec_pos.get("width", 0), sec_pos.get("height", 0)
    bl, bt = br_pos.get("left", 0), br_pos.get("top", 0)
    bw, bh = br_pos.get("width", 0), br_pos.get("height", 0)
    tol = 1.0
    rx, ry = sx + bl, sy + bt
    if rx < sx - tol or ry < sy - tol or rx + bw > sx + sw + tol or ry + bh > sy + sh + tol:
        return float(bl), float(bt)
    return rx, ry


def _draw_sub_scores(draw: ImageDraw.Draw, title: int, steps: List[Tuple[float, float | None]],
                     x: float, y: float, fsize: int) -> float:
    """分小问扣分, 网页同款两列布局: 16(1): -0  16(2): -3/3 (黑色题号+红色扣分).

    steps = [(得分, 满分或None)]; 无满分时退化为显示得分. 返回占用高度.
    """
    if not steps:
        return 0.0
    font = _load_font(fsize, bold=True)
    line_h = int(fsize * 1.35)
    col_gap = int(fsize * 0.6)
    cells = []
    for i, (score, full) in enumerate(steps):
        if full is not None:
            text = f"-{full - score:g}/{full:g}"
        else:
            text = f"{score:g}"
        cells.append((f"{title}({i + 1}):", text))
    # 行优先两列: 16(1) 16(2) 第一行, 与网页一致
    col0, col1 = cells[0::2], cells[1::2]

    def col_width(col):
        w = 0
        for label, num in col:
            wl = draw.textbbox((0, 0), label, font=font)[2]
            wn = draw.textbbox((0, 0), num, font=font)[2]
            w = max(w, wl + wn)
        return w

    w0 = col_width(col0)
    for row, (label, num) in enumerate(col0):
        yy = y + row * line_h
        draw.text((x, yy), label, fill=(70, 70, 70), font=font)
        draw.text((x + draw.textbbox((0, 0), label, font=font)[2], yy), num, fill=RED, font=font)
    for row, (label, num) in enumerate(col1):
        yy = y + row * line_h
        draw.text((x + w0 + col_gap, yy), label, fill=(70, 70, 70), font=font)
        draw.text((x + w0 + col_gap + draw.textbbox((0, 0), label, font=font)[2], yy), num, fill=RED, font=font)
    return len(col0) * line_h


def _draw_spot_marks(draw: ImageDraw.Draw, marks: List[dict],
                     img_w: int, img_h: int, fsize: int) -> None:
    """画逐点批注 (spotMark): content 如 '+1', left/top 为百分比坐标."""
    font = _load_font(fsize, bold=True)
    for m in marks:
        try:
            left = float(m.get("left", 0)) / 100.0
            top = float(m.get("top", 0)) / 100.0
        except (ValueError, TypeError):
            continue
        content = str(m.get("content", ""))
        color = RED if content.startswith("-") else GREEN
        x, y = left * img_w, top * img_h
        pad = max(2, fsize // 6)
        bbox = draw.textbbox((0, 0), content, font=font)
        draw.rectangle([x, y, x + (bbox[2] - bbox[0]) + pad * 2, y + (bbox[3] - bbox[1]) + pad * 2],
                       fill=color)
        draw.text((x + pad, y + pad - bbox[1]), content, fill="white", font=font)


def _paper_width_mm(page: dict, img: Image.Image) -> float:
    """由 locatePoint 定位块推断卡纸宽度.

    A3 横向 420mm (maxX≈408-414) / A4 横向 297mm / A4 纵向 210mm (maxX≈204).
    无定位数据时按图片纵横比 fallback.
    """
    lp = page.get("locatePoint") or []
    if lp:
        try:
            max_x = max(p.get("left", 0) + p.get("width", 0) for p in lp)
            if max_x > 350:
                return 420.0
            if max_x > 250:
                return 297.0
            return 210.0
        except (ValueError, TypeError):
            pass
    return 420.0 if img.width >= img.height else 210.0


def annotate_page(img: Image.Image, page: dict, score_map: Dict[int, dict]) -> None:
    """在单页答题卡图上画批注 (原位修改).

    - 各题区域: 错题画红框 + 得分/扣分标签; 满分画绿标签
      (含选择题涂卡区 Object, 其 branch 为题组区域)
    """
    draw = ImageDraw.Draw(img)
    px_per_mm = img.width / _paper_width_mm(page, img)
    fsize = max(22, int(img.width * 0.011))
    seen_boxes = set()

    for sec in page.get("sections") or []:
        if not sec:
            continue
        contents = sec.get("contents") or {}
        sec_pos = sec.get("position") or contents.get("position") or {}
        for br in contents.get("branch") or []:
            if not br:
                continue
            ix_list = br.get("ixList") or []
            recs = [(i, score_map[i]) for i in ix_list if i in score_map]
            if not recs:
                continue
            br_pos = br.get("position") or {}
            ax, ay = _resolve_branch_mm(sec_pos, br_pos)
            bw, bh = br_pos.get("width", 0), br_pos.get("height", 0)
            box_key = (round(ax, 1), round(ay, 1), round(bw, 1), round(bh, 1))
            if box_key in seen_boxes:
                continue
            seen_boxes.add(box_key)
            x1, y1 = ax * px_per_mm, ay * px_per_mm
            x2, y2 = (ax + bw) * px_per_mm, (ay + bh) * px_per_mm
            if x2 - x1 < 20 or y2 - y1 < 20:
                continue
            got = sum(r["score"] for _, r in recs)
            full = sum(r["standard_score"] for _, r in recs)
            wrong = [i for i, r in recs if r["score"] < r["standard_score"]]
            color = RED if wrong else GREEN
            if wrong:
                draw.rectangle([x1, y1, x2, y2], outline=color, width=max(3, fsize // 8))
            start = f"{ix_list[0]}题" if len(recs) == 1 else f"{ix_list[0]}-{ix_list[-1]}题"
            deduct = full - got
            label = f"{start} {got:g}/{full:g}" + (f"  -{deduct:g}" if wrong else "")
            label_h = _draw_label(draw, label, x1, max(0, y1 - fsize * 1.6), color, fsize)
            # 分小问扣分明细 (网页同款两列 "16(2): -3/3")
            sub_y = y1 - fsize * 1.6 + label_h + 6
            if sub_y + fsize * 1.3 < y2 or y1 - fsize * 1.6 < 0:
                for i, r in recs:
                    if r.get("steps"):
                        used = _draw_sub_scores(draw, i, r["steps"], x1, max(sub_y, 4), int(fsize * 0.8))
                        sub_y += used + 4
            # 逐点批注 (spotMark, 老师在图上的 +N/-N 标注)
            for _, r in recs:
                _draw_spot_marks(draw, r.get("spot_marks", []), img.width, img.height, int(fsize * 0.9))


def add_score_bar(img: Image.Image, score, standard_score, title: str) -> Image.Image:
    """在图顶部加白色信息栏: 红色总分大字 + 标题."""
    bar_h = max(80, int(img.height * 0.055))
    big = _load_font(int(bar_h * 0.62), bold=True)
    small = _load_font(int(bar_h * 0.30))
    bar = Image.new("RGB", (img.width, bar_h), "white")
    d = ImageDraw.Draw(bar)
    score_text = f"{score:g}" if score is not None else "?"
    d.text((24, bar_h * 0.10), score_text, fill=RED, font=big)
    sw = d.textbbox((24, bar_h * 0.10), score_text, font=big)[2]
    tail = f" / {standard_score:g} 分" if standard_score is not None else ""
    d.text((sw + 14, bar_h * 0.56), tail, fill=GRAY, font=small)
    if title:
        d.text((sw + 14 + int(bar_h * 2.4), bar_h * 0.56), title, fill=DARK, font=small)
    out = Image.new("RGB", (img.width, img.height + bar_h), "white")
    out.paste(bar, (0, 0))
    out.paste(img, (0, bar_h))
    return out


def merge_pages(images: List[Image.Image], max_width: int = MAX_WIDTH) -> bytes:
    """纵向拼接多页为一张长图, 返回 JPEG 字节."""
    max_w = max(p.width for p in images)
    scaled = []
    for p in images:
        if p.width != max_w:
            p = p.resize((max_w, int(p.height * max_w / p.width)), Image.LANCZOS)
        scaled.append(p)
    total_h = sum(p.height for p in scaled)
    canvas = Image.new("RGB", (max_w, total_h), "white")
    y = 0
    for p in scaled:
        canvas.paste(p, (0, y))
        y += p.height
    if canvas.width > max_width:
        canvas = canvas.resize((max_width, int(canvas.height * max_width / canvas.width)), Image.LANCZOS)
    buf = io.BytesIO()
    canvas.save(buf, format="JPEG", quality=88)
    return buf.getvalue()
