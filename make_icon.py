"""Turns logo_source.jpg (the card) into a macOS .icns. Usage: python3 make_icon.py out.icns [preview.png]"""
import subprocess, sys, tempfile
from pathlib import Path
from PIL import Image, ImageDraw

src = Image.open(Path(__file__).with_name("logo_source.jpg")).convert("RGB")
# crop to the card: bounding box of the red pixels (drops the white margin and stray marks)
red = src.convert("HSV").split()[1].point(lambda s: 255 if s > 120 else 0)  # saturated = red card
card = src.crop(red.getbbox())

# rounded corners so no white shows at the card's edges
w, h = card.size
mask = Image.new("L", card.size, 0)
ImageDraw.Draw(mask).rounded_rectangle((0, 0, w - 1, h - 1), radius=int(w * 0.09), fill=255)
card.putalpha(mask)

# centre on a 1024 canvas, card height = 824 (macOS icon grid)
S, H = 1024, 824
card = card.resize((round(w * H / h), H), Image.LANCZOS)
icon = Image.new("RGBA", (S, S), (0, 0, 0, 0))
icon.paste(card, ((S - card.width) // 2, (S - H) // 2), card)

out = Path(sys.argv[1])
if len(sys.argv) > 2:
    icon.save(sys.argv[2])
icon.save(Path(__file__).with_name("logo.ico"), sizes=[(16, 16), (32, 32), (48, 48), (256, 256)])  # Windows shortcut icon
with tempfile.TemporaryDirectory() as tmp:
    iconset = Path(tmp) / "icon.iconset"
    iconset.mkdir()
    for size in (16, 32, 128, 256, 512):
        icon.resize((size, size), Image.LANCZOS).save(iconset / f"icon_{size}x{size}.png")
        icon.resize((size * 2, size * 2), Image.LANCZOS).save(iconset / f"icon_{size}x{size}@2x.png")
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
