"""Draw ``monitorctl/app/assets/monitorctl.ico``.

Development-time only: needs Pillow, which the app itself does not.  Run it
again after changing the design and commit the resulting ``.ico``.

Each size is drawn on its own at 4x and downsampled, rather than one large
image being shrunk by the ICO writer: at 16 px - the tray - a monitor outline
shrunk from 256 px turns to mush, while one drawn for 16 px stays legible.

The design: a monitor on a stand, its screen split into red, green and blue
bands - the gains this program exists to keep in place.  A light outline keeps
it visible on both dark and light taskbars.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SUPERSAMPLE = 4
OUT = (
    Path(__file__).resolve().parent.parent
    / "monitorctl"
    / "app"
    / "assets"
    / "monitorctl.ico"
)

FRAME = (232, 236, 242, 255)
SHADOW = (18, 20, 26, 255)
BANDS = ((232, 76, 76, 255), (76, 190, 108, 255), (82, 132, 232, 255))


def draw(size: int) -> Image.Image:
    s = size * SUPERSAMPLE
    image = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    u = s / 16  # one "pixel" of the 16 px design

    # Screen: wide rounded rectangle with a dark border, so it reads on a
    # light taskbar, inside a light frame, so it reads on a dark one.
    left, top, right, bottom = 0.5 * u, 1.5 * u, 15.5 * u, 11.5 * u
    d.rounded_rectangle((left, top, right, bottom), radius=1.6 * u, fill=SHADOW)
    inset = 0.9 * u
    d.rounded_rectangle(
        (
            left + inset * 0.5,
            top + inset * 0.5,
            right - inset * 0.5,
            bottom - inset * 0.5,
        ),
        radius=1.2 * u,
        fill=FRAME,
    )
    inner = (left + 1.6 * u, top + 1.6 * u, right - 1.6 * u, bottom - 1.6 * u)
    band_width = (inner[2] - inner[0]) / 3
    for i, colour in enumerate(BANDS):
        x0 = inner[0] + i * band_width
        d.rectangle((x0, inner[1], x0 + band_width, inner[3]), fill=colour)

    # Stand.
    d.rectangle((7 * u, 11.5 * u, 9 * u, 13.4 * u), fill=SHADOW)
    d.rectangle((7.4 * u, 11.5 * u, 8.6 * u, 13.4 * u), fill=FRAME)
    d.rounded_rectangle((4 * u, 13 * u, 12 * u, 15 * u), radius=0.8 * u, fill=SHADOW)
    d.rounded_rectangle(
        (4.5 * u, 13.4 * u, 11.5 * u, 14.6 * u), radius=0.5 * u, fill=FRAME
    )

    return image.resize((size, size), Image.Resampling.LANCZOS)


def main() -> None:
    images = [draw(size) for size in SIZES]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    largest = images[-1]
    largest.save(
        OUT,
        format="ICO",
        sizes=[(i.width, i.height) for i in images],
        append_images=images[:-1],
    )
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, sizes {', '.join(map(str, SIZES))})")


if __name__ == "__main__":
    main()
