"""Draws the Athena app icon (gold shield + A on black) as .ico for the desktop shortcut and a 512px PNG for phones."""
from PIL import Image, ImageDraw, ImageFont
S = 512
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle([0, 0, S - 1, S - 1], radius=110, fill=(8, 10, 16, 255))
shield = [(256, 58), (430, 118), (418, 290), (256, 458), (94, 290), (82, 118)]
d.polygon(shield, fill=(212, 169, 58, 255))
inner = [(256, 88), (402, 138), (392, 282), (256, 422), (120, 282), (110, 138)]
d.polygon(inner, fill=(14, 16, 24, 255))
try:
    f = ImageFont.truetype("arialbd.ttf", 230)
except OSError:
    f = ImageFont.load_default()
d.text((256, 262), "A", font=f, fill=(255, 106, 0, 255), anchor="mm")
im.save(r"C:\jarvis-trading\static\athena-512.png")
im.resize((180, 180), Image.LANCZOS).save(r"C:\jarvis-trading\static\athena-180.png")
im.save(r"C:\jarvis-trading\static\athena.ico", sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
print("icons ok")
