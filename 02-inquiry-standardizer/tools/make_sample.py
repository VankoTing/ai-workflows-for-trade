"""生成测试用的中文询价样本，模拟客户发来的原始文件。

刻意埋了两个真实的技术坑，用来验证 AI 审查是否能发现：
    1. 把铸造不锈钢写成 ASTM A350-CF8M（A350 只覆盖锻件，铸造应为 A351）
    2. 丁腈橡胶(NBR)衬里 + 180℃ 高温导热油工况（NBR 耐温通常 ≤100℃）
"""

from pathlib import Path

from openpyxl import Workbook

OUT = Path(__file__).resolve().parent.parent / "data" / "input"


def make_egypt() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "询价单"
    ws.append(["采购询价单 / Purchase Inquiry"])
    ws.append(["项目 Project", "埃及 691-2610-26"])
    ws.append([])
    ws.append(["序号", "阀门类型", "口径", "压力等级", "连接形式", "结构长度", "工作介质", "数量", "单位"])
    for row in [
        [2, "球阀", "DN100", "Class 150", "凸面法兰(RF)", "229 mm", "水与烃类凝析液", 2, "台"],
        [3, "旋启式止回阀", "DN50", "Class 150", "凸面法兰(RF)", "203.2 mm", "水与烃类凝析液", 3, "台"],
        [4, "截止阀", "DN100", "Class 600", "环形槽面(RTJ)", "434.8 mm", "原水与烃类凝析液", 2, "台"],
        [5, "闸阀", "DN100", "Class 600", "凸面法兰(RF)", "431.8 mm", "原水与烃类凝析液", 2, "台"],
    ]:
        ws.append(row)
    ws.append([])
    ws.append(["备注：清单从序号 2 开始，序号 1 是否遗漏请确认。"])
    wb.save(OUT / "埃及项目询价_中文.xlsx")


def make_butterfly() -> None:
    wb = Workbook()
    ws = wb.active
    ws.title = "询价"
    ws.append(["蝶阀询价单"])
    for key, value in [
        ("询价日期", "2026-09-07"),
        ("阀门类型", "蝶阀"),
        ("结构型式", "中线式，衬里阀体"),
        ("阀体材质", "碳钢"),
        ("内件", "316不锈钢"),
        ("压力等级", "Class 150"),
        ("连接方式", "双法兰，凸面"),
        ("阀杆结构", "两段式阀杆"),
        ("法兰面", "锯齿面(水线)"),
        ("衬里材质", "丁腈橡胶"),
        ("口径", "未注明"),
        ("数量", "未注明"),
    ]:
        ws.append([key, value])
    ws.append([])
    ws.append(["备注", "蝶板材质 ASTM A350-CF8M，阀杆材质 ASTM A564-30"])
    ws.append(["备注", "用于高温导热油工况，工作温度约 180 摄氏度"])
    wb.save(OUT / "蝶阀询价_中文.xlsx")


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    make_egypt()
    make_butterfly()
    print("样本已生成 ->", OUT)
