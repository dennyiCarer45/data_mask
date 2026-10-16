"""Render fictional Indian-style ID cards as phone photos and run them through the gateway.

    docker run --rm --network llm-privacy-gateway_private --env-file .env \
        -e GATEWAY_URL=http://gateway:8000 -e PYTHONPATH=/app:/app/scripts \
        -v "$PWD:/app" -w /app local-privacy-gateway:dev python scripts/id_card_demo.py

Writes samples/id-card-demo/input/*.jpg and output/<name>/*. Every person, number, and address is
invented; cards carry a SPECIMEN watermark, a silhouette instead of a face, and no emblems or logos.
"""

import io
import json
import os
from pathlib import Path

import demo_run
import httpx
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path("samples/id-card-demo")
FONTS = Path("/usr/share/fonts/truetype/dejavu")
W, H = 1012, 638  # ID-1 card at 300 dpi

CARDS = {
    "aadhaar-style": {
        "band": (232, 120, 40),
        "title": "GOVERNMENT OF INDIA",
        "subtitle": "Aadhaar - Unique Identification (SPECIMEN)",
        "fields": [
            ("", "Kavitha Raman"),
            ("DOB", "09/11/1990"),
            ("", "Female"),
            ("C/O", "Suresh Raman"),
            ("Address", "No. 42, 3rd Cross, Indiranagar,"),
            ("", "Bengaluru, Karnataka - 560038"),
        ],
        "number": "4821 7390 5162",
        "footer": "VID: 9134 6620 4817 2290",
    },
    "pan-style": {
        "band": (40, 90, 160),
        "title": "INCOME TAX DEPARTMENT",
        "subtitle": "Permanent Account Number Card (SPECIMEN)",
        "fields": [
            ("Name", "ARJUN MEHTA"),
            ("Father's Name", "SURESH MEHTA"),
            ("Date of Birth", "23/04/1987"),
        ],
        "number": "BQKPM4821K",
        "footer": "Signature",
    },
    "voter-id-style": {
        "band": (60, 130, 90),
        "title": "ELECTION COMMISSION OF INDIA",
        "subtitle": "Elector Photo Identity Card (SPECIMEN)",
        "fields": [
            ("EPIC No", "XKD4827391"),
            ("Elector's Name", "Farida Sheikh"),
            ("Father's Name", "Abdul Sheikh"),
            ("Sex", "Female"),
            ("Date of Birth", "15/08/1995"),
            ("Address", "Flat 7B, Lake View Apts, Salt Lake,"),
            ("", "Kolkata - 700091"),
        ],
        "number": "",
        "footer": "",
    },
    "driving-licence-style": {
        "band": (150, 60, 70),
        "title": "UNION OF INDIA - DRIVING LICENCE",
        "subtitle": "Transport Department, Karnataka (SPECIMEN)",
        "fields": [
            ("DL No", "KA05 20190034871"),
            ("Name", "Rohan Dsouza"),
            ("S/O", "Peter Dsouza"),
            ("DOB", "02/02/1992"),
            ("Blood Group", "B+"),
            ("Address", "18 Church Street, Mangaluru,"),
            ("", "Karnataka - 575001"),
            ("Valid Till", "01/02/2039"),
        ],
        "number": "",
        "footer": "Class of vehicle: LMV, MCWG",
    },
}


def font(size, bold=False):
    return ImageFont.truetype(
        str(FONTS / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")), size
    )


def draw_card(card):
    image = Image.new("RGB", (W, H), (250, 248, 240))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, W, 110), fill=card["band"])
    draw.text((30, 18), card["title"], fill="white", font=font(38, bold=True))
    draw.text((30, 68), card["subtitle"], fill="white", font=font(24))
    # Silhouette, not a face.
    draw.rectangle((30, 140, 230, 400), fill=(214, 214, 214), outline=(150, 150, 150), width=2)
    draw.ellipse((90, 170, 170, 250), fill=(160, 160, 160))
    draw.ellipse((60, 260, 200, 420), fill=(160, 160, 160))
    draw.rectangle((30, 400, 230, 420), fill=(250, 248, 240))
    y = 140
    for label, value in card["fields"]:
        text = f"{label}: {value}" if label else value
        draw.text((260, y), text, fill=(25, 25, 40), font=font(28))
        y += 44
    if card["number"]:
        draw.text((260, y + 20), card["number"], fill=(10, 10, 20), font=font(46, bold=True))
    if card["footer"]:
        draw.text((30, H - 60), card["footer"], fill=(60, 60, 70), font=font(24))
    # Faint diagonal SPECIMEN mark, as on sample documents.
    mark = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mark).text((180, 250), "SPECIMEN", fill=40, font=font(150, bold=True))
    mark = mark.rotate(18, center=(W // 2, H // 2))
    image.paste((200, 60, 60), (0, 0), mark)
    return image


def phone_photo(card_image):
    # Card on a desk, held slightly crooked; soft focus and heavy JPEG compression.
    desk = Image.new("RGB", (W + 240, H + 240), (122, 98, 76))
    desk.paste(card_image, (120, 120))
    desk = desk.rotate(2.5, expand=False, fillcolor=(122, 98, 76), resample=Image.BICUBIC)
    desk = desk.filter(ImageFilter.GaussianBlur(0.9))
    output = io.BytesIO()
    desk.save(output, "JPEG", quality=62)
    return output.getvalue()


def main():
    headers = {"Authorization": "Bearer " + os.environ["PRIVACY_API_KEY"]}
    url = os.environ.get("GATEWAY_URL", "http://127.0.0.1:8000")
    demo_run.ROOT = ROOT
    (ROOT / "input").mkdir(parents=True, exist_ok=True)
    names = []
    for name, card in CARDS.items():
        (ROOT / "input" / f"{name}.jpg").write_bytes(phone_photo(draw_card(card)))
        names.append(f"{name}.jpg")
    results = []
    with httpx.Client(base_url=url, timeout=600, trust_env=False) as client:
        print("gateway:", client.get("/health").json())
        for name in names:
            results.append(demo_run.save(ROOT / "output", name, "general", client, headers))
            print(json.dumps(results[-1]))
    (ROOT / "summary.json").write_text(json.dumps(results, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
