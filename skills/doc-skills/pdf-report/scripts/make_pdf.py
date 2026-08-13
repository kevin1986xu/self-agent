"""JSON 规格 → pdf（reportlab，自动注册系统中文字体）。
用法：python make_pdf.py <spec.json> <out.pdf>"""

import json
import sys
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

CJK_CANDIDATES = [
    "/System/Library/Fonts/STHeiti Light.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/Supplemental/Songti.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
]


def register_cjk() -> str:
    for path in CJK_CANDIDATES:
        if Path(path).exists():
            try:
                pdfmetrics.registerFont(TTFont("CJK", path, subfontIndex=0))
                return "CJK"
            except Exception:  # noqa: BLE001
                continue
    return "Helvetica"  # 兜底（中文会缺字，脚本仍能出文件）


def main():
    spec_path, out_path = sys.argv[1], sys.argv[2]
    spec = json.loads(Path(spec_path).read_text())
    font = register_cjk()
    title_style = ParagraphStyle("t", fontName=font, fontSize=20, spaceAfter=6)
    sub_style = ParagraphStyle("s", fontName=font, fontSize=11, textColor=colors.grey, spaceAfter=12)
    h_style = ParagraphStyle("h", fontName=font, fontSize=14, spaceBefore=10, spaceAfter=6,
                             textColor=colors.HexColor("#1F4E79"))
    body = ParagraphStyle("b", fontName=font, fontSize=10.5, leading=16)

    flow = [Paragraph(spec.get("title", "报告"), title_style)]
    if spec.get("subtitle"):
        flow.append(Paragraph(spec["subtitle"], sub_style))
    for sec in spec.get("sections", []):
        flow.append(Paragraph(sec.get("heading", ""), h_style))
        for para in sec.get("paragraphs") or []:
            flow.append(Paragraph(str(para), body))
        for b in sec.get("bullets") or []:
            flow.append(Paragraph(f"• {b}", body))
        t = sec.get("table")
        if t and t.get("headers"):
            data = [[str(h) for h in t["headers"]]] + [
                [str(v) for v in row[: len(t["headers"])]] for row in (t.get("rows") or [])]
            table = Table(data, hAlign="LEFT")
            table.setStyle(TableStyle([
                ("FONTNAME", (0, 0), (-1, -1), font),
                ("FONTSIZE", (0, 0), (-1, -1), 9.5),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E79")),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F2F6FA")]),
            ]))
            flow.append(Spacer(1, 3 * mm))
            flow.append(table)
        flow.append(Spacer(1, 4 * mm))
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)
    SimpleDocTemplate(out_path, pagesize=A4).build(flow)
    print(f"OK {out_path}")


if __name__ == "__main__":
    main()
