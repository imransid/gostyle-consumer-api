"""
GET and POST /api/v1/me/gallery, DELETE /api/v1/me/gallery/<id>, against
docs/my-gallery-fe-contract.md.

Everything is real: our tables, and real photos in an in-memory file store
(so the size is read and the grid copy is made as on the server).
"""

import uuid
from datetime import timedelta
from io import BytesIO

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import ExifTags, Image
from pillow_heif import register_heif_opener
from rest_framework.test import APIClient

from apps.accounts.models import ConsumerAccount
from apps.accounts.services import tokens_for
from apps.gallery import service
from apps.gallery.models import GalleryItem
from apps.reviews.models import Review, ReviewPhoto
from apps.uploads.models import FileType, UploadedFile

register_heif_opener()

URL = "/api/v1/me/gallery"
IN_MEMORY = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}
ITEM_KEYS = ["id", "category", "image_url", "thumbnail_url", "width", "height", "can_delete", "created_at"]


def photo_bytes(size=(1080, 1350), fmt="JPEG", orientation=None):
    image = Image.new("RGB", size, "teal")
    out = BytesIO()
    if orientation:
        exif = Image.Exif()
        exif[ExifTags.Base.Orientation] = orientation
        image.save(out, fmt, exif=exif.tobytes())
    else:
        image.save(out, fmt)
    return out.getvalue()


@override_settings(MY_GALLERY_V1=True, STORAGES=IN_MEMORY)
class GalleryTestCase(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.sara = ConsumerAccount.objects.create(phone="+971500000201")
        self.omar = ConsumerAccount.objects.create(phone="+971500000202")
        self.sign_in(self.sara)

    def sign_in(self, account):
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {tokens_for(account)['access']}")

    def upload(self, account=None, content=None, file_type=FileType.IMAGE, ext="jpg"):
        """A file as POST /upload-files leaves it: its row and its bytes in the store."""
        row = UploadedFile(file_type=file_type, original_name=f"cut.{ext}", uploaded_by=account or self.sara)
        row.file.save(f"{row.id}.{ext}", ContentFile(photo_bytes() if content is None else content))
        return row

    def reference(self, category="haircut", account=None, ago=timedelta(0)):
        """A reference saved straight in the table (no size read)."""
        account = account or self.sara
        return GalleryItem.objects.create(
            customer=account,
            file=self.upload(account),
            category=category,
            created_at=timezone.now() - ago,
        )

    def moment(self, account=None, ago=timedelta(0), photos=1):
        """A review of the customer's with photos: their Moments."""
        account = account or self.sara
        review = Review.objects.create(
            booking_id=uuid.uuid4(),
            customer=account,
            salon_id=uuid.uuid4(),
            salon_stars=5,
            submitted_at=timezone.now() - ago,
        )
        return [
            ReviewPhoto.objects.create(review=review, file=self.upload(account), position=i) for i in range(photos)
        ]

    def add(self, body):
        return self.client.post(URL, body, format="json")

    def errors(self, response):
        return [(e["field"], e["code"]) for e in response.json()["errors"]]


class SwitchAndAuthTests(GalleryTestCase):
    @override_settings(MY_GALLERY_V1=False)
    def test_off_all_three_are_a_404(self):
        item = self.reference()
        self.assertEqual(self.client.get(URL).status_code, 404)
        self.assertEqual(self.add({"file_id": str(self.upload().id), "category": "haircut"}).status_code, 404)
        self.assertEqual(self.client.delete(f"{URL}/{item.id}").status_code, 404)
        self.assertTrue(GalleryItem.objects.filter(pk=item.pk).exists())

    def test_a_guest_has_no_gallery(self):
        self.client.credentials()
        item = self.reference()
        self.assertEqual(self.client.get(URL).status_code, 401)
        self.assertEqual(self.add({"file_id": str(self.upload().id), "category": "haircut"}).status_code, 401)
        self.assertEqual(self.client.delete(f"{URL}/{item.id}").status_code, 401)

    def test_an_unverified_customer_can_use_it(self):
        # Only POST /upload-files needs a verified account (the doc's 2.1).
        self.assertFalse(self.sara.account_verified)
        self.assertEqual(self.client.get(URL).status_code, 200)


class ReadTests(GalleryTestCase):
    def test_an_empty_gallery(self):
        body = self.client.get(URL).json()
        self.assertEqual(
            body,
            {
                "count": 0,
                "next": None,
                "previous": None,
                "results": [],
                "categories": [
                    {"code": "haircut", "label": "Haircut", "count": 0, "can_add": True},
                    {"code": "beard", "label": "Beard", "count": 0, "can_add": True},
                    {"code": "moment", "label": "Moments", "count": 0, "can_add": False},
                ],
                "limits": {"max_references": 100},
            },
        )

    def test_a_reference_row_as_in_the_doc(self):
        item = self.reference()
        item.created_at = timezone.datetime(2026, 11, 12, 13, 41, 7, 123456, tzinfo=timezone.get_fixed_timezone(240))
        item.width, item.height = 1080, 1350
        item.save()

        row = self.client.get(URL).json()["results"][0]
        self.assertEqual(list(row), ITEM_KEYS)
        self.assertEqual(row["id"], str(item.id))
        self.assertNotEqual(row["id"], str(item.file_id))
        self.assertEqual(row["category"], "haircut")
        self.assertEqual(row["image_url"], f"http://testserver/media/{item.file.file.name}")
        self.assertIsNone(row["thumbnail_url"])
        self.assertEqual((row["width"], row["height"]), (1080, 1350))
        self.assertIs(row["can_delete"], True)
        self.assertEqual(row["created_at"], "2026-11-12T09:41:07Z")

    def test_a_moment_row(self):
        (photo,) = self.moment()
        row = self.client.get(URL).json()["results"][0]
        self.assertEqual(list(row), ITEM_KEYS)
        self.assertEqual(row["category"], "moment")
        self.assertEqual(row["id"], str(service.moment_id(photo.file_id)))
        self.assertNotEqual(row["id"], str(photo.file_id))
        self.assertEqual(row["image_url"], f"http://testserver/media/{photo.file.file.name}")
        self.assertIs(row["can_delete"], False)
        self.assertEqual((row["thumbnail_url"], row["width"], row["height"]), (None, None, None))

    def test_newest_first_every_category_mixed(self):
        old_cut = self.reference("haircut", ago=timedelta(days=3))
        (moment,) = self.moment(ago=timedelta(days=2))
        beard = self.reference("beard", ago=timedelta(days=1))
        new_cut = self.reference("haircut")

        body = self.client.get(URL).json()
        self.assertEqual(body["count"], 4)
        self.assertEqual(
            [row["id"] for row in body["results"]],
            [str(new_cut.id), str(beard.id), str(service.moment_id(moment.file_id)), str(old_cut.id)],
        )
        self.assertEqual(
            [(c["code"], c["count"]) for c in body["categories"]],
            [("haircut", 2), ("beard", 1), ("moment", 1)],
        )

    def test_one_category(self):
        self.reference("haircut")
        beard = self.reference("beard")
        self.moment(photos=2)

        body = self.client.get(f"{URL}?category=beard").json()
        self.assertEqual(body["count"], 1)
        self.assertEqual([row["id"] for row in body["results"]], [str(beard.id)])
        # The chips never depend on the filter.
        self.assertEqual([c["count"] for c in body["categories"]], [1, 1, 2])

        moments = self.client.get(f"{URL}?category=moment").json()
        self.assertEqual(moments["count"], 2)
        self.assertEqual({row["category"] for row in moments["results"]}, {"moment"})

    def test_only_the_customers_own(self):
        self.reference(account=self.omar)
        self.moment(account=self.omar)
        mine = self.reference()

        body = self.client.get(URL).json()
        self.assertEqual(body["count"], 1)
        self.assertEqual([row["id"] for row in body["results"]], [str(mine.id)])
        self.assertEqual([c["count"] for c in body["categories"]], [1, 0, 0])

    def test_an_unknown_category_is_a_422(self):
        for bad in ("nails", "Haircut", ""):
            with self.subTest(bad=bad):
                response = self.client.get(f"{URL}?category={bad}")
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.errors(response), [("category", "invalid_category")])


class PagingTests(GalleryTestCase):
    def setUp(self):
        super().setUp()
        self.items = [self.reference(ago=timedelta(minutes=i)) for i in range(25)]

    def ids(self, body):
        return [row["id"] for row in body["results"]]

    def test_pages_like_get_bookings(self):
        first = self.client.get(f"{URL}?pageSize=10").json()
        self.assertEqual(first["count"], 25)
        self.assertEqual(self.ids(first), [str(item.id) for item in self.items[:10]])
        self.assertEqual(first["next"], "http://testserver/api/v1/me/gallery?page=2&pageSize=10")
        self.assertIsNone(first["previous"])

        last = self.client.get(f"{URL}?page=3&pageSize=10").json()
        self.assertEqual(self.ids(last), [str(item.id) for item in self.items[20:]])
        self.assertIsNone(last["next"])
        self.assertEqual(last["previous"], "http://testserver/api/v1/me/gallery?page=2&pageSize=10")

    def test_the_links_keep_the_category(self):
        body = self.client.get(f"{URL}?category=haircut&pageSize=10&page=2").json()
        self.assertEqual(body["next"], "http://testserver/api/v1/me/gallery?category=haircut&page=3&pageSize=10")
        self.assertEqual(body["previous"], "http://testserver/api/v1/me/gallery?category=haircut&page=1&pageSize=10")

    def test_default_20_capped_at_50_and_bad_numbers_are_the_default(self):
        self.assertEqual(len(self.client.get(URL).json()["results"]), 20)
        for query, size in (("pageSize=51", 25), ("pageSize=abc", 20), ("pageSize=0", 20), ("page_size=5", 5)):
            with self.subTest(query=query):
                self.assertEqual(len(self.client.get(f"{URL}?{query}").json()["results"]), size)
        self.assertIn("pageSize=50", self.client.get(f"{URL}?pageSize=99&page=2").json()["previous"])
        self.assertEqual(self.ids(self.client.get(f"{URL}?page=abc&pageSize=5").json()), self.ids(
            self.client.get(f"{URL}?page=1&pageSize=5").json()
        ))

    def test_past_the_end_is_an_empty_page(self):
        body = self.client.get(f"{URL}?page=9&pageSize=10").json()
        self.assertEqual((body["count"], body["results"], body["next"]), (25, [], None))
        self.assertEqual(body["previous"], "http://testserver/api/v1/me/gallery?page=8&pageSize=10")

    def test_ties_never_repeat_or_skip(self):
        same = timezone.now() - timedelta(days=1)
        GalleryItem.objects.update(created_at=same)
        seen = []
        for page in (1, 2, 3):
            seen += self.ids(self.client.get(f"{URL}?page={page}&pageSize=10").json())
        self.assertEqual(sorted(seen), sorted(str(item.id) for item in self.items))


class AddTests(GalleryTestCase):
    def test_the_docs_example(self):
        upload = self.upload(content=photo_bytes((1080, 1350)))
        response = self.add({"file_id": str(upload.id), "category": "haircut"})
        self.assertEqual(response.status_code, 201)

        item = GalleryItem.objects.get()
        body = response.json()
        self.assertEqual(list(body), ITEM_KEYS)
        self.assertEqual(body["id"], str(item.id))
        self.assertEqual(body["category"], "haircut")
        self.assertEqual(body["image_url"], f"http://testserver/media/{upload.file.name}")
        self.assertEqual(body["thumbnail_url"], f"http://testserver/media/gallery/{item.id}-480.jpg")
        self.assertEqual((body["width"], body["height"]), (1080, 1350))
        self.assertIs(body["can_delete"], True)
        self.assertRegex(body["created_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")

        # Exactly one row of results[], at the top.
        self.assertEqual(self.client.get(URL).json()["results"], [body])

        with default_storage.open(item.thumbnail.name) as stored, Image.open(stored) as thumbnail:
            self.assertEqual((thumbnail.format, thumbnail.size), ("JPEG", (480, 600)))

    def test_a_photo_taken_upright_is_tall(self):
        # Stored on its side with an EXIF quarter turn, as phones do.
        upload = self.upload(content=photo_bytes((1350, 1080), orientation=6))
        body = self.add({"file_id": str(upload.id), "category": "beard"}).json()
        self.assertEqual((body["width"], body["height"]), (1080, 1350))
        item = GalleryItem.objects.get()
        with default_storage.open(item.thumbnail.name) as stored, Image.open(stored) as thumbnail:
            self.assertEqual(thumbnail.size, (480, 600))

    def test_heic_png_webp_and_avif(self):
        for fmt, ext in (("HEIF", "heic"), ("PNG", "png"), ("WEBP", "webp"), ("AVIF", "avif")):
            with self.subTest(fmt=fmt):
                upload = self.upload(content=photo_bytes((960, 1280), fmt=fmt), ext=ext)
                body = self.add({"file_id": str(upload.id), "category": "haircut"}).json()
                self.assertEqual((body["width"], body["height"]), (960, 1280))
                self.assertTrue(body["thumbnail_url"].endswith("-480.jpg"))

    def test_a_small_photo_has_no_grid_copy(self):
        upload = self.upload(content=photo_bytes((480, 640)))
        body = self.add({"file_id": str(upload.id), "category": "haircut"}).json()
        self.assertEqual((body["width"], body["height"], body["thumbnail_url"]), (480, 640, None))

    def test_a_photo_that_cant_be_read_is_still_added_with_nulls(self):
        upload = self.upload(content=b"\xff\xd8\xff not really a jpeg")
        with self.assertLogs("apps.gallery.images", "WARNING"):
            response = self.add({"file_id": str(upload.id), "category": "haircut"})
        self.assertEqual(response.status_code, 201)
        body = response.json()
        self.assertEqual((body["width"], body["height"], body["thumbnail_url"]), (None, None, None))
        self.assertEqual(GalleryItem.objects.count(), 1)

    def test_invalid_file(self):
        pdf = self.upload(file_type=FileType.PDF, ext="pdf")
        omars = self.upload(account=self.omar)
        for file_id in (str(uuid.uuid4()), str(omars.id), str(pdf.id), "not-a-uuid", 7, None):
            with self.subTest(file_id=file_id):
                response = self.add({"file_id": file_id, "category": "haircut"})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.errors(response), [("file_id", "invalid_file")])
        response = self.add({"category": "haircut"})
        self.assertEqual(self.errors(response), [("file_id", "invalid_file")])
        self.assertFalse(GalleryItem.objects.exists())

    def test_invalid_category(self):
        upload = self.upload()
        for category in ("moment", "nails", "Haircut", None, ["haircut"]):
            with self.subTest(category=category):
                response = self.add({"file_id": str(upload.id), "category": category})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(self.errors(response), [("category", "invalid_category")])
        self.assertEqual(
            self.add({"file_id": str(upload.id), "category": "moment"}).json()["errors"][0]["message"],
            "Photos can't be added to this category.",
        )
        self.assertFalse(GalleryItem.objects.exists())

    def test_both_wrong_says_both(self):
        response = self.add({"file_id": str(uuid.uuid4()), "category": "nails"})
        self.assertEqual(self.errors(response), [("file_id", "invalid_file"), ("category", "invalid_category")])
        self.assertEqual(self.add([]).status_code, 422)

    def test_the_same_file_twice_is_a_409_with_nothing_saved(self):
        upload = self.upload()
        self.assertEqual(self.add({"file_id": str(upload.id), "category": "haircut"}).status_code, 201)
        response = self.add({"file_id": str(upload.id), "category": "beard"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.errors(response), [("file_id", "already_in_gallery")])
        self.assertEqual(list(GalleryItem.objects.values_list("category", flat=True)), ["haircut"])

    def test_a_moments_photo_is_already_in_the_gallery(self):
        (photo,) = self.moment()
        response = self.add({"file_id": str(photo.file_id), "category": "haircut"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.errors(response), [("file_id", "already_in_gallery")])

    def test_gallery_full(self):
        for _ in range(service.MAX_REFERENCES):
            self.reference()
        response = self.add({"file_id": str(self.upload().id), "category": "haircut"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["errors"],
            [
                {
                    "field": None,
                    "code": "gallery_full",
                    "message": "You can keep up to 100 photos. Remove one to add another.",
                }
            ],
        )
        self.assertEqual(GalleryItem.objects.count(), service.MAX_REFERENCES)

    def test_moments_do_not_count(self):
        for _ in range(service.MAX_REFERENCES - 1):
            self.reference()
        self.moment(photos=3)
        response = self.add({"file_id": str(self.upload().id), "category": "haircut"})
        self.assertEqual(response.status_code, 201)

    def test_full_says_already_in_gallery_for_a_retry(self):
        for _ in range(service.MAX_REFERENCES - 1):
            self.reference()
        upload = self.upload()
        self.assertEqual(self.add({"file_id": str(upload.id), "category": "haircut"}).status_code, 201)
        response = self.add({"file_id": str(upload.id), "category": "haircut"})
        self.assertEqual(self.errors(response), [("file_id", "already_in_gallery")])


class RemoveTests(GalleryTestCase):
    def added(self):
        upload = self.upload(content=photo_bytes((1080, 1350)))
        self.add({"file_id": str(upload.id), "category": "haircut"})
        return GalleryItem.objects.get(file=upload)

    def test_remove_a_reference(self):
        item = self.added()
        names = [item.file.file.name, item.thumbnail.name]
        self.assertTrue(all(default_storage.exists(name) for name in names))

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.delete(f"{URL}/{item.id}")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertFalse(GalleryItem.objects.exists())
        # The photo goes too: its row and its files.
        self.assertFalse(UploadedFile.objects.filter(pk=item.file_id).exists())
        self.assertFalse(any(default_storage.exists(name) for name in names))
        self.assertEqual(self.client.get(URL).json()["count"], 0)

    def test_again_is_a_404(self):
        item = self.added()
        self.client.delete(f"{URL}/{item.id}")
        response = self.client.delete(f"{URL}/{item.id}")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.errors(response), [(None, "gallery_item_not_found")])

    def test_someone_elses_or_no_such_item_is_the_same_404(self):
        omars = self.reference(account=self.omar)
        for item_id in (omars.id, uuid.uuid4(), "not-a-uuid", omars.file_id):
            with self.subTest(item_id=item_id):
                response = self.client.delete(f"{URL}/{item_id}")
                self.assertEqual(response.status_code, 404)
                self.assertEqual(self.errors(response), [(None, "gallery_item_not_found")])
        self.assertTrue(GalleryItem.objects.filter(pk=omars.pk).exists())

    def test_a_moment_is_not_deletable(self):
        (photo,) = self.moment()
        response = self.client.delete(f"{URL}/{service.moment_id(photo.file_id)}")
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.errors(response), [(None, "not_deletable")])
        self.assertTrue(ReviewPhoto.objects.filter(pk=photo.pk).exists())
        # Someone else's moment is a 404, as any item of theirs.
        (omars,) = self.moment(account=self.omar)
        self.assertEqual(self.client.delete(f"{URL}/{service.moment_id(omars.file_id)}").status_code, 404)

    def test_a_photo_also_on_a_review_keeps_its_file(self):
        item = self.added()
        review = Review.objects.create(booking_id=uuid.uuid4(), customer=self.sara, salon_id=uuid.uuid4(), salon_stars=4)
        ReviewPhoto.objects.create(review=review, file=item.file, position=0)
        with self.captureOnCommitCallbacks(execute=True):
            self.assertEqual(self.client.delete(f"{URL}/{item.id}").status_code, 204)
        self.assertTrue(UploadedFile.objects.filter(pk=item.file_id).exists())
        self.assertTrue(default_storage.exists(item.file.file.name))
        self.assertFalse(default_storage.exists(item.thumbnail.name))


class AccountDeletionTests(GalleryTestCase):
    def test_the_purge_deletes_the_gallery_and_its_files(self):
        upload = self.upload(content=photo_bytes((1080, 1350)))
        self.add({"file_id": str(upload.id), "category": "haircut"})
        item = GalleryItem.objects.get()
        names = [item.file.file.name, item.thumbnail.name]
        (moment,) = self.moment()
        omars = self.reference(account=self.omar)

        with self.captureOnCommitCallbacks(execute=True):
            self.sara.purge()

        self.assertFalse(GalleryItem.objects.filter(customer=self.sara).exists())
        self.assertFalse(UploadedFile.objects.filter(pk=upload.pk).exists())
        self.assertFalse(any(default_storage.exists(name) for name in names))
        # A review keeps its photos; someone else's gallery is untouched.
        self.assertTrue(ReviewPhoto.objects.filter(pk=moment.pk).exists())
        self.assertTrue(GalleryItem.objects.filter(pk=omars.pk).exists())
