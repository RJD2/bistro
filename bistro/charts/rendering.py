from io import BytesIO
from textwrap import fill
from threading import Lock

import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.colors import to_rgb
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Patch, Wedge


COLORS = (
    "#2563eb",
    "#f59e0b",
    "#16a34a",
    "#9333ea",
    "#dc2626",
    "#0891b2",
    "#db2777",
    "#64748b",
)
_RENDER_LOCK = Lock()


def _gradient(start, end) -> np.ndarray:
    coordinates = np.linspace(0, 1, 320)
    blend = (coordinates[:, None] + coordinates[None, :]) / 2
    start, end = np.asarray(to_rgb(start)), np.asarray(to_rgb(end))
    return start + blend[..., None] * (end - start)


def render_statistics_chart(statistics: dict[str, float]) -> bytes:
    """Render answer percentages as a PNG, including unselected choices in the legend"""
    with _RENDER_LOCK:
        return _render_statistics_chart(statistics)


def _render_statistics_chart(statistics: dict[str, float]) -> bytes:
    font = FontProperties(family="DejaVu Sans", size=12)
    entries = list(statistics.items())
    colors = [COLORS[index % len(COLORS)] for index in range(len(entries))]
    labels = [
        fill(f"{choice}: {percentage:.0f}%", width=48)
        for choice, percentage in entries
    ]
    legend_lines = sum(label.count("\n") + 1 for label in labels)
    legend_height = legend_lines * 0.26 + len(entries) * 0.14
    height = 5.1 + legend_height
    figure = Figure(figsize=(7, height), dpi=140, facecolor="#f4f7ff")
    FigureCanvasAgg(figure)
    background = figure.add_axes((0, 0, 1, 1), zorder=0)
    background.imshow(
        _gradient("#ffffff", "#eaf0ff"),
        extent=(0, 1, 0, 1), aspect="auto", interpolation="bilinear",
    )
    background.set_axis_off()
    axes = figure.add_axes((0.06, (legend_height + 0.3) / height, 0.88, 4.6 / height))
    axes.set_facecolor("none")

    for offset, alpha in ((0.045, 0.025), (0.025, 0.045)):
        axes.add_patch(Wedge(
            (0, -offset), 1.015, 0, 360, width=0.4,
            facecolor="#284475", edgecolor="none", alpha=alpha, zorder=1,
        ))

    selected = [
        (percentage, color)
        for (_, percentage), color in zip(entries, colors)
        if percentage > 0
    ]
    if selected:
        values, selected_colors = zip(*selected)
        wedges, _, percentages = axes.pie(
            values,
            colors=selected_colors,
            startangle=90,
            counterclock=False,
            autopct=lambda percentage: f"{percentage:.0f}%" if percentage >= 5 else "",
            pctdistance=0.81,
            wedgeprops={"width": 0.38, "linewidth": 2.5, "edgecolor": "white", "zorder": 3},
            textprops={"fontproperties": font, "color": "white", "weight": "bold"},
        )
        for wedge, color in zip(wedges, selected_colors):
            base = np.asarray(to_rgb(color))
            axes.imshow(
                _gradient(base * 0.82 + 0.18, base * 0.72),
                extent=(-1, 1, -1, 1), interpolation="bilinear",
                clip_path=wedge, clip_on=True, zorder=2,
            )
            wedge.set_facecolor("none")
        for percentage in percentages:
            percentage.set_zorder(4)
            percentage.set_bbox({
                "boxstyle": "round,pad=0.32,rounding_size=0.6",
                "facecolor": "#172b4d", "edgecolor": "none", "alpha": 0.64,
            })
    else:
        axes.add_patch(Wedge(
            (0, 0), 1, 0, 360, width=0.38,
            facecolor="#dce5f5", edgecolor="white", linewidth=2.5, zorder=2,
        ))
        axes.text(
            0, 0, "Пока нет\nответов", ha="center", va="center",
            fontproperties=font, color="#64748b", linespacing=1.6,
        )

    if entries:
        figure.legend(
            handles=[
                Patch(facecolor=color, edgecolor="none", label=label)
                for color, label in zip(colors, labels)
            ],
            loc="upper center",
            bbox_to_anchor=(0.5, (legend_height + 0.2) / height),
            frameon=False,
            prop=font,
            labelcolor="#243552",
            handlelength=1,
            handleheight=1,
            labelspacing=0.9,
            borderaxespad=0,
        )
    axes.set_xlim(-1.15, 1.15)
    axes.set_ylim(-1.15, 1.15)
    axes.set_aspect("equal")
    axes.set_axis_off()

    with BytesIO() as image:
        figure.savefig(image, format="png", bbox_inches="tight", pad_inches=0)
        return image.getvalue()
