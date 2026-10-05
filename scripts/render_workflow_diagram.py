"""Regenerate docs/workflow-diagram.png with the current node/prompt facts.

The original PNG was exported by hand and no generator was kept, so the N3 box
went on claiming "4 concurrent" long after the GLM pacer made it 2. This script
is that missing generator: re-run it whenever a prompt version or a concurrency
default changes, so the diagram cannot drift away from the code again.

    pip install pillow                      # not a runtime dependency
    python3 scripts/render_workflow_diagram.py

Pillow is deliberately *not* in requirements.txt: the pipeline never imports it,
it is only needed to re-draw this one asset.

Everything is drawn on a 2x canvas and downscaled, which is what keeps the text
crisp. All coordinates go through `S()` so the scale is applied exactly once.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

W, H = 1080, 1010
SCALE = 2

BG = (255, 255, 255)
INK = (17, 22, 28)
MUTED = (98, 107, 122)
LINE = (176, 184, 196)
BOX = (252, 253, 255)
BLUE_BG = (238, 243, 254)
BLUE_LINE = (176, 199, 240)
BLUE_INK = (27, 95, 217)
SIDE_BG = (250, 251, 253)
SIDE_LINE = (206, 213, 222)
GREEN_BG = (240, 253, 244)
GREEN_LINE = (140, 200, 165)
GREEN_INK = (21, 128, 61)

ARIAL = "/System/Library/Fonts/Supplemental/Arial.ttf"
ARIAL_BOLD = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
MONO = "/System/Library/Fonts/Menlo.ttc"

LEFT, RIGHT = 60, 620
SIDE_LEFT, SIDE_RIGHT = 668, 1040
CX = (LEFT + RIGHT) / 2


def S(v: float) -> float:
    return v * SCALE


def fnt(path: str, size: int):
    return ImageFont.truetype(path, int(S(size)))


F_TITLE = fnt(ARIAL_BOLD, 15)
F_MONO = fnt(MONO, 11)
F_BODY = fnt(ARIAL, 11)
F_SIDE_H = fnt(ARIAL_BOLD, 13)
F_SMALL = fnt(ARIAL, 10)
F_USER = fnt(ARIAL, 14)
F_FINAL = fnt(ARIAL_BOLD, 16)

# (node label, [monospace detail lines])
MAIN_NODES = [
    (
        "N1 01_parse_intent",
        [
            "Tool: LLM (GLM) · temperature 0",
            "prompt: prompts/01_parse_intent.md v1.0",
            "schema: IntentParseResult",
        ],
    ),
    (
        "N2 02_retrieve",
        [
            "Tool: GdeltAdapter — GDELT DOC 2.0 · no key",
            "Tool: China5eAdapter — section pages + article bodies",
            "Tool: RateLimiter + disk cache · tools/http.py",
        ],
    ),
    (
        "N3 03_extract",
        [
            "Tool: LLM (GLM) · temperature 0",
            "prompt: prompts/03_extract.md v1.3",
            "batched 8 docs · body trimmed to 1500 chars",
            "2 requests in flight, 0.5s apart (ProviderPacer)",
            "rejected batch halved + retried, not dropped",
        ],
    ),
    ("N4 04_aggregate", ["pure Python · no LLM"]),
    ("N5 05_analyze", ["LLM explains pre-computed numbers · prompt: prompts/05_analyze.md v1.2"]),
    ("N6 06_generate_report", ["Jinja2 + Leaflet + Chart.js · prompt: prompts/06_report.md v1.0"]),
    ("N7 07_quality_gate", ["confidence threshold + retry budget"]),
]

EDGE_LABELS = ["QuerySpec", "list[RawDoc] · failed_sources[]", "list[IntelItem]", "", "", ""]

SIDE = {
    0: ("Clarification question returned", ["to the user · pipeline stops"]),
    2: ("Human review queue", ["subject missing → needs_review = True"]),
    4: (
        "Snapshots & trace",
        [
            "data/runs/RUN_ID/snapshots/*.json",
            "data/runs/RUN_ID/trace.jsonl",
            "written by every node",
        ],
    ),
}

LINE_H = 17
BOX_PAD_TOP = 40
BOX_PAD_BOTTOM = 10


def main() -> Image.Image:
    img = Image.new("RGB", (int(S(W)), int(S(H))), BG)
    d = ImageDraw.Draw(img)

    def box(x0, y0, x1, y1, radius, fill, outline, width=1):
        d.rounded_rectangle(
            [S(x0), S(y0), S(x1), S(y1)],
            radius=S(radius),
            fill=fill,
            outline=outline,
            width=int(S(width)),
        )

    def put(x, y, value, font, fill):
        d.text((S(x), S(y)), value, font=font, fill=fill)

    def width_of(value, font):
        return d.textlength(value, font=font) / SCALE

    def arrow_down(y0, y1, label=""):
        d.line((S(CX), S(y0), S(CX), S(y1)), fill=INK, width=int(S(1.8)))
        d.polygon([(S(CX - 5), S(y1 - 6)), (S(CX + 5), S(y1 - 6)), (S(CX), S(y1))], fill=INK)
        if label:
            put(CX + 12, (y0 + y1) / 2 - 8, label, F_MONO, MUTED)

    y = 34.0
    box(LEFT + 40, y, RIGHT - 40, y + 54, 27, BLUE_BG, BLUE_LINE, 2)
    user = "User: one natural-language question"
    put(CX - width_of(user, F_USER) / 2, y + 17, user, F_USER, INK)
    y += 54 + 24

    for index, (title, details) in enumerate(MAIN_NODES):
        height = BOX_PAD_TOP + len(details) * LINE_H + BOX_PAD_BOTTOM + 4
        box(LEFT, y, RIGHT, y + height, 8, BOX, LINE, 1.6)
        put(LEFT + 16, y + 12, title, F_TITLE, INK)

        inner_top = y + BOX_PAD_TOP - 6
        box(
            LEFT + 16,
            inner_top,
            RIGHT - 16,
            inner_top + len(details) * LINE_H + 14,
            6,
            BLUE_BG,
            BLUE_LINE,
            1,
        )
        for i, line in enumerate(details):
            put(LEFT + 28, inner_top + 7 + i * LINE_H, line, F_MONO, BLUE_INK)

        annotation = SIDE.get(index)
        if annotation:
            heading, lines = annotation
            side_h = 32 + len(lines) * 15
            top = y + (height - side_h) / 2
            box(SIDE_LEFT, top, SIDE_RIGHT, top + side_h, 8, SIDE_BG, SIDE_LINE, 1)
            put(SIDE_LEFT + 14, top + 9, heading, F_SIDE_H, INK)
            for i, line in enumerate(lines):
                put(SIDE_LEFT + 14, top + 29 + i * 15, line, F_SMALL, MUTED)
            mid = top + side_h / 2
            d.line((S(RIGHT), S(mid), S(SIDE_LEFT - 6), S(mid)), fill=SIDE_LINE, width=int(S(1)))
            d.polygon(
                [
                    (S(SIDE_LEFT - 6), S(mid - 4)),
                    (S(SIDE_LEFT - 6), S(mid + 4)),
                    (S(SIDE_LEFT), S(mid)),
                ],
                fill=SIDE_LINE,
            )

        y += height
        if index < len(MAIN_NODES) - 1:
            arrow_down(y, y + 24, EDGE_LABELS[index])
            y += 24

    y += 24
    box(LEFT, y, RIGHT, y + 56, 8, GREEN_BG, GREEN_LINE, 1.8)
    final = "report.html"
    put(CX - width_of(final, F_FINAL) / 2, y + 10, final, F_FINAL, GREEN_INK)
    sub = "single-file HTML · map + time series + sourced findings"
    put(CX - width_of(sub, F_BODY) / 2, y + 34, sub, F_BODY, GREEN_INK)

    return img.resize((W, H), Image.LANCZOS)


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "docs" / "workflow-diagram.png"
    main().save(out, "PNG", optimize=True)
    print(f"wrote {out} ({out.stat().st_size} bytes)")
