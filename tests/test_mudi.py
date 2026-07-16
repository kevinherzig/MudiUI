"""MudiUI unit tests — stdlib unittest only (no pytest on the box or the dev machine).

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import mudi  # noqa: E402


class TestStyleRegistry(unittest.TestCase):
    def test_registry_order_drives_the_stepper(self):
        self.assertEqual(list(mudi.GAUGE_STYLES), ["hero", "arc"])

    def test_registry_maps_slugs_to_classes(self):
        self.assertIs(mudi.GAUGE_STYLES["hero"], mudi.HeroGraph)
        self.assertIs(mudi.GAUGE_STYLES["arc"], mudi.ArcGauge)

    def test_styles_have_labels(self):
        self.assertEqual(mudi.HeroGraph.LABEL, "Hero")
        self.assertEqual(mudi.ArcGauge.LABEL, "Arc")

    def test_hero_supplies_history_arc_does_not(self):
        self.assertTrue(mudi.HeroGraph.SUPPLIES_HISTORY)
        self.assertFalse(mudi.ArcGauge.SUPPLIES_HISTORY)

    def test_slot_geometry(self):
        self.assertEqual(
            (mudi.HeroGraph.TOP, mudi.HeroGraph.HEIGHT, mudi.HeroGraph.STACK_Y), (32, 118, 172))
        self.assertEqual(
            (mudi.ArcGauge.TOP, mudi.ArcGauge.HEIGHT, mudi.ArcGauge.STACK_Y), (42, 100, 150))

    def test_both_styles_subclass_gauge(self):
        self.assertTrue(issubclass(mudi.HeroGraph, mudi.Gauge))
        self.assertTrue(issubclass(mudi.ArcGauge, mudi.Gauge))


class TestGraphStyleSetting(unittest.TestCase):
    def test_default_is_hero(self):
        self.assertEqual(mudi.Settings.DEFAULTS["graph_style"], "hero")

    def test_gauge_cls_resolves_from_settings(self):
        a = mudi.MockApp()
        a.settings.vals["graph_style"] = "arc"
        self.assertIs(a.gauge_cls(), mudi.ArcGauge)
        a.settings.vals["graph_style"] = "hero"
        self.assertIs(a.gauge_cls(), mudi.HeroGraph)

    def test_gauge_cls_falls_back_to_hero_on_junk(self):
        a = mudi.MockApp()
        a.settings.vals["graph_style"] = "not-a-style"
        self.assertIs(a.gauge_cls(), mudi.HeroGraph)


class TestMockApp(unittest.TestCase):
    def test_serves_mock_data_to_subscribers(self):
        seen = []
        mudi.MockApp().subscribe("signal.rsrp", seen.append)
        self.assertEqual(seen, [mudi.MOCK_DATA["signal.rsrp"]])

    def test_unknown_key_is_a_no_op(self):
        seen = []
        mudi.MockApp().subscribe("nope.nothing", seen.append)
        self.assertEqual(seen, [])


if __name__ == "__main__":
    unittest.main()
