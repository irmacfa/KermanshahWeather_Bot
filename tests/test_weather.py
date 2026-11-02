"""Regression tests for the weather formatting and calculation helpers."""

import unittest

from core.weather import (
    _circular_mean_deg,
    compute_dual_estimate,
    format_models_message,
)


class WeatherHelpersTests(unittest.TestCase):
    def test_circular_mean_handles_north_boundary(self):
        result = _circular_mean_deg([359.0, 1.0])
        self.assertTrue(result < 1.0 or result > 359.0)

    def test_dual_estimate_averages_gfs_and_icon(self):
        models = [
            {"model": "GFS", "speed": 10.0, "direction": 359.0, "gust": 15.0,
             "cloud": 20.0, "precip": 0.0, "snow": 0.0, "temp": 20.0, "code": 1},
            {"model": "ICON", "speed": 20.0, "direction": 1.0, "gust": 25.0,
             "cloud": 40.0, "precip": 0.0, "snow": 0.0, "temp": 22.0, "code": 1},
        ]
        result = compute_dual_estimate(models)
        self.assertEqual(result["method"], "gfs_icon_avg")
        self.assertEqual(result["speed"], 15.0)
        self.assertEqual(result["gust"], 20.0)
        self.assertTrue(result["direction"] < 1.0 or result["direction"] > 359.0)

    def test_requester_name_is_escaped_for_html(self):
        message = format_models_message(
            "ویس قرنی",
            "الان",
            [{"theme": "x", "flag": "", "model": "GFS", "country": "USA",
              "code": 0, "cloud": 0, "precip": 0, "snow": 0, "temp": 20,
              "speed": 10, "gust": None, "direction": 0}],
            requested_by="<b>intruder</b>",
        )
        self.assertIn("&lt;b&gt;intruder&lt;/b&gt;", message)
        self.assertNotIn("<b>intruder</b>", message)


if __name__ == "__main__":
    unittest.main()
