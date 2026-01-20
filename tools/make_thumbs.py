from pathlib import Path
from PIL import Image, ImageOps

SRC_DIR = Path("public/products")
OUT_DIR = SRC_DIR / "thumbs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

THUMB_W, THUMB_H = 600, 600
BG = (10, 10, 10)  # dark background
EXTS = {".png", ".jpg", ".jpeg", ".webp"}


def make_thumb(src: Path, dst: Path):
    img = Image.open(src).convert("RGBA")

    # Trim transparent borders (nice for cutout PNG)
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)

    # Resize without distortion
    img = ImageOps.contain(img, (THUMB_W, THUMB_H), Image.LANCZOS)

    # Center on square background
    canvas = Image.new("RGBA", (THUMB_W, THUMB_H), BG + (255,))
    x = (THUMB_W - img.width) // 2
    y = (THUMB_H - img.height) // 2
    canvas.alpha_composite(img, (x, y))

    # Save PNG thumbnail
    canvas.convert("RGB").save(dst, "PNG", optimize=True)


def main():
    images = [p for p in SRC_DIR.iterdir() if p.is_file() and p.suffix.lower() in EXTS]

    if not images:
        print(f"⚠️ No product images found in {SRC_DIR}")
        return

    for img in images:
        out = OUT_DIR / f"{img.stem}.png"
        make_thumb(img, out)
        print(f"✅ thumbnail generated: {out}")

    print("🎉 All thumbnails ready")


if __name__ == "__main__":
    main()
