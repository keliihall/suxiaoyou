"""Generate macOS-native tray template + squircle app icons from the 苏小有 logo.

Outputs (next to this file):
  tray-template.png        — pure-black 苏小有 template mask, transparent, ~22pt menu bar
  tray-template@2x.png     — Retina version
  macos-icon-1024.png      — squircle app icon (used to rebuild icon.icns)
"""

from pathlib import Path

from PIL import Image
from PIL import ImageDraw

ICON_DIR = Path(__file__).resolve().parent
SOURCE = ICON_DIR / "Suxiaoyou logo" / "Yak@3x.png"

TRAY_VIEWBOX = (32.0, 18.0, 124.0, 128.0)


def cubic_points(p0, p1, p2, p3, steps: int = 32) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for i in range(1, steps + 1):
        t = i / steps
        u = 1.0 - t
        x = (
            u * u * u * p0[0]
            + 3 * u * u * t * p1[0]
            + 3 * u * t * t * p2[0]
            + t * t * t * p3[0]
        )
        y = (
            u * u * u * p0[1]
            + 3 * u * u * t * p1[1]
            + 3 * u * t * t * p2[1]
            + t * t * t * p3[1]
        )
        pts.append((x, y))
    return pts


def transform_points(
    points: list[tuple[float, float]],
    target_size: int,
    supersample: int,
) -> list[tuple[float, float]]:
    left, top, right, bottom = TRAY_VIEWBOX
    canvas = target_size * supersample
    pad = max(1.0, target_size * 3.0 / 44.0) * supersample
    scale = (canvas - pad * 2.0) / (bottom - top)
    x_offset = (canvas - (right - left) * scale) / 2.0
    y_offset = pad
    return [
        (x_offset + (x - left) * scale, y_offset + (y - top) * scale)
        for x, y in points
    ]


def draw_filled_path(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    target_size: int,
    supersample: int,
    fill: int,
) -> None:
    draw.polygon(transform_points(points, target_size, supersample), fill=fill)


def draw_curve_stroke(
    draw: ImageDraw.ImageDraw,
    points: list[tuple[float, float]],
    target_size: int,
    supersample: int,
    width: float,
    fill: int,
) -> None:
    scaled = transform_points(points, target_size, supersample)
    pad = max(1.0, target_size * 3.0 / 44.0) * supersample
    scale = (target_size * supersample - 2 * pad) / (TRAY_VIEWBOX[3] - TRAY_VIEWBOX[1])
    stroke_width = max(1, int(round(width * scale)))
    draw.line(scaled, fill=fill, width=stroke_width, joint="curve")
    radius = stroke_width / 2
    for x, y in (scaled[0], scaled[-1]):
        draw.ellipse((x - radius, y - radius, x + radius, y + radius), fill=fill)


def suxiaoyou_tray_template(target_size: int, supersample: int = 8) -> Image.Image:
    """Monochrome menu-bar mask matching the 苏小有 arch logo.

    The inner arch reaches the baseline and is subtracted from the outer arch,
    leaving the doorway open at the bottom. The two wave marks are then drawn
    back inside that doorway as positive template strokes.
    """
    canvas_size = target_size * supersample
    alpha = Image.new("L", (canvas_size, canvas_size), 0)
    draw = ImageDraw.Draw(alpha)

    outer = (
        [(32, 128), (32, 68)]
        + cubic_points((32, 68), (32, 39), (52, 18), (78, 18))
        + cubic_points((78, 18), (104, 18), (124, 39), (124, 68))
        + [(124, 128)]
    )
    inner = (
        [(50, 128), (50, 72)]
        + cubic_points((50, 72), (50, 51), (61, 39), (78, 39))
        + cubic_points((78, 39), (95, 39), (106, 51), (106, 72))
        + [(106, 128)]
    )

    draw_filled_path(draw, outer, target_size, supersample, 255)
    draw_filled_path(draw, inner, target_size, supersample, 0)

    clip = Image.new("L", (canvas_size, canvas_size), 0)
    draw_filled_path(ImageDraw.Draw(clip), inner, target_size, supersample, 255)

    waves = Image.new("L", (canvas_size, canvas_size), 0)
    wave_draw = ImageDraw.Draw(waves)
    wave_a = (
        [(55, 84)]
        + cubic_points((55, 84), (63, 78), (71, 78), (79, 84), steps=18)
        + cubic_points((79, 84), (87, 90), (95, 90), (103, 84), steps=18)
    )
    wave_b = (
        [(58, 100)]
        + cubic_points((58, 100), (66, 94), (74, 94), (82, 100), steps=18)
        + cubic_points((82, 100), (90, 106), (98, 106), (106, 100), steps=18)
    )
    draw_curve_stroke(wave_draw, wave_a, target_size, supersample, 9.5, 255)
    draw_curve_stroke(wave_draw, wave_b, target_size, supersample, 9.5, 255)
    clipped_waves = Image.composite(waves, Image.new("L", alpha.size, 0), clip)
    alpha = Image.composite(Image.new("L", alpha.size, 255), alpha, clipped_waves)

    alpha = alpha.resize((target_size, target_size), Image.LANCZOS)
    out = Image.new("RGBA", (target_size, target_size), (0, 0, 0, 0))
    out.putalpha(alpha.point(lambda v: 255 if v >= 128 else 0, mode="L"))
    return out


def trim_to_content(img: Image.Image) -> Image.Image:
    bbox = img.split()[-1].getbbox()
    return img.crop(bbox) if bbox else img


def extract_letter_mask(src: Image.Image, black_threshold: int = 110) -> Image.Image:
    """Binary alpha mask of the 苏小有 symbol interiors — not outline or shadow.

    The source logo stacks: outer drop shadow (black) → letter outline (black) →
    letter fill (yellow / white / blue). Keeping only pixels whose max channel
    clears `black_threshold` isolates the three letter fills so Y | A | K read as
    distinct glyphs, with the natural gaps where the outlines used to be. The
    mask is hard-binary (0 or 255) to avoid gray fringe when blended on colored
    menu bars — we rely on supersampling + downscale for edge smoothness.
    """
    rgba = src.convert("RGBA")
    w, h = rgba.size
    mask = Image.new("L", (w, h), 0)
    src_px = rgba.load()
    mask_px = mask.load()
    for y in range(h):
        for x in range(w):
            r, g, b, a = src_px[x, y]
            if a < 128:
                continue
            if max(r, g, b) <= black_threshold:
                continue
            mask_px[x, y] = 255
    return mask


def white_template(src: Image.Image, target_height: int, pad: int = 1) -> Image.Image:
    """Legacy pure-white template generator, kept for reference.

    `target_height` is the intended pixel height at 1x or 2x (e.g. 44 for @2x on
    a 22pt menu bar). The output keeps the letters' natural wide aspect so they
    fill the menu bar vertically — the square-canvas padding from the previous
    version was shrinking them visually. Alpha is strictly {0, 255}; RGB is
    always pure white.
    """
    mask_full = extract_letter_mask(src)
    bbox = mask_full.getbbox()
    cropped = mask_full.crop(bbox) if bbox else mask_full
    cw, ch = cropped.size
    scale = target_height / ch
    new_w, new_h = max(1, int(round(cw * scale))), target_height
    resized = cropped.resize((new_w, new_h), Image.LANCZOS)
    # Re-binarize after resample to kill any gray edge pixels LANCZOS may have
    # introduced — guarantees pure white, never colored or semi-transparent.
    binary = resized.point(lambda v: 255 if v >= 128 else 0, mode="L")
    # Pre-fill RGB as pure white everywhere, then stamp the binary mask as alpha.
    # Transparent pixels carry RGB=(255,255,255,0) so the file is literally
    # "pure white and nothing else" — no black RGB lurking under alpha=0.
    canvas = Image.new("RGBA", (new_w + pad * 2, new_h + pad * 2), (255, 255, 255, 0))
    alpha = Image.new("L", canvas.size, 0)
    alpha.paste(binary, (pad, pad))
    canvas.putalpha(alpha)
    return canvas


def squircle_mask(size: int, n: float = 5.0, supersample: int = 4) -> Image.Image:
    """Superellipse |x|^n + |y|^n <= 1 — macOS-style squircle.

    Built row-by-row so we don't need numpy. Supersample then downscale for AA.
    """
    s = size * supersample
    half = s / 2.0
    mask = Image.new("L", (s, s), 0)
    px = mask.load()
    for y in range(s):
        dy = abs((y + 0.5 - half) / half) ** n
        if dy > 1.0:
            continue
        threshold = (1.0 - dy)
        for x in range(s):
            dx = abs((x + 0.5 - half) / half) ** n
            if dx <= threshold:
                px[x, y] = 255
    return mask.resize((size, size), Image.LANCZOS)


def macos_app_icon(src: Image.Image, size: int = 1024) -> Image.Image:
    """Black squircle with the 苏小有 logo centered.

    Apple's macOS icon grid: squircle fits 824/1024 (~80%) of the canvas,
    leaving room for the system shadow. Logo content sits inside that square.
    """
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    sq_size = int(round(size * 0.824))
    offset = (size - sq_size) // 2

    mask = squircle_mask(sq_size)
    bg = Image.new("RGBA", (sq_size, sq_size), (0, 0, 0, 255))
    bg.putalpha(mask)
    canvas.paste(bg, (offset, offset), bg)

    cropped = trim_to_content(src)
    w, h = cropped.size
    inner_target = sq_size * 0.78
    scale = inner_target / max(w, h)
    new_w, new_h = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    logo = cropped.resize((new_w, new_h), Image.LANCZOS)

    lx = offset + (sq_size - new_w) // 2
    ly = offset + (sq_size - new_h) // 2
    canvas.paste(logo, (lx, ly), logo)
    return canvas


ICONSET_SIZES = [
    ("icon_16x16.png", 16),
    ("icon_16x16@2x.png", 32),
    ("icon_32x32.png", 32),
    ("icon_32x32@2x.png", 64),
    ("icon_128x128.png", 128),
    ("icon_128x128@2x.png", 256),
    ("icon_256x256.png", 256),
    ("icon_256x256@2x.png", 512),
    ("icon_512x512.png", 512),
    ("icon_512x512@2x.png", 1024),
]


def main() -> None:
    src = Image.open(SOURCE).convert("RGBA")

    # Target menu-bar height: ~22pt. Output 1x at 22px tall, @2x at 44px.
    suxiaoyou_tray_template(22).save(ICON_DIR / "tray-template.png")
    suxiaoyou_tray_template(44).save(ICON_DIR / "tray-template@2x.png")

    icon1024 = macos_app_icon(src, 1024)
    icon1024.save(ICON_DIR / "macos-icon-1024.png")

    iconset_dir = ICON_DIR / "icon.iconset"
    iconset_dir.mkdir(exist_ok=True)
    for name, size in ICONSET_SIZES:
        icon1024.resize((size, size), Image.LANCZOS).save(iconset_dir / name)
    print("Wrote tray-template{,@2x}.png, macos-icon-1024.png, icon.iconset/*")


if __name__ == "__main__":
    main()
