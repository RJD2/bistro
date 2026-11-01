from io import BytesIO
from pathlib import Path
import os
import tempfile
import unittest
from unittest.mock import patch

from matplotlib.figure import Figure
from PIL import Image

from bistro.charts.rendering import FONT_PATH, render_statistics_chart


class GlowDonutRenderingTests(unittest.TestCase):
    def test_personal_selection_changes_the_rendered_chart(self):
        statistics = {"Музыкант": 46.0, "Слесарь": 32.0, "Шифровальщик": 16.0, "Географ": 6.0}
        first = render_statistics_chart(statistics, "Музыкант")
        second = render_statistics_chart(statistics, "Слесарь")
        self.assertNotEqual(first, second)
        self.assertEqual(
            render_statistics_chart(statistics),
            render_statistics_chart(statistics, "Несуществующий результат"),
        )

    def test_empty_single_and_eight_choice_charts_are_valid_pngs(self):
        cases = (
            ({}, None),
            ({"Музыкант": 0.0, "Слесарь": 0.0}, "Слесарь"),
            ({"Единственный результат": 100.0}, "Единственный результат"),
            ({"Музыкант": 99.5, "Слесарь": 0.5}, "Слесарь"),
            ({f"Длинный результат с кириллицей Ёё {index}": 12.5 for index in range(8)}, "Длинный результат с кириллицей Ёё 7"),
        )
        for statistics, choice in cases:
            with self.subTest(statistics=statistics, choice=choice):
                with Image.open(BytesIO(render_statistics_chart(statistics, choice))) as image:
                    self.assertEqual(image.format, "PNG")
                    self.assertGreater(image.width, 300)
                    self.assertGreater(image.height, 300)
                    image.verify()

    def test_legend_has_names_only_including_zero_percent_choices(self):
        statistics = {"Музыкант": 100.0, "Слесарь": 0.0}
        with patch.object(Figure, "legend", autospec=True) as legend:
            render_statistics_chart(statistics, "Музыкант")
        handles = legend.call_args.kwargs["handles"]
        self.assertEqual([handle.get_label() for handle in handles], list(statistics))
        self.assertEqual(Path(legend.call_args.kwargs["prop"].get_file()), FONT_PATH)

    def test_bundled_font_does_not_depend_on_working_directory(self):
        self.assertTrue(FONT_PATH.is_file())
        self.assertTrue(FONT_PATH.with_name("OFL.txt").is_file())
        previous = Path.cwd()
        with tempfile.TemporaryDirectory() as temporary:
            try:
                os.chdir(temporary)
                image = render_statistics_chart({"Слесарь": 100.0}, "Слесарь")
            finally:
                os.chdir(previous)
        self.assertTrue(image.startswith(b"\x89PNG\r\n\x1a\n"))


if __name__ == "__main__":
    unittest.main()
