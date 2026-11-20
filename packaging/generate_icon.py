"""使用 Pillow 生成供 cuaflow 桌面端与安装包构建使用的多尺寸 Windows .ico 图标。"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw


def build_icon(out_path: Path) -> Path:
    size = 256
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 圆角深藏青色背景卡片
    draw.rounded_rectangle([(12, 12), (244, 244)], radius=54, fill=(15, 23, 42, 255))
    draw.rounded_rectangle([(18, 18), (238, 238)], radius=48, outline=(56, 189, 248, 90), width=4)

    # 浏览器窗口顶栏
    draw.rounded_rectangle([(44, 52), (212, 196)], radius=20, fill=(30, 41, 59, 255), outline=(59, 130, 246, 255), width=5)
    draw.line([(46, 84), (210, 84)], fill=(59, 130, 246, 200), width=4)
    for idx, col in enumerate([(248, 113, 113, 255), (251, 191, 36, 255), (74, 222, 128, 255)]):
        cx = 64 + idx * 18
        draw.ellipse([(cx - 5, 63), (cx + 5, 73)], fill=col)

    # 窗口内的自动流程播放徽标
    draw.polygon([(94, 108), (94, 172), (150, 140)], fill=(56, 189, 248, 255))
    # 右下角的绿色监督守护圆标
    draw.ellipse([(150, 146), (218, 214)], fill=(22, 163, 74, 255), outline=(15, 23, 42, 255), width=6)
    draw.line([(168, 181), (180, 193), (202, 168)], fill=(255, 255, 255, 255), width=7, joint="curve")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(
        out_path,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    return out_path


if __name__ == "__main__":
    target = Path(__file__).resolve().parent / "cuaflow.ico"
    build_icon(target)
    print(f"Generated {target}")
