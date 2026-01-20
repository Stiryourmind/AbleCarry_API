from pathlib import Path
from PIL import Image, ImageOps

SRC_DIR = Path("public/products")
OUT_DIR = SRC_DIR / "thumbs"
OUT_DIR.mkdir(parents=True, exist_ok=True)

THUMB_W, THUMB_H = 600, 600
BG = (10, 10, 10)
EXTS = {".png", ".jpg", ".jpeg", ".webp"}

def make_thumb(src: Path, dst: Path):
    img = Image.open(src).convert("RGBA")
    bbox = img.getbbox()
    if bbox:
        img = img.crop(bbox)

    img = ImageOps.contain(img, (THUMB_W, THUMB_H), Image.LANCZOS)

    canvas = Image.new("RGBA", (THUMB_W, THUMB_H), BG + (255,))
    x = (THUMB_W - img.width) // 2
    y = (THUMB_H - img.height) // 2
    canvas.alpha_composite(img, (x, y))

    dst.parent.mkdir(parents=True, exist_ok=True)
    canvas.convert("RGB").save(dst, "PNG", optimize=True)

def main():
    print(f"[thumbs] SRC_DIR = {SRC_DIR.resolve()}")
    print(f"[thumbs] OUT_DIR = {OUT_DIR.resolve()}")

    if not SRC_DIR.exists():
        print("[thumbs] ERROR: SRC_DIR does not exist")
        return

    files = [p for p in SRC_DIR.iterdir() if p.is_file() and p.suffix.lower() in EXTS]
    print(f"[thumbs] Found {len(files)} image(s): {[p.name for p in files]}")

    count = 0
    for src in files:
        dst = OUT_DIR / f"{src.stem}.png"
        make_thumb(src, dst)
        print(f"[thumbs] ✅ {src.name} -> {dst.as_posix()}")
        count += 1

    print(f"[thumbs] Done. Generated {count} thumbnail(s).")

if __name__ == "__main__":
    main()
