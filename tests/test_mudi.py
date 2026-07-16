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


class TestHeroSeriesLabel(unittest.TestCase):
    LABEL_BAND = (12, mudi.HeroGraph.TOP + 66, 90, mudi.HeroGraph.TOP + 78)

    def render(self, series_label):
        from PIL import Image, ImageDraw
        g = mudi.HeroGraph(mudi.MockApp(), value="batt.pct", series="sys.load",
                           unit="%  BATTERY", series_label=series_label)
        g.hist = [1.0, 2.0, 1.5, 3.0, 2.5]
        img = Image.new("RGB", (mudi.W, mudi.H), mudi.Theme.BG)
        g.draw(ImageDraw.Draw(img), mudi.Theme)
        return img

    def test_label_changes_the_frame(self):
        self.assertNotEqual(self.render("LOAD").tobytes(), self.render(None).tobytes())

    def test_label_band_is_empty_without_a_label(self):
        band = self.render(None).crop(self.LABEL_BAND)
        self.assertTrue(all_bg(band))

    def test_label_band_is_painted_with_a_label(self):
        band = self.render("LOAD").crop(self.LABEL_BAND)
        self.assertFalse(all_bg(band))

    def test_label_needs_history_to_mean_anything(self):
        from PIL import Image, ImageDraw
        g = mudi.HeroGraph(mudi.MockApp(), value="batt.pct", series="sys.load",
                           unit="%  BATTERY", series_label="LOAD")
        img = Image.new("RGB", (mudi.W, mudi.H), mudi.Theme.BG)
        g.draw(ImageDraw.Draw(img), mudi.Theme)          # hist empty -> must not raise
        self.assertTrue(all_bg(img.crop(self.LABEL_BAND)))


class TestGraphStyleRow(unittest.TestCase):
    def row(self):
        page = mudi.SettingsPage(mudi.MockApp())
        rows = getattr(page, "rows", None) or page.widgets
        found = [r for r in rows
                 if isinstance(r, mudi.StepperRow) and r.skey == "graph_style"]
        self.assertEqual(len(found), 1, "expected exactly one graph_style row")
        return found[0]

    def test_row_offers_every_registered_style(self):
        self.assertEqual(self.row().options, list(mudi.GAUGE_STYLES))

    def test_row_shows_the_style_labels(self):
        r = self.row()
        self.assertEqual([r.fmt(v) for v in r.options], ["Hero", "Arc"])

    def test_row_wraps(self):
        self.assertTrue(self.row().wrap)

    def test_row_is_not_confirm_gated(self):
        self.assertFalse(self.row().confirm)             # cosmetic, instant, reversible


class TestMetricPageRebuild(unittest.TestCase):
    def app(self):
        a = mudi.MockApp()
        a.pages = [mudi.SignalPage(a), mudi.WifiPage(a), mudi.SystemPage(a),
                   mudi.EthernetPage(a), mudi.SettingsPage(a)]
        a.idx = 4
        a.current = a.pages[4]
        return a

    def test_rebuild_swaps_every_metric_page_to_the_new_style(self):
        a = self.app()
        for p in a.pages[:4]:
            self.assertIsInstance(only(p, mudi.Gauge), mudi.HeroGraph)
        a.settings.vals["graph_style"] = "arc"
        a._rebuild_metric_pages()
        for p in a.pages[:4]:
            self.assertIsInstance(only(p, mudi.Gauge), mudi.ArcGauge)

    def test_rebuild_preserves_the_settings_page_instance(self):
        a = self.app()
        settings_page = a.pages[4]
        a.settings.vals["graph_style"] = "arc"
        a._rebuild_metric_pages()
        self.assertIs(a.pages[4], settings_page)         # scroll pos + tapped row must survive
        self.assertIs(a.current, settings_page)

    def test_rebuild_never_leaves_current_dangling(self):
        a = self.app()
        a.idx = 0
        a.current = a.pages[0]
        a.settings.vals["graph_style"] = "arc"
        a._rebuild_metric_pages()
        self.assertIs(a.current, a.pages[0])             # render thread must never see a stale page
        self.assertIsInstance(only(a.current, mudi.Gauge), mudi.ArcGauge)

    def test_rebuild_keeps_page_count_and_order(self):
        a = self.app()
        before = [type(p) for p in a.pages]
        a.settings.vals["graph_style"] = "arc"
        a._rebuild_metric_pages()
        self.assertEqual([type(p) for p in a.pages], before)


class TestApplySettingRouting(unittest.TestCase):
    def test_graph_style_triggers_a_rebuild(self):
        class SpyApp(mudi.MockApp):
            apply_setting = mudi.App.apply_setting       # exercise the real router
            def __init__(self):
                super().__init__()
                self.rebuilds = 0
            def _rebuild_metric_pages(self):
                self.rebuilds += 1
        a = SpyApp()
        a.apply_setting("graph_style", "arc")
        self.assertEqual(a.rebuilds, 1)

    def test_other_settings_do_not_rebuild(self):
        class SpyApp(mudi.MockApp):
            apply_setting = mudi.App.apply_setting
            def __init__(self):
                super().__init__()
                self.rebuilds = 0
            def _rebuild_metric_pages(self):
                self.rebuilds += 1
        a = SpyApp()
        a.apply_setting("screen_timeout", "60")
        self.assertEqual(a.rebuilds, 0)


def fake_scroll_page(app, n_rows):
    """A ScrollPage with a known row count, so scroll math doesn't depend on the settings list."""
    class _Page(mudi.ScrollPage):
        def build(self):
            self.add(mudi.Banner(self.app, "Fake"))
            y = 0
            for i in range(n_rows):
                self.add_row(mudi.ActionRow(self.app, "row %d" % i, lambda: None).place(y))
                y += mudi.Row.H
            self.content_h = y
    return _Page(app)


class TestScrollMath(unittest.TestCase):
    def test_short_page_does_not_scroll(self):
        p = fake_scroll_page(mudi.MockApp(), 3)
        self.assertFalse(p.scrollable())
        self.assertEqual(p.max_scroll(), 0)

    def test_long_page_scrolls_by_the_overflow(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        self.assertEqual(p.content_h, 20 * mudi.Row.H)
        self.assertTrue(p.scrollable())
        self.assertEqual(p.max_scroll(), 20 * mudi.Row.H - p.VIEW_H)

    def test_scroll_clamps_at_both_ends(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        p.scroll_to(-500)
        self.assertEqual(p.scroll_y, 0)
        p.scroll_to(99999)
        self.assertEqual(p.scroll_y, p.max_scroll())

    def test_scroll_wakes_the_render_loop_only_on_change(self):
        a = mudi.MockApp()
        p = fake_scroll_page(a, 20)
        a.wake.clear()
        p.scroll_to(0)                                   # already there
        self.assertFalse(a.wake.is_set())
        p.scroll_to(10)
        self.assertTrue(a.wake.is_set())


class TestScrollTouch(unittest.TestCase):
    def test_touch_translates_screen_y_to_content_y(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        p.scroll_to(p.max_scroll())
        hits = []
        for r in p.rows:
            r.act = lambda x, r=r: hits.append(r)
        p.on_touch(20, p.VIEW_TOP + 5)
        expected = [r for r in p.rows if r.in_row(p.max_scroll() + 5)]
        self.assertEqual(hits, expected)
        self.assertEqual(len(hits), 1)

    def test_touch_above_the_viewport_hits_no_row(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        for r in p.rows:
            r.act = lambda x: self.fail("a row acted on a touch in the chrome")
        self.assertFalse(p.on_touch(20, 5))

    def test_unscrolled_touch_hits_the_row_under_the_finger(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        hits = []
        for r in p.rows:
            r.act = lambda x, r=r: hits.append(r)
        p.on_touch(20, p.VIEW_TOP + mudi.Row.H + 2)
        self.assertEqual(hits, [p.rows[1]])


class TestScrollDraw(unittest.TestCase):
    def render(self, page):
        from PIL import Image, ImageDraw
        img = Image.new("RGB", (mudi.W, mudi.H), mudi.Theme.BG)
        page.draw(ImageDraw.Draw(img), mudi.Theme, img)
        return img

    def test_half_scrolled_rows_never_bleed_into_the_chrome(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        p.scroll_to(mudi.Row.H // 2)                     # half a row above the fold
        img = self.render(p)
        gap = img.crop((0, 26, mudi.W, p.VIEW_TOP))      # between banner rule and viewport
        self.assertTrue(all_bg(gap))

    def test_scrollbar_appears_only_when_scrollable(self):
        short = self.render(fake_scroll_page(mudi.MockApp(), 3))
        long_ = self.render(fake_scroll_page(mudi.MockApp(), 20))
        strip = (mudi.W - mudi.ScrollPage.BAR_W, mudi.ScrollPage.VIEW_TOP, mudi.W, mudi.H)
        self.assertTrue(all_bg(short.crop(strip)))
        self.assertFalse(all_bg(long_.crop(strip)))

    def test_scrolling_changes_what_is_drawn(self):
        p = fake_scroll_page(mudi.MockApp(), 20)
        top = self.render(p).tobytes()
        p.scroll_to(p.max_scroll())
        self.assertNotEqual(self.render(p).tobytes(), top)


class TestSettingsPageScrolls(unittest.TestCase):
    def page(self):
        return mudi.SettingsPage(mudi.MockApp())

    def test_settings_is_a_scrollpage(self):
        self.assertIsInstance(self.page(), mudi.ScrollPage)

    def test_rows_start_at_the_content_origin(self):
        self.assertEqual(self.page().rows[0].y, 0)

    def test_content_height_covers_every_row(self):
        p = self.page()
        self.assertEqual(p.content_h, len(p.rows) * mudi.Row.H)

    def test_every_row_is_reachable_by_scrolling(self):
        p = self.page()
        p.scroll_to(p.max_scroll())
        last = p.rows[-1]
        self.assertLessEqual(last.y + mudi.Row.H - p.scroll_y, p.VIEW_H)

    def test_settings_paints_at_every_scroll_extreme(self):
        # Asserting against the whole frame would pass even if no row ever rendered, since the
        # Banner alone paints unconditionally -- crop to the viewport region so this actually
        # verifies rows painted at each scroll extreme.
        from PIL import Image, ImageDraw
        p = self.page()
        for pos in (0, p.max_scroll() // 2, p.max_scroll()):
            p.scroll_to(pos)
            img = Image.new("RGB", (mudi.W, mudi.H), mudi.Theme.BG)
            p.draw(ImageDraw.Draw(img), mudi.Theme, img)
            viewport = img.crop((0, p.VIEW_TOP, mudi.W, mudi.H))
            self.assertFalse(all_bg(viewport),
                             "settings viewport drew nothing but background at scroll %d" % pos)

    def test_last_row_is_actually_painted_at_max_scroll(self):
        """The user-visible property this whole task exists for: "About" (the last row) is
        really painted at max_scroll(), not just reachable by arithmetic."""
        from PIL import Image, ImageDraw
        p = self.page()
        p.scroll_to(p.max_scroll())
        img = Image.new("RGB", (mudi.W, mudi.H), mudi.Theme.BG)
        p.draw(ImageDraw.Draw(img), mudi.Theme, img)
        # exclude the scrollbar's column: it spans nearly the whole viewport height whenever the
        # page is scrollable, so including it would make this pass even if the row itself never
        # painted (verified: dropping the last row from self.rows still left the full-width crop
        # non-background, purely from the scrollbar).
        band = img.crop((0, mudi.H - mudi.Row.H, mudi.W - mudi.ScrollPage.BAR_W, mudi.H))
        self.assertFalse(all_bg(band),
                         "last row's band at the bottom of the viewport is unpainted background")


if __name__ == "__main__":
    unittest.main()
