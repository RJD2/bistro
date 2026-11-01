from io import BytesIO
from pathlib import Path
from textwrap import fill
from threading import Lock

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import to_rgb
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Patch, Wedge
from PIL import Image, ImageFilter

from bistro.ui.text_formatting import capitalize_first


COLORS = (
    "#2563eb", "#f59e0b", "#16a34a", "#9333ea",
    "#dc2626", "#0891b2", "#db2777", "#64748b",
)
GRADIENT_COLORS = (
    ("#00d9f4", "#0044ff"),
    ("#fff500", "#ff9800"),
    ("#79efad", "#16a34a"),
    ("#d58aff", "#7931e8"),
    ("#ff8b9c", "#e63357"),
    ("#56e9ee", "#008caa"),
    ("#ff9ae3", "#c82eae"),
    ("#b8caec", "#566f9c"),
)
FONT_PATH = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "Nunito-Medium.ttf"
_INNER_RADIUS = 0.38
_RENDER_LOCK = Lock()


def _gradient(start, end) -> np.ndarray:
    coordinates = np.linspace(0, 1, 320)
    blend = (coordinates[:, None] + coordinates[None, :]) / 2
    start, end = np.asarray(to_rgb(start)), np.asarray(to_rgb(end))
    return start + blend[..., None] * (end - start)


def _soft_wedge(axes, center, theta1, theta2, color, *, blur, alpha, spread=0, zorder=1):
    """Blur an annular mask so the highlight never fills the donut's centre."""
    limit, size = 1.3, 480
    coordinates = np.linspace(-limit, limit, size)
    x, y = np.meshgrid(coordinates - center[0], coordinates - center[1])
    radius = np.hypot(x, y)
    angle = (np.degrees(np.arctan2(y, x)) - theta1) % 360
    mask = (
        (radius <= 1 + spread)
        & (radius >= max(0, _INNER_RADIUS - spread))
        & (angle <= theta2 - theta1 + 1e-8)
    )
    blurred = Image.fromarray((mask * 255).astype(np.uint8)).filter(
        ImageFilter.GaussianBlur(blur * size / (limit * 2))
    )
    rgba = np.empty((size, size, 4), dtype=float)
    rgba[..., :3] = to_rgb(color)
    rgba[..., 3] = np.asarray(blurred) / 255 * alpha
    axes.imshow(
        rgba, extent=(-limit, limit, -limit, limit), origin="lower",
        interpolation="bilinear", zorder=zorder,
    )


def render_statistics_chart(
    statistics: dict[str, float], selected_choice: str | None = None,
) -> bytes:
    """Render the thick 'Сияние' donut and the recipient's selected answer."""
    with _RENDER_LOCK:
        return _render_statistics_chart(statistics, selected_choice)


def _render_statistics_chart(
    statistics: dict[str, float], selected_choice: str | None = None,
) -> bytes:
    legend_font = FontProperties(fname=str(FONT_PATH), size=13, weight="medium")
    percentage_font = FontProperties(family="DejaVu Sans", size=12)
    entries = list(statistics.items())
    labels = [fill(capitalize_first(choice), width=42, break_on_hyphens=False) for choice, _ in entries]
    legend_lines = sum(label.count("\n") + 1 for label in labels)
    legend_height = legend_lines * 0.28 + len(entries) * 0.14
    height = 5.1 + legend_height
    figure = Figure(figsize=(7, height), dpi=140, facecolor="#f4f7ff")
    FigureCanvasAgg(figure)
    background = figure.add_axes((0, 0, 1, 1), zorder=0)
    background.imshow(
        _gradient("#ffffff", "#eaf0ff"), extent=(0, 1, 0, 1),
        aspect="auto", interpolation="bilinear",
    )
    background.set_axis_off()
    axes = figure.add_axes((0.06, (legend_height + 0.3) / height, 0.88, 4.6 / height))
    axes.set_facecolor("none")

    positive = [
        (index, choice, percentage)
        for index, (choice, percentage) in enumerate(entries)
        if np.isfinite(percentage) and percentage > 0
    ]
    total = sum(percentage for _, _, percentage in positive)
    sectors = []
    angle = 90.0
    for index, choice, value in positive:
        percentage = value / total * 100
        theta1, theta2 = angle - percentage * 3.6, angle
        middle = np.radians((theta1 + theta2) / 2)
        selected = choice == selected_choice
        center = np.array((np.cos(middle), np.sin(middle))) * (0.055 if selected else 0)
        sectors.append((index, percentage, theta1, theta2, middle, selected, center))
        angle = theta1

    for index, percentage, theta1, theta2, middle, selected, center in sorted(
        sectors, key=lambda sector: sector[5],
    ):
        start, end = GRADIENT_COLORS[index % len(GRADIENT_COLORS)]
        start, end = np.asarray(to_rgb(start)), np.asarray(to_rgb(end))
        if selected:
            _soft_wedge(
                axes, center + (0, -0.025), theta1, theta2, "#26063d",
                blur=0.035, alpha=0.14, zorder=1,
            )
            _soft_wedge(
                axes, center, theta1, theta2, (start + end) / 2,
                blur=0.05, alpha=0.60, spread=0.025, zorder=2,
            )
        else:
            start, end = start * 0.88 + 0.12, end * 0.88 + 0.12
        wedge = Wedge(
            center, 1, theta1, theta2, width=1 - _INNER_RADIUS,
            facecolor="none", edgecolor="none", zorder=4 if selected else 3,
        )
        axes.add_patch(wedge)
        bounds = wedge.get_path().get_extents()
        axes.imshow(
            _gradient(start, end), extent=(bounds.x0, bounds.x1, bounds.y0, bounds.y1),
            origin="upper", interpolation="bilinear", clip_path=wedge,
            clip_on=True, zorder=4 if selected else 3,
        )

    for index, percentage, theta1, theta2, middle, selected, center in sectors:
        if percentage < 5 and not selected:
            continue
        number = "<1%" if percentage < 1 else f"{percentage:.0f}%"
        text = f"✓ {number}" if selected else number
        bbox = {
            "boxstyle": "round,pad=0.34,rounding_size=0.55", "facecolor": "#26063d",
            "edgecolor": "none", "alpha": 0.96 if selected else 0.78,
        }
        if percentage < 5:
            direction = 1 if np.cos(middle) >= 0 else -1
            axes.annotate(
                text, xy=center + np.array((np.cos(middle), np.sin(middle))) * 1.02,
                xytext=(direction * 1.15, np.sin(middle) * 1.15),
                ha="right" if direction > 0 else "left", va="center",
                fontproperties=percentage_font, color="white", bbox=bbox, zorder=6,
                arrowprops={"arrowstyle": "-", "color": "#26063d", "linewidth": 0.9},
            )
        else:
            distance = 0.78 if percentage < 10 else 0.70
            position = center + np.array((np.cos(middle), np.sin(middle))) * distance
            axes.text(
                *position, text, ha="center", va="center", fontproperties=percentage_font,
                color="white", bbox=bbox, zorder=6,
            )

    if not sectors:
        axes.add_patch(Wedge(
            (0, 0), 1, 0, 360, width=1 - _INNER_RADIUS,
            facecolor="#dce5f5", edgecolor="none", zorder=3,
        ))
        axes.text(
            0, 0, "Пока нет\nответов", ha="center", va="center",
            fontproperties=legend_font, color="#64748b", linespacing=1.5,
        )

    if entries:
        figure.legend(
            handles=[
                Patch(facecolor=COLORS[index % len(COLORS)], edgecolor="none", label=label)
                for index, label in enumerate(labels)
            ],
            loc="upper center", bbox_to_anchor=(0.5, (legend_height + 0.2) / height),
            frameon=False, prop=legend_font, labelcolor="#243552",
            handlelength=1, handleheight=1, labelspacing=0.9, borderaxespad=0,
        )
    axes.set_xlim(-1.25, 1.25)
    axes.set_ylim(-1.25, 1.25)
    axes.set_aspect("equal")
    axes.set_axis_off()
    with BytesIO() as image:
        figure.savefig(image, format="png", bbox_inches="tight", pad_inches=0)
        return image.getvalue()
