"""
The Smart Search's lists and word chips (SEARCH_OPTIONS_V1,
docs/SEARCH_OPTIONS_API.md): the search_tag table and its seed, GET
/search/options, and the quick and service_tags params of /search/salons
and /search/services.

search_tag is this service's own table, so it exists in the test database
(with the seed migration's rows). The platform's tables do not: their
querysets are inspected or mocked, never run.
"""

import importlib
import types
import uuid
from unittest import mock

from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.db.models import Exists
from django.http import QueryDict
from django.test import SimpleTestCase, TestCase, override_settings

from apps.salons import admin as salons_admin
from apps.salons import map_filters, search_options, search_results, search_views
from apps.salons.models import SearchTag
from apps.salons.search_results import SALON_SORTS, SearchQuery, parse_search

seed = importlib.import_module("apps.salons.migrations.0004_seed_search_tags")

OPTIONS_URL = "/api/v1/search/options"
SALONS_URL = "/api/v1/search/salons"
CUTS = uuid.UUID(int=11)
COLOUR = uuid.UUID(int=12)
CATEGORIES = {
    CUTS: {"id": CUTS, "name_en": "Haircut and Styling", "parent_id": None, "icon": None},
    COLOUR: {"id": COLOUR, "name_en": "Colour", "parent_id": None, "icon": None},
}


def menu_row(name, category=None, tags=()):
    return types.SimpleNamespace(service={"name": name, "category_0_id": category, "tags": list(tags)})


class SeedTests(SimpleTestCase):
    """Migration 0004: the Figma's chips, and nothing that waits on the client."""

    def test_the_figma_s_chips(self):
        by_group = {}
        for group, slug, label, words in seed.TAGS:
            by_group.setdefault(group, []).append(label)
            self.assertTrue(words, slug)
        self.assertEqual(by_group, {
            "quick": ["Cuts & Fade", "Beard & Shave", "Color & Styles"],
            "cuts_styling": ["Fade", "Buzz", "Scissor"],
            "beard_shave": ["Beard Trim", "Hot Towel", "Shave"],
            "color_treatments": ["Grey Blend", "Highlights", "Keratin"],
        })

    def test_no_points_chip_while_d5_waits(self):
        self.assertFalse([t for t in seed.TAGS if "pts" in t[2].lower() or "offer" in t[2].lower()])

    def test_slugs_are_unique_and_every_group_is_a_choice(self):
        slugs = [slug for _, slug, _, _ in seed.TAGS]
        self.assertEqual(len(slugs), len(set(slugs)))
        self.assertTrue({group for group, *_ in seed.TAGS} <= set(SearchTag.Group.values))


class SeededTableTests(TestCase):
    """The seed reached the table, and the admin's checks."""

    def setUp(self):
        cache.clear()

    def test_the_rows_and_their_order(self):
        tags = search_results.active_tags()
        self.assertEqual(list(tags)[:3], ["beard_trim", "hot_towel", "shave"])
        self.assertEqual(tags["cuts_fade"], {
            "group": "quick", "label": "Cuts & Fade", "label_ar": None, "icon": None, "terms": ("cut", "fade"),
        })
        self.assertEqual(len(tags), 12)

    def test_inactive_or_wordless_chips_are_left_out(self):
        SearchTag.objects.filter(slug="buzz").update(active=False)
        SearchTag.objects.filter(slug="scissor").update(match_terms=["  "])
        tags = search_results.active_tags()
        self.assertNotIn("buzz", tags)
        self.assertNotIn("scissor", tags)

    def test_the_list_is_cached_and_forget_drops_it(self):
        search_results.active_tags()
        SearchTag.objects.filter(slug="buzz").update(active=False)
        self.assertIn("buzz", search_results.active_tags())
        search_options.forget()
        self.assertNotIn("buzz", search_results.active_tags())

    def test_clean_wants_a_list_of_words(self):
        tag = SearchTag(group="quick", slug="x", label_en="X")
        for bad in ([], "fade", [""], [3]):
            tag.match_terms = bad
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                tag.clean()
        tag.match_terms = ["  skin   fade "]
        tag.clean()
        self.assertEqual(tag.match_terms, ["skin fade"])

    def test_saving_in_the_admin_clears_the_cache(self):
        page = salons_admin.SearchTagAdmin(SearchTag, salons_admin.admin.site)
        tag = SearchTag.objects.get(slug="buzz")
        with mock.patch.object(search_options, "forget") as forget:
            page.save_model(None, tag, None, True)
        forget.assert_called_once_with()


TAGS = {
    "cuts_fade": {"group": "quick", "label": "Cuts & Fade", "label_ar": None, "icon": None, "terms": ("cut", "fade")},
    "color_styles": {"group": "quick", "label": "Color & Styles", "label_ar": None, "icon": None, "terms": ("colour",)},
    "fade": {"group": "cuts_styling", "label": "Fade", "label_ar": None, "icon": "scissors", "terms": ("fade",)},
    "keratin": {"group": "color_treatments", "label": "Keratin", "label_ar": None, "icon": None, "terms": ("keratin",)},
}


def query(text, sorts=SALON_SORTS):
    with mock.patch.object(search_results, "active_tags", return_value=TAGS):
        return parse_search(QueryDict(text), sorts, "rating")


class ParamsTests(SimpleTestCase):
    """quick and service_tags: read only with the switch on; a gone id is ignored."""

    def test_off_they_are_not_read(self):
        found = query("quick=cuts_fade&service_tags=fade")
        self.assertEqual((found.quick, found.quick_terms, found.service_tags, found.tag_terms), (None, (), (), ()))

    @override_settings(SEARCH_OPTIONS_V1=True)
    def test_on_the_chips_and_their_words(self):
        found = query("quick=Cuts_Fade&service_tags=fade,keratin,gone")
        self.assertEqual(found.quick, "cuts_fade")
        self.assertEqual(found.quick_terms, ("cut", "fade"))
        self.assertEqual(found.service_tags, ("fade", "keratin"))
        self.assertEqual(found.tag_terms, ("fade", "keratin"))

    @override_settings(SEARCH_OPTIONS_V1=True)
    def test_each_param_takes_its_own_kind_only(self):
        found = query("quick=fade&service_tags=cuts_fade")
        self.assertEqual((found.quick, found.service_tags), (None, ()))

    @override_settings(SEARCH_OPTIONS_V1=True)
    def test_any_or_nothing_reads_no_table(self):
        with mock.patch.object(search_results, "active_tags") as tags:
            parse_search(QueryDict("quick=any"), SALON_SORTS, "rating")
        tags.assert_not_called()


class MatchingTests(SimpleTestCase):
    """A chip matches a service when one of its words is in its name, chip label or tags."""

    def row(self, name, category=None, tags=()):
        return types.SimpleNamespace(service={"name": name, "category_0_id": category, "tags": list(tags)})

    def test_by_name_label_or_tag(self):
        self.assertTrue(search_results.words_match(self.row("Skin Fade"), CATEGORIES, ("buzz", "fade")))
        self.assertTrue(search_results.words_match(self.row("Classic", CUTS), CATEGORIES, ("cut",)))
        self.assertTrue(search_results.words_match(self.row("Classic", tags=["Hot Towel"]), CATEGORIES, ("hot towel",)))
        self.assertFalse(search_results.words_match(self.row("Classic", COLOUR), CATEGORIES, ("cut", "fade")))

    def test_words_where_is_service_text_word_by_word(self):
        where = search_results.words_where(("fade", "buzz"), CATEGORIES)
        self.assertEqual(where.connector, "OR")
        # Django flattens the ORs: name, chip label and tag, for each word.
        self.assertEqual(
            [child[0] for child in where.children],
            ["name__icontains", "category_0_id__in", "tags__icontains"] * 2,
        )
        self.assertEqual([child[1] for child in where.children][::3], ["fade", "buzz"])


def exists_count(qs):
    return sum(isinstance(getattr(c, "lhs", None), Exists) for c in qs.query.where.children)


class FilterTests(SimpleTestCase):
    """The chips narrow the salons (SQL) and the services."""

    def build(self, found):
        with mock.patch.object(search_results, "public_categories", return_value=CATEGORIES), \
                mock.patch.object(map_filters, "category_catalog", return_value={}):
            return search_results.salon_queryset(found)[0]

    def test_no_chip_no_filter(self):
        self.assertEqual(exists_count(self.build(SearchQuery())), 0)

    def test_quick_and_tags_each_their_own_exists(self):
        self.assertEqual(exists_count(self.build(SearchQuery(quick_terms=("cut",)))), 1)
        self.assertEqual(exists_count(self.build(SearchQuery(quick_terms=("cut",), tag_terms=("fade",)))), 2)

    def test_the_services_tab_reads_the_words_too(self):
        found = SearchQuery(quick_terms=("cut",), tag_terms=("keratin",))
        with mock.patch.object(search_results, "Service") as service:
            qs = service.objects.filter.return_value
            qs.filter.return_value = qs
            qs.values.return_value = []
            search_results.menu_rows([uuid.uuid4()], found, CATEGORIES, None)
        self.assertEqual(qs.filter.call_count, 2)

    def test_service_list_keeps_the_services_that_match_both(self):
        cut = types.SimpleNamespace(salon={}, service={"name": "Kids Cut", "category_0_id": None, "tags": []})
        keratin_cut = types.SimpleNamespace(salon={}, service={"name": "Cut", "category_0_id": None, "tags": ["Keratin"]})
        found = SearchQuery(quick_terms=("cut",), tag_terms=("keratin",))
        with mock.patch.object(search_results, "salon_queryset", return_value=(mock.MagicMock(), None)), \
                mock.patch.object(search_results, "categories_for_tenants", return_value={}), \
                mock.patch.object(search_results, "menu_rows", return_value=[]), \
                mock.patch.object(search_results, "branch_service_rows", return_value={}), \
                mock.patch.object(search_results.service_search, "listing", return_value=[cut, keratin_cut]):
            rows, _ = search_results.service_list(found)
        self.assertEqual(rows, [keratin_cut])


class BuildTests(SimpleTestCase):
    """search_options.build: the sections, only what has data."""

    def build(self, data=None, menu=None):
        data = data if data is not None else {"near_me": True, "packages": True, "parking": True, "free_parking": False}
        menu = menu if menu is not None else [menu_row("Skin Fade"), menu_row("Classic", CUTS)]
        return {s["key"]: s for s in search_options.build(TAGS, data, menu, CATEGORIES)["sections"]}

    def test_the_sections_in_order_with_their_shape(self):
        body = search_options.build(TAGS, {"near_me": True}, [menu_row("Skin Fade")], CATEGORIES)
        self.assertEqual([s["key"] for s in body["sections"]], ["tab", "quick", "service_tags"])
        sections = self.build()
        self.assertEqual(sections["tab"], {
            "key": "tab", "title": "Search in", "multiple": False, "default": ["near_me"],
            "options": [{"id": "near_me", "label": "Near Me"}, {"id": "packages", "label": "Packages"}],
        })
        # Packages Use: the same data as the Packages tab.
        self.assertEqual(
            sections["amenities"]["options"],
            [{"id": "parking", "label": "Parking"}, {"id": "packages", "label": "Packages Use"}],
        )
        self.assertTrue(sections["amenities"]["multiple"])

    def test_a_chip_no_service_matches_is_left_out(self):
        sections = self.build()
        self.assertEqual([o["id"] for o in sections["quick"]["options"]], ["cuts_fade"])
        self.assertEqual(sections["service_tags"]["groups"], [{
            "key": "cuts_styling", "title": "Cuts & Styling",
            "options": [{"id": "fade", "label": "Fade", "icon": "scissors"}],
        }])

    def test_no_data_at_all_leaves_only_near_me(self):
        body = search_options.build(TAGS, {"near_me": True}, [], CATEGORIES)
        self.assertEqual(body, {"sections": [{
            "key": "tab", "title": "Search in", "multiple": False, "default": ["near_me"],
            "options": [{"id": "near_me", "label": "Near Me"}],
        }]})

    def test_nothing_waiting_on_the_client_is_ever_offered(self):
        sections = self.build(data={k: True for k in ("near_me", "packages", "parking", "free_parking", "mall", "home")})
        offered = {o["id"] for s in sections.values() for o in s.get("options", [])}
        for waiting in ("mall", "home", "verified", "membership_card", "metro_near", "offers"):
            self.assertNotIn(waiting, offered)

    def test_the_options_are_cached(self):
        cache.delete(search_options.CACHE_KEY)
        with mock.patch.object(search_options, "build", return_value={"sections": []}) as built, \
                mock.patch.object(search_results, "active_tags", return_value={}), \
                mock.patch.object(search_options, "has_data", return_value={}), \
                mock.patch.object(search_options, "public_menu", return_value=[]), \
                mock.patch.object(search_results, "public_categories", return_value={}):
            search_options.options()
            search_options.options()
        built.assert_called_once()
        cache.delete(search_options.CACHE_KEY)


class OptionsViewTests(SimpleTestCase):
    def test_off_it_is_404(self):
        self.assertEqual(self.client.get(OPTIONS_URL).status_code, 404)

    @override_settings(SEARCH_OPTIONS_V1=True)
    def test_on_it_answers_the_lists(self):
        with mock.patch.object(search_options, "options", return_value={"sections": []}):
            response = self.client.get(OPTIONS_URL)
        self.assertEqual((response.status_code, response.json()), (200, {"sections": []}))


@override_settings(SEARCH_RESULTS_V1=True, SEARCH_OPTIONS_V1=True)
class ResultsViewTests(SimpleTestCase):
    """The results routes pass the chips on."""

    def test_the_salons_tab_gets_the_chips(self):
        with mock.patch.object(search_results, "active_tags", return_value=TAGS), \
                mock.patch.object(search_results, "salons", return_value=[]) as salons, \
                mock.patch.object(search_views, "SalonCardSerializer") as card:
            card.return_value.data = []
            response = self.client.get(f"{SALONS_URL}?quick=cuts_fade&service_tags=keratin")
        self.assertEqual(response.status_code, 200)
        found = salons.call_args.args[0]
        self.assertEqual((found.quick_terms, found.tag_terms), (("cut", "fade"), ("keratin",)))
