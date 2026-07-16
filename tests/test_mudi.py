"""MudiUI unit tests — stdlib unittest only (no pytest on the box or the dev machine).

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import os
import sys
import unittest

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import mudi  # noqa: E402


def all_bg(img):
    """True if nothing was painted — every pixel is still the theme background.

    Uses numpy (already a hard dependency of mudi) rather than Image.getdata(), which is
    deprecated in Pillow 12 and removed in Pillow 14."""
    return bool((np.asarray(img) == mudi.Theme.BG).all())


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


METRIC_PAGES = ("SignalPage", "WifiPage", "SystemPage", "EthernetPage")


def build_page(name, style):
    a = mudi.MockApp()
    a.settings.vals["graph_style"] = style
    return getattr(mudi, name)(a)


def only(page, cls):
    """The page's single widget of type cls (fails loudly if there isn't exactly one)."""
    found = [w for w in page.widgets if isinstance(w, cls)]
    assert len(found) == 1, "expected 1 %s, got %d" % (cls.__name__, len(found))
    return found[0]


class TestMetricPageLayout(unittest.TestCase):
    def test_all_metric_pages_subclass_metricpage(self):
        for name in METRIC_PAGES:
            self.assertTrue(issubclass(getattr(mudi, name), mudi.MetricPage), name)

    def test_page_builds_the_selected_style(self):
        for style, cls in (("hero", mudi.HeroGraph), ("arc", mudi.ArcGauge)):
            for name in METRIC_PAGES:
                self.assertIsInstance(only(build_page(name, style), mudi.Gauge), cls,
                                      "%s / %s" % (name, style))

    def test_hero_layout_matches_todays_signal_page(self):
        for name in METRIC_PAGES:
            p = build_page(name, "hero")
            self.assertEqual(only(p, mudi.StatsRow).y, 172, name)
            self.assertEqual(only(p, mudi.InfoPanel).y, 208, name)
            self.assertEqual([w for w in p.widgets if isinstance(w, mudi.Trace)], [], name)

    def test_arc_layout(self):
        for name in METRIC_PAGES:
            p = build_page(name, "arc")
            self.assertEqual(only(p, mudi.StatsRow).y, 150, name)
            self.assertEqual(only(p, mudi.InfoPanel).y, 186, name)
            trace = only(p, mudi.Trace)
            self.assertEqual((trace.y, trace.h), (270, 36), name)

    def test_arc_trace_graphs_the_pages_declared_series(self):
        for name, series in (("SignalPage", "signal.rsrp"), ("WifiPage", "wifi.signal"),
                             ("SystemPage", "sys.load"), ("EthernetPage", "eth.rxn")):
            self.assertEqual(only(build_page(name, "arc"), mudi.Trace).k, series, name)

    def test_hero_binds_the_pages_declared_series(self):
        for name, series in (("SignalPage", "signal.rsrp"), ("WifiPage", "wifi.signal"),
                             ("SystemPage", "sys.load"), ("EthernetPage", "eth.rxn")):
            self.assertEqual(only(build_page(name, "hero"), mudi.HeroGraph).k_series, series, name)

    def test_system_and_ethernet_label_their_curve(self):
        self.assertEqual(only(build_page("SystemPage", "hero"), mudi.Gauge).series_label, "LOAD")
        self.assertEqual(only(build_page("EthernetPage", "hero"), mudi.Gauge).series_label, "RX")

    def test_signal_and_wifi_need_no_curve_label(self):
        for name in ("SignalPage", "WifiPage"):
            self.assertIsNone(only(build_page(name, "hero"), mudi.Gauge).series_label, name)

    def test_every_page_and_style_actually_paints(self):
        from PIL import Image, ImageDraw
        for style in ("hero", "arc"):
            for name in METRIC_PAGES:
                p = build_page(name, style)
                p.wire()
                img = Image.new("RGB", (mudi.W, mudi.H), mudi.Theme.BG)
                p.draw(ImageDraw.Draw(img), mudi.Theme)
                self.assertFalse(all_bg(img),
                                 "%s/%s drew nothing but background" % (name, style))


if __name__ == "__main__":
    unittest.main()
