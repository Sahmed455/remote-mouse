from pathlib import Path
from PIL import Image, ImageDraw

ASSETS_DIR = Path(__file__).resolve().parent / "assets"
ASSETS_DIR.mkdir(parents=True, exist_ok=True)


def make_icon(width: int, height: int) -> Image.Image:
    img = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    size = min(width, height)
    ox = (width - size) // 2
    oy = (height - size) // 2
    pad = max(2, int(size * 0.08))

    draw.rounded_rectangle(
        (ox + pad, oy + pad, ox + size - pad, oy + size - pad),
        radius=int(size * 0.22),
        fill=(37, 99, 235, 255),
    )

    mx1, my1 = ox + int(size * 0.30), oy + int(size * 0.20)
    mx2, my2 = ox + int(size * 0.70), oy + int(size * 0.80)
    draw.rounded_rectangle((mx1, my1, mx2, my2), radius=int(size * 0.18), fill=(240, 244, 248, 255))
    draw.line(
        (ox + size // 2, my1 + int(size * 0.04), ox + size // 2, my1 + int(size * 0.26)),
        fill=(37, 99, 235, 255),
        width=max(2, int(size * 0.05)),
    )
    return img


def main() -> None:
    targets = {
        "StoreLogo.png": (50, 50),
        "Square44x44Logo.png": (44, 44),
        "Square150x150Logo.png": (150, 150),
        "Wide310x150Logo.png": (310, 150),
    }
    for filename, dims in targets.items():
        make_icon(*dims).save(ASSETS_DIR / filename)
        print(f"Generated {ASSETS_DIR / filename} ({dims[0]}x{dims[1]})")


if __name__ == "__main__":
    main()