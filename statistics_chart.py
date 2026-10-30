from io import BytesIO
from textwrap import fill
from threading import Lock

from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.font_manager import FontProperties
from matplotlib.patches import Patch


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


def render_statistics_chart(statistics: dict[str, float]) -> bytes:
    """Render answer percentages as a PNG, including unselected choices in the legend."""
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
    figure = Figure(figsize=(7, 5 + legend_lines * 0.3), dpi=140, facecolor="white")
    FigureCanvasAgg(figure)
    axes = figure.subplots()
    axes.set_title("Распределение ответов", fontproperties=font, fontsize=16, pad=18)

    selected = [
        (percentage, color)
        for (_, percentage), color in zip(entries, colors)
        if percentage > 0
    ]
    if selected:
        values, selected_colors = zip(*selected)
        axes.pie(
            values,
            colors=selected_colors,
            startangle=90,
            counterclock=False,
            autopct=lambda percentage: f"{percentage:.0f}%" if percentage >= 5 else "",
            pctdistance=0.7,
            wedgeprops={"linewidth": 2, "edgecolor": "white"},
            textprops={"fontproperties": font, "color": "black"},
        )
    else:
        axes.text(
            0.5, 0.5, "Пока нет ответов", ha="center", va="center",
            transform=axes.transAxes, fontproperties=font,
        )
        axes.set_axis_off()

    if entries:
        axes.legend(
            handles=[
                Patch(facecolor=color, label=label)
                for color, label in zip(colors, labels)
            ],
            loc="upper center",
            bbox_to_anchor=(0.5, -0.03),
            frameon=False,
            prop=font,
        )
    axes.set_aspect("equal")

    with BytesIO() as image:
        figure.savefig(image, format="png", bbox_inches="tight", facecolor="white")
        return image.getvalue()
