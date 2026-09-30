"""앱 아이콘(초록 둥근 사각형 + 흰 말풍선) 을 그려 config/app.ico 로 저장한다. 실행: python tools/make_icon.py"""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
S = 1024  # 크게 그린 뒤 줄여서 가장자리를 매끄럽게


def squircle_mask(size: int, radius: float) -> Image.Image:
    m = Image.new("L", (size, size), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    return m


def draw() -> Image.Image:
    # 배경: 위 밝은 초록 → 아래 진한 초록 그라데이션
    top, bottom = (104, 240, 120), (22, 196, 60)
    grad = Image.new("RGB", (S, S))
    px = grad.load()
    for y in range(S):
        t = y / (S - 1)
        c = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(S):
            px[x, y] = c
    icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    icon.paste(grad, (0, 0), squircle_mask(S, S * 0.225))

    # 흰 말풍선(타원 + 왼쪽 아래로 휘어진 초승달 꼬리)
    bub = Image.new("L", (S, S), 0)
    d = ImageDraw.Draw(bub)
    d.ellipse([S * 0.155, S * 0.215, S * 0.845, S * 0.745], fill=255)
    tail = Image.new("L", (S, S), 0)
    td = ImageDraw.Draw(tail)
    td.ellipse([S * 0.17, S * 0.50, S * 0.47, S * 0.80], fill=255)       # 꼬리 몸통
    td.ellipse([S * 0.03, S * 0.44, S * 0.36, S * 0.775], fill=0)        # 왼쪽 위를 파내 초승달 모양
    td.ellipse([S * 0.33, S * 0.66, S * 0.80, S * 1.02], fill=0)         # 오른쪽 아래도 둥글게 파내 말풍선과 부드럽게 이음
    tail_clip = Image.new("L", (S, S), 0)
    ImageDraw.Draw(tail_clip).rectangle([0, S * 0.55, S * 0.50, S * 0.80], fill=255)
    tail = Image.composite(tail, Image.new("L", (S, S), 0), tail_clip)
    bub = Image.eval(Image.merge("L", [bub]), lambda v: v)
    bub.paste(255, (0, 0), tail)
    icon.paste((255, 255, 255, 255), (0, 0), bub)

    # 말풍선 안 점 세 개(단톡방 3명) — 연한 초록
    dot = (40, 205, 75, 255)
    dd = ImageDraw.Draw(icon)
    r = S * 0.052
    for cx in (0.36, 0.50, 0.64):
        dd.ellipse([S * cx - r, S * 0.47 - r, S * cx + r, S * 0.47 + r], fill=dot)
    return icon


def main() -> None:
    big = draw()
    out = ROOT / "config" / "app.ico"
    sizes = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]
    big.resize((256, 256), Image.LANCZOS).save(out, sizes=sizes)
    big.resize((512, 512), Image.LANCZOS).save(ROOT / "config" / "app_512.png")
    print(out)


if __name__ == "__main__":
    main()
