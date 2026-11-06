from amzvid.models import DraftScene, DraftShort, Product, ProductImage, ScriptDraft
from amzvid.plan import assign_images, choose_overlays
from amzvid.publish.metadata import DISCLOSURE, build_chapters, timestamp
from amzvid.sources import canonical_url, ensure_tag, extract_asin


def test_extract_asin_from_dp_url():
    assert extract_asin("https://www.amazon.com/dp/B08N5WRWNW?tag=x-20") == "B08N5WRWNW"


def test_extract_asin_from_gp_product_url():
    assert extract_asin("https://www.amazon.com/gp/product/B08N5WRWNW/ref=foo") == "B08N5WRWNW"


def test_extract_asin_from_long_slug_url():
    url = "https://www.amazon.com/Some-Product-Name-Here/dp/B01LXVKQ5X/ref=sr_1_3?keywords=x"
    assert extract_asin(url) == "B01LXVKQ5X"


def test_extract_asin_from_bare_asin():
    assert extract_asin("B08N5WRWNW") == "B08N5WRWNW"


def test_extract_asin_returns_none_when_absent():
    assert extract_asin("https://example.com/nothing/here") is None


def test_ensure_tag_adds_and_does_not_duplicate():
    once = ensure_tag("https://www.amazon.com/dp/B0", "t-20")
    assert once.endswith("?tag=t-20")
    assert ensure_tag(once, "t-20") == once


def test_canonical_url_includes_tag():
    assert canonical_url("B08N5WRWNW", "t-20") == "https://www.amazon.com/dp/B08N5WRWNW?tag=t-20"


def _product(n_images=3, price="$29.99"):
    return Product(
        asin="B0TEST",
        title="Acme Widget Pro, 3-Pack, Stainless Steel - Blue",
        affiliate_url="https://www.amazon.com/dp/B0TEST?tag=t-20",
        images=[ProductImage(path=f"/tmp/img{i}.jpg", width=1000, height=1000) for i in range(n_images)],
        price=price,
    )


def test_short_title_cuts_the_keyword_soup():
    assert _product().short_title == "Acme Widget Pro"


def test_hero_image_carries_hook_and_verdict():
    product = _product()
    images = assign_images(product, ["hook", "feature", "spec", "verdict", "cta"])
    hero = product.images[0].path
    assert images[0] == hero and images[3] == hero and images[4] == hero
    assert images[1] != hero


def test_image_assignment_cycles_when_photos_run_short():
    product = _product(n_images=2)
    images = assign_images(product, ["feature"] * 5)
    assert len(set(images)) == 1  # only one detail photo exists, so it repeats


def test_assign_images_requires_at_least_one_photo():
    try:
        assign_images(_product(n_images=0), ["hook"])
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def _draft(kinds):
    return ScriptDraft(
        scenes=[DraftScene(narration="x y z", kind=k, on_screen_text="Label" if k == "spec" else "") for k in kinds],
        pros=["a", "b", "c"],
        cons=["d"],
        verdict="Buy it if you need one.",
        youtube_title="T",
        youtube_description="D",
        tags=["t"],
        shorts=[DraftShort(title="S", scene_indices=[0, 1])],
        thumbnail_text="WORTH IT",
    )


def test_pros_cons_card_lands_on_the_con_scene():
    kinds = ["hook", "spec", "con", "verdict", "cta"]
    overlays = choose_overlays(_draft(kinds), _product())
    assert overlays[2] == "pros_cons"
    assert overlays[1] == "lower_third"


def test_price_overlay_is_placed_exactly_once():
    kinds = ["hook", "feature", "con", "verdict", "cta"]
    overlays = choose_overlays(_draft(kinds), _product())
    assert overlays.count("price") == 1


def test_no_price_overlay_without_a_price():
    overlays = choose_overlays(_draft(["hook", "feature", "cta"]), _product(price=""))
    assert "price" not in overlays


def test_timestamp_formats():
    assert timestamp(0) == "0:00"
    assert timestamp(75) == "1:15"
    assert timestamp(3725) == "1:02:05"


def test_chapters_respect_the_ten_second_minimum():
    from amzvid.models import PlannedScene, RenderPlan

    kinds = ["hook", "feature", "spec", "con", "verdict", "cta"]
    scenes = [
        PlannedScene(index=i, kind=k, narration="x", image="/tmp/a.jpg", move="zoom_in", duration=6.0)
        for i, k in enumerate(kinds)
    ]
    plan = RenderPlan(job_id="j", product=_product(), draft=_draft(kinds), scenes=scenes, cuts=[])
    chapters = build_chapters(plan, [6.0] * 6)
    assert len(chapters) == 3
    gaps = [b[0] - a[0] for a, b in zip(chapters, chapters[1:])]
    assert all(g >= 10.0 for g in gaps)


def test_chapters_are_dropped_when_too_few():
    from amzvid.models import PlannedScene, RenderPlan

    kinds = ["hook", "cta"]
    scenes = [
        PlannedScene(index=i, kind=k, narration="x", image="/tmp/a.jpg", move="zoom_in", duration=6.0)
        for i, k in enumerate(kinds)
    ]
    plan = RenderPlan(job_id="j", product=_product(), draft=_draft(kinds), scenes=scenes, cuts=[])
    assert build_chapters(plan, [6.0, 6.0]) == []


def test_disclosure_leads_the_description():
    from amzvid.config import Config, Paths, VoiceConfig
    from amzvid.models import PlannedScene, RenderPlan
    from amzvid.publish.metadata import build_metadata
    from pathlib import Path

    cfg = Config(
        paths=Paths(work=Path("/tmp"), output=Path("/tmp"), db=Path("/tmp/x.db"), assets=Path("/tmp")),
        voice=VoiceConfig(api_key="k", voice_id="v"),
        telegram=None,
        affiliate_tag="t-20",
    )
    kinds = ["hook", "feature", "con", "verdict", "cta"]
    scenes = [
        PlannedScene(index=i, kind=k, narration="x", image="/tmp/a.jpg", move="zoom_in", duration=6.0)
        for i, k in enumerate(kinds)
    ]
    plan = RenderPlan(job_id="j", product=_product(), draft=_draft(kinds), scenes=scenes, cuts=[])
    meta = build_metadata(cfg, plan, [6.0] * 5)
    assert meta.description.startswith(DISCLOSURE)
    assert plan.product.affiliate_url in meta.description
