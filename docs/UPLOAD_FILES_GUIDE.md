# Upload files: build guide

For: Rafa, building `POST /api/v1/upload-files` by hand, to learn.

Status: guide only. No code in the repo was changed to write it.

## How to use this guide

- Do one step. Run its check. Go to the next step only when the check passes.
- Each step says: which file, the code, what each part does, why, and what breaks without it.
- Run every command from the repo root, with the venv on: `source .venv/bin/activate`.
- When a step says "whole file", replace the file. When it says "add", add only those lines.

## What we are building

| Part | Value |
|---|---|
| Method and path | `POST /api/v1/upload-files` |
| Body | `multipart/form-data`, field `files`, one file or many |
| Auth | Bearer token of a verified account |
| Success | `201`, always a JSON array, one item per file, in the order sent |
| Item | `{ "id", "file_url", "file_type" }` |
| `file_type` | `IMAGE`, `VIDEO`, `AUDIO`, `PDF`, `DOCUMENT` |
| Errors | our usual envelope `{ "detail", "code", "errors": [...] }` |

Example answer:

```json
[
  {
    "id": "0b8f6c1e-3f7a-4b7e-9a51-2d8c3c1f9e10",
    "file_url": "https://entity-blob-storage.s3.<region>.amazonaws.com/customers/uploads/2026/09/0b8f6c1e-3f7a-4b7e-9a51-2d8c3c1f9e10.jpg",
    "file_type": "IMAGE"
  }
]
```

## Facts this guide is built on (audit, 2026-09-25)

1. **customer-api today.** No boto3, no django-storages, no file-type library. `STORAGES["default"]` is `FileSystemStorage`, `MEDIA_ROOT` is `<repo>/media`. No AWS or S3 variable names in `.env`, `.env.example` or `docker-compose.yml`.
2. **A model already exists.** `apps/uploads/models.py` has `UploadedFile` (id, file, file_type, original_name, uploaded_by, created_at). Migration `0001` is applied locally. The table is `consumer.uploads_uploadedfile`, empty. The app is already in `INSTALLED_APPS`. There is no view, no URL and no test yet.
3. **gostyle-platform.** One class, `apps/gostyle-api/src/storage.service.ts`, uploads from the server with `PutObject`. Bucket `entity-blob-storage`. Static keys. Variable names: `AWS_REGION`, `AWS_BUCKET_NAME`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`. It never sets an ACL. It builds a plain public URL: `https://{bucket}.s3.{region}.amazonaws.com/{key}`. Public read comes from the **bucket policy, per folder**, not from the upload. Its keys live under `tenants/<tenantId>/...`.
4. **nginx.** `client_max_body_size 10m` for api.gostyle.uk (`nginx/api.gostyle.uk.conf`). That is for the whole request, all files together.
5. **Docker.** `python:3.13-slim`. boto3, django-storages and filetype are all pure Python wheels. No `apt-get` needed. The image grows by about 30 MB, mostly botocore. (`python-magic` would need the system package `libmagic1`, so we do not use it.)
6. **Error envelope.** `apps/accounts/exceptions.py` turns a 400 with field errors into a 422. Gotcha: an error from the child field inside a list comes back as `"field": 0` (the list index), not `"field": "files"`. Step 7 avoids it.
7. **Swagger.** drf-spectacular draws a file inside a list as a URL string, not a file picker. The global fix (`COMPONENT_SPLIT_REQUEST`) renames every request schema in the API (`Login` becomes `LoginRequest`). So Step 10 writes the upload schema by hand.
8. **Tests.** Existing tests fake outside services with `unittest.mock` (booking-api: `mock.patch.object(booking_api.urllib.request, "urlopen", ...)` and patching helper functions in `group_views`). For files, Django has `InMemoryStorage`. Step 12 swaps it in with `override_settings`, so S3 is never called.

## Decisions (my defaults, change them if you want)

- **D1. Folder.** Our files go to `customers/` in the same bucket. Never `tenants/`: the platform treats every key under `tenants/<id>/` as that salon's, and its gallery and build checks accept such keys.
- **D2. Public URLs.** `file_url` is a plain public URL, like the platform's. Someone with AWS access must add public read for `customers/*` to the bucket policy. Step 14 checks this. Anyone with the URL can open the file, so this endpoint is not for private documents.
- **D3. Keys.** Same 4 variable names as the platform. Best: a new IAM user that may only `s3:PutObject` on `entity-blob-storage/customers/*`. Quick: copy the platform's keys.
- **D4. Region.** The platform repo gives two answers: `eu-north-1` in a hardcoded email logo URL, `me-south-1` in `.env.prod.example`. Step 14 checks the real one.
- **D5. Limits.** 5 files per request, 10 MB per file (the platform's default is also 10 MB). nginx goes from `10m` to `55m`.
- **D6. Who may upload.** `IsVerified`. Uploads cost money and create public URLs. A throwaway account should not get that. Use `IsAuthenticated` only if the app must upload before the OTP is verified.

---

## Step 1. Install the libraries

**File:** `requirements.txt`. Add at the end:

```text
boto3==1.43.102
botocore==1.43.102
s3transfer==0.19.2
jmespath==1.1.0
python-dateutil==2.9.0.post0
six==1.17.0
urllib3==2.8.0
django-storages==1.14.6
filetype==1.2.0
```

**What each one is:**

- `boto3` and `botocore`: the AWS SDK for Python. botocore does the HTTP and the signing. boto3 is the friendly layer on top.
- `s3transfer`: splits a big upload into parts. boto3 uses it by itself.
- `jmespath`, `python-dateutil`, `six`, `urllib3`: what botocore needs.
- `django-storages`: plugs S3 into Django's storage system. After this, saving a `FileField` sends the file to S3.
- `filetype`: reads the first bytes of a file and names its type. Pure Python, no system package.

**Why pin all of them:** this file pins every package, like a `pip freeze`. The Docker image must get the same versions as your laptop.

**Command:**

```bash
pip install -r requirements.txt
```

These were the latest versions on 2026-09-25. If pip cannot find one, run `pip install boto3 django-storages filetype` and copy the versions from `pip freeze`.

Note: django-storages 1.14.6 does not list Django 6 officially. I checked that its S3 backend imports and builds URLs on Django 6.0.7.

**Check:**

```bash
python -c "import boto3, storages, filetype; print('ok')"
python manage.py check
```

You should see `ok`, then `System check identified no issues`.

**Breaks without it:** Step 6 fails with `No module named 'filetype'`. In production the first upload fails with `No module named 'storages'`.

---

## Step 2. Settings: where files are stored

**File:** `config/settings/base.py`. **Add** these lines directly under the `STORAGES = {...}` block:

```python
# File uploads (apps/uploads). Same bucket and same variable names as
# gostyle-platform, so the server's .env lines can be copied from there.
# Empty means "no S3": files go to MEDIA_ROOT on this disk (local dev only).
AWS_BUCKET_NAME = env("AWS_BUCKET_NAME", default="")
AWS_REGION = env("AWS_REGION", default="")

# boto3 reads AWS_ACCESS_KEY_ID and AWS_SECRET_ACCESS_KEY from the
# environment by itself, so they are not read here.
S3_STORAGE = {
    "BACKEND": "storages.backends.s3.S3Storage",
    "OPTIONS": {
        "bucket_name": AWS_BUCKET_NAME,
        "region_name": AWS_REGION,
        # Our own top-level folder. Never "tenants/": the platform treats
        # every key under it as a salon's.
        "location": "customers",
        # Plain public URL, in the same shape the platform builds.
        "custom_domain": f"{AWS_BUCKET_NAME}.s3.{AWS_REGION}.amazonaws.com",
        "querystring_auth": False,
    },
}

if AWS_BUCKET_NAME:
    STORAGES["default"] = S3_STORAGE
```

**What each part does:**

- `env("...", default="")`: no value gives an empty string, not a crash. CI and your local tests have no AWS values, and they must still start.
- `S3_STORAGE`: a recipe for the S3 backend. Everything in `OPTIONS` is passed to django-storages' `S3Storage`.
- `bucket_name`, `region_name`: which bucket, and which AWS region to talk to.
- `location: "customers"`: a folder put in front of every key. A file becomes `customers/uploads/2026/09/<id>.png`.
- `custom_domain`: by default django-storages builds `https://bucket.s3.amazonaws.com/key`. This makes it the same shape as the platform's `https://bucket.s3.<region>.amazonaws.com/key`. The platform's code only recognises that shape.
- `querystring_auth: False`: the default (`True`) makes signed URLs that stop working after 1 hour. We store and return a URL that keeps working.
- No `default_acl`: the platform never sets one, and the bucket may refuse ACLs.
- `if AWS_BUCKET_NAME`: switch to S3 only when a bucket is set. Local dev and tests keep the disk.
- The two keys are not read here on purpose. boto3 finds `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY` in the environment by itself. Locally, django-environ has already loaded `.env` into the environment at the top of this file.

**Check:**

```bash
python manage.py shell -c "from django.core.files.storage import default_storage; print(default_storage.__class__.__name__)"
```

Expect `FileSystemStorage`, because your local `.env` has no bucket yet.

**Breaks without it:** everything is saved on the local disk, also in production.

---

## Step 3. Production always uses S3

**File:** `config/settings/production.py`. **Add** at the end:

```python
# Uploads must never land on the container disk here: two replicas and no
# volume, so a file saved on one is missing on the other and gone after the
# next deploy. With the bucket unset, an upload fails with a 503 instead.
STORAGES = {**STORAGES, "default": S3_STORAGE}  # noqa: F405
```

**What each part does:**

- Production runs 2 replicas and has no volume. If the bucket variable were missing on the server, Step 2's `if` would quietly fall back to disk. The file would be on one replica, missing on the other, and deleted at the next deploy.
- Forcing S3 here makes a missing bucket fail loud: the upload answers 503 and the error is logged. Nothing is lost quietly.
- `{**STORAGES, "default": ...}`: copies the dict and replaces only `default`. The `staticfiles` entry (WhiteNoise) stays as it is.
- `# noqa: F405`: ruff warns that these names come from `import *`. The `REST_FRAMEWORK` line above does the same.

**Check:**

```bash
DJANGO_SETTINGS_MODULE=config.settings.production DJANGO_ALLOWED_HOSTS=example.com \
  python manage.py shell -c "from django.core.files.storage import default_storage; print(default_storage.__class__.__name__)"

DJANGO_SETTINGS_MODULE=config.settings.production DJANGO_ALLOWED_HOSTS=example.com \
  python manage.py check --deploy
```

The first prints `S3Storage`. No network call happens: the backend only talks to AWS when a file is saved. The second is what CI runs. It should say the same as before your change.

**Breaks without it:** a missing server variable loses customer files without any error.

---

## Step 4. Env example and ignore files

**File:** `.env.example`. **Add** at the end:

```text
# File uploads (POST /api/v1/upload-files). Same names as gostyle-platform.
# Leave AWS_BUCKET_NAME empty locally to keep files in ./media instead.
AWS_BUCKET_NAME=
AWS_REGION=
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
```

**Files:** `.gitignore` and `.dockerignore`. **Add** one line to each:

```text
media/
```

**Why:**

- `.env.example` is the list of what a new machine needs. Without these lines, the next person does not know uploads need AWS values.
- Local uploads land in `media/`. Without the `.gitignore` line you could commit real customer files.
- Without the `.dockerignore` line, files in your local `media/` get copied into the image.

**Check:** after Step 11 has saved a file, `git status` shows nothing under `media/`.

---

## Step 5. The model

**File:** `apps/uploads/models.py` (whole file):

```python
import uuid

from django.conf import settings
from django.db import models


class FileType(models.TextChoices):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"
    AUDIO = "AUDIO"
    PDF = "PDF"
    DOCUMENT = "DOCUMENT"


class UploadedFile(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    file = models.FileField(upload_to="uploads/%Y/%m/")
    file_type = models.CharField(max_length=20, choices=FileType.choices)
    # What the bytes really are (from the file type check), not what the
    # client said. Kept for support and cleanup; the app only sees file_type.
    mime_type = models.CharField(max_length=100, default="")
    size = models.PositiveBigIntegerField(default=0)
    original_name = models.CharField(max_length=255)
    uploaded_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.original_name
```

**What changed, and why:**

- `FileType`: the five values in one place. Steps 6 and 8 reuse it, and Swagger shows them as a list of allowed values.
- `choices=FileType.choices`: the database column does not change (still `varchar(20)`). Django now knows the allowed values.
- `mime_type`: the exact type, for example `image/heic`, where `file_type` only says `IMAGE`. Useful for support and cleanup later.
- `size`: in bytes. `PositiveBigIntegerField` because `PositiveIntegerField` stops at about 2 GB, and a video can pass that one day.
- `default=""` and `default=0`: the table already exists (migration 0001 is applied, maybe on the server too). A new column that cannot be empty needs a value for old rows. Without a default, `makemigrations` stops and asks you.
- `upload_to="uploads/%Y/%m/"`: unchanged. Files go in folders by year and month.
- `uploaded_by` with `SET_NULL`: unchanged. If an account is deleted, its rows stay (see "Later").

**Commands:**

```bash
python manage.py makemigrations uploads
python manage.py sqlmigrate uploads 0002
python manage.py migrate uploads
python manage.py showmigrations uploads
```

**Check:**

- `makemigrations` creates `apps/uploads/migrations/0002_....py` with: Add field `mime_type`, Add field `size`, Alter field `file_type`.
- `sqlmigrate` shows two `ALTER TABLE ... ADD COLUMN` lines. The `file_type` part says `(no-op)`: choices live in Python only.
- `showmigrations` shows `[X] 0002_...`.

**Why a migration:** CI runs `makemigrations --check` and fails when the model and the migrations differ. Deploy runs `migrate` before the new code starts.

**Breaks without it:** every upload fails with `column "mime_type" does not exist`.

---

## Step 6. Decide the file type from the bytes

**File:** `apps/uploads/filetypes.py` (new file):

```python
"""Which files we accept, decided by their first bytes.

The file name and the Content-Type the client sends are both typed by the
client, so neither is trusted. `filetype` reads the magic bytes at the start
of the file instead. Anything it cannot name, or that is not in the list
below, is refused. That also refuses HTML, SVG and plain text, which have no
magic bytes.
"""

import filetype

from .models import FileType

ALLOWED_MIME_TYPES = {
    "image/jpeg": FileType.IMAGE,
    "image/png": FileType.IMAGE,
    "image/gif": FileType.IMAGE,
    "image/webp": FileType.IMAGE,
    "image/heic": FileType.IMAGE,
    "image/avif": FileType.IMAGE,
    "video/mp4": FileType.VIDEO,
    "video/quicktime": FileType.VIDEO,
    "video/webm": FileType.VIDEO,
    "video/3gpp": FileType.VIDEO,
    "audio/mpeg": FileType.AUDIO,
    "audio/mp4": FileType.AUDIO,
    "audio/aac": FileType.AUDIO,
    "audio/x-wav": FileType.AUDIO,
    "audio/ogg": FileType.AUDIO,
    "audio/amr": FileType.AUDIO,
    "application/pdf": FileType.PDF,
    "application/msword": FileType.DOCUMENT,
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": FileType.DOCUMENT,
    "application/vnd.ms-excel": FileType.DOCUMENT,
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": FileType.DOCUMENT,
    "application/vnd.ms-powerpoint": FileType.DOCUMENT,
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": FileType.DOCUMENT,
    "application/vnd.oasis.opendocument.text": FileType.DOCUMENT,
    "application/vnd.oasis.opendocument.spreadsheet": FileType.DOCUMENT,
    "application/vnd.oasis.opendocument.presentation": FileType.DOCUMENT,
}


def detect(upload):
    """Return {"mime", "extension", "file_type"}, or None if we refuse it."""
    kind = filetype.guess(upload)
    if kind is None or kind.mime not in ALLOWED_MIME_TYPES:
        return None
    return {
        "mime": kind.mime,
        "extension": kind.extension,
        "file_type": ALLOWED_MIME_TYPES[kind.mime],
    }
```

**What each part does:**

- Why not trust the name or the Content-Type: the client types both. `virus.exe` renamed to `photo.jpg` also says `image/jpeg`.
- `filetype.guess(upload)`: reads the first 8 KB, then moves back to the start of the file. So the upload later still sends the whole file.
- `ALLOWED_MIME_TYPES`: only the types the app needs, each mapped to our five values. filetype knows many more (psd, exe, zip). Those are refused because they are not in the dict.
- Refused for free: HTML, SVG, plain text and CSV have no magic bytes, so `guess` returns `None`. HTML and SVG can run scripts in a browser, so refusing them is good. If the app needs `.txt` or `.csv` one day, that needs its own rule.
- Office files are zip files inside. filetype looks for `word/`, `xl/` or `ppt/` near the start. A few unusual tools write those late; such a file comes back as `zip` and is refused. Rare.
- The return value: `mime` goes to S3 and the database, `extension` goes into the key, `file_type` goes into the response.

**Check:**

```bash
python manage.py shell -c "from apps.uploads.filetypes import detect; print(detect(b'\x89PNG\r\n\x1a\n' + bytes(20))); print(detect(b'%PDF-1.4 ')); print(detect(b'hello'))"
```

Expect three lines: a dict with `image/png` and `FileType.IMAGE`, a dict with `application/pdf` and `FileType.PDF`, then `None`.

**Breaks without it:** any file is accepted, and S3 serves it with whatever Content-Type the client claimed.

---

## Step 7. The request serializer

**File:** `apps/uploads/serializers.py` (new file). This is the first part:

```python
from rest_framework import serializers

from .filetypes import detect
from .models import UploadedFile

MAX_FILES = 5
MAX_FILE_MB = 10
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024


class UploadFilesSerializer(serializers.Serializer):
    # allow_empty_file=True on purpose: an empty file is refused below, in
    # validate_files, so its error names the field "files". Refused by the
    # child field, it would come back as field 0 (the list index).
    files = serializers.ListField(
        child=serializers.FileField(allow_empty_file=True),
        allow_empty=False,
        max_length=MAX_FILES,
    )

    def validate_files(self, files):
        checked = []
        for upload in files:
            if upload.size == 0:
                raise serializers.ValidationError(
                    f"{upload.name} is empty.", code="empty_file"
                )
            if upload.size > MAX_FILE_BYTES:
                raise serializers.ValidationError(
                    f"{upload.name} is bigger than {MAX_FILE_MB} MB.",
                    code="file_too_large",
                )
            found = detect(upload)
            if found is None:
                raise serializers.ValidationError(
                    f"{upload.name} is not a supported file type.",
                    code="unsupported_file_type",
                )
            checked.append({"upload": upload, **found})
        return checked
```

(`UploadedFile` is imported now because Step 8 uses it.)

**What each part does:**

- `ListField(child=FileField(...))`: `files` holds many files. DRF collects every multipart part named `files`. One file becomes a list of one. That is how the answer can always be a list.
- `allow_empty=False`: an empty list is an error. `max_length=MAX_FILES`: more than 5 is an error (`code: max_length`).
- `allow_empty_file=True`, plus our own empty check: this is the fix for the gotcha from the audit. Try removing it in Step 12 and watch a test fail with `field: 0`.
- `validate_files`: DRF calls `validate_<field name>` after the basic checks pass. We check each file: empty, too big, wrong type. The first bad file stops everything, so nothing is saved.
- Each error names the field `files` and has a stable `code` the app can branch on: `empty_file`, `file_too_large`, `unsupported_file_type`.
- `return checked`: whatever this returns becomes `validated_data["files"]`. So the view gets each file with its detected type, and nothing is detected twice.
- Why check size here when nginx has a limit: nginx limits the whole request and answers with an HTML 413 page. This check limits each file and answers with our JSON 422.

**Check** (interactive shell, type one line at a time):

```bash
python manage.py shell
```

```python
from django.core.files.uploadedfile import SimpleUploadedFile as F
from apps.uploads.serializers import UploadFilesSerializer as S
png = b"\x89PNG\r\n\x1a\n" + bytes(20)
s = S(data={"files": [F("a.png", png)]}); s.is_valid(); s.validated_data
s = S(data={"files": [F("a.txt", b"hello")]}); s.is_valid(); s.errors
s = S(data={}); s.is_valid(); s.errors
```

Expect: `True` and a list with one dict; `False` and `unsupported_file_type`; `False` and `required`.

**Breaks without it:** no limits, no type check, and errors in the wrong shape.

---

## Step 8. The response serializer

**File:** `apps/uploads/serializers.py`. **Add** at the bottom:

```python
class UploadedFileSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = UploadedFile
        fields = ["id", "file_url", "file_type"]

    def get_file_url(self, obj) -> str:
        # S3 already gives a full https URL, and this returns it unchanged.
        # Local disk gives "/media/...", and this adds the host in front.
        return self.context["request"].build_absolute_uri(obj.file.url)
```

**What each part does:**

- `ModelSerializer` with `fields`: exactly the three keys of the contract, nothing else.
- `SerializerMethodField`: `file_url` is not a column, so a method builds it.
- `-> str`: tells drf-spectacular the type. Without it, Swagger guesses and prints a warning.
- `obj.file.url`: the storage builds the URL. S3 gives `https://...`, local disk gives `/media/...`.
- `build_absolute_uri`: leaves a full URL as it is, and puts `http://127.0.0.1:8000` in front of a path. It needs the request, so the view passes it in `context`.

**Check:** nothing on its own. Step 11 shows it.

---

## Step 9. Save the files

**File:** `apps/uploads/services.py` (new file):

```python
import logging

from botocore.exceptions import BotoCoreError, ClientError
from django.db import transaction
from rest_framework import status
from rest_framework.exceptions import APIException

from .models import UploadedFile

logger = logging.getLogger(__name__)


class StorageUnavailable(APIException):
    """503 when the file store (S3) fails. Not the customer's fault."""

    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    default_detail = "File upload is temporarily unavailable. Please try again."
    default_code = "storage_unavailable"


def save_uploads(checked, user):
    """Store each checked file and its row. All rows, or none.

    If S3 fails on the third file, the rows for the first two are rolled
    back, so the table never lists a file the app was not told about. Their
    objects stay in the bucket as orphans; that is the cheaper mistake.
    """
    rows = []
    try:
        with transaction.atomic():
            for item in checked:
                upload = item["upload"]
                # django-storages sends upload.content_type to S3 as the
                # object's Content-Type. The client set it, so replace it
                # with what the bytes really are.
                upload.content_type = item["mime"]

                row = UploadedFile(
                    file_type=item["file_type"],
                    mime_type=item["mime"],
                    size=upload.size,
                    original_name=upload.name[:255],
                    uploaded_by=user,
                )
                # Our own name, never the client's: "<row id>.<ext>".
                row.file.save(f"{row.id}.{item['extension']}", upload)
                rows.append(row)
    except (BotoCoreError, ClientError) as exc:
        logger.exception("File upload to storage failed")
        raise StorageUnavailable() from exc
    return rows
```

**What each part does:**

- `StorageUnavailable`: the same pattern as `BookingApiDown` in `apps/salons/views.py`. Our envelope turns it into `503` with top `code: "service_unavailable"` and `errors[0].code: "storage_unavailable"`.
- `transaction.atomic()`: all rows or none. If file 3 fails, rows 1 and 2 are rolled back. Their S3 objects stay (orphans). A clean table matters more than a few stray objects.
- `upload.content_type = item["mime"]`: django-storages sends this to S3 as the Content-Type (I read this in its source: `_get_write_parameters`). The client set it. A PNG sent as `text/html` would be served as a web page. We replace it with the real type.
- `UploadedFile(...)`: the `id` is made in Python now (the `uuid4` default), before saving. So we can use it in the file name.
- `upload.name[:255]`: the column holds 255 characters.
- `row.file.save(name, upload)`: builds `uploads/2026/09/<id>.png`, S3 adds `customers/` in front, uploads it, then saves the row.
- Why our own name, not the client's: client names can hold personal data (`passport_john_smith.pdf`), and the URL is public. Also django-storages overwrites by default: two `image.jpg` in the same month would replace each other. A uuid never clashes, and needs no extra S3 call to check.
- `except (BotoCoreError, ClientError)`: network down, wrong key, access denied, missing bucket. We log it with the traceback (it reaches Loki) and answer 503. Any other error is a bug, so it stays a loud 500.

**Check:** nothing on its own. Steps 11 and 12 run it.

---

## Step 10. The view

**File:** `apps/uploads/views.py` (whole file):

```python
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.parsers import MultiPartParser
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.accounts.permissions import IsVerified

from . import services
from .serializers import (
    MAX_FILE_MB,
    MAX_FILES,
    UploadedFileSerializer,
    UploadFilesSerializer,
)

# Written by hand: drf-spectacular draws a FileField inside a list as a URL
# string, not a file picker, unless COMPONENT_SPLIT_REQUEST is on, and that
# setting renames every request schema in the whole API.
_UPLOAD_REQUEST = {
    "multipart/form-data": {
        "type": "object",
        "properties": {
            "files": {
                "type": "array",
                "items": {"type": "string", "format": "binary"},
                "maxItems": MAX_FILES,
            },
        },
        "required": ["files"],
    }
}


class UploadFilesView(APIView):
    """
    POST /api/v1/upload-files

    Send one or more files in the multipart field "files". The answer is
    always a list, one item per file, in the order they were sent.
    """

    permission_classes = [IsVerified]
    parser_classes = [MultiPartParser]

    @extend_schema(
        request=_UPLOAD_REQUEST,
        responses={
            201: UploadedFileSerializer(many=True),
            413: OpenApiResponse(
                description="The whole request is over nginx's limit. HTML, not JSON."
            ),
            415: OpenApiResponse(description="The body is not multipart/form-data."),
            422: OpenApiResponse(
                description=(
                    f"No files, more than {MAX_FILES}, a file over {MAX_FILE_MB} MB, "
                    "an empty file, or a type we do not accept. Nothing is saved."
                )
            ),
            503: OpenApiResponse(description="The file store failed. Try again."),
        },
    )
    def post(self, request):
        s = UploadFilesSerializer(data=request.data)
        s.is_valid(raise_exception=True)

        rows = services.save_uploads(s.validated_data["files"], user=request.user)

        out = UploadedFileSerializer(rows, many=True, context={"request": request})
        return Response(out.data, status=status.HTTP_201_CREATED)
```

**What each part does:**

- `permission_classes = [IsVerified]`: decision D6. No token gives 401, an unverified account gives 403.
- `parser_classes = [MultiPartParser]`: only multipart is accepted. A JSON body gets `415 unsupported_media_type`, already in our envelope table.
- `_UPLOAD_REQUEST`: the hand-written request schema from the audit. `"format": "binary"` is what makes Swagger show file pickers. `maxItems` reuses the constant, so docs and code cannot disagree.
- `responses`: `201` is an array of `UploadedFileSerializer`. The other codes are written down so the app developers know what to handle, including nginx's HTML 413.
- `post`: validate, save, serialize, answer `201`. Same shape as the views in `apps/accounts/views.py`.

**Check:**

```bash
python manage.py check
```

The route does not exist yet, so Swagger comes in Step 11.

---

## Step 11. The URL, then try it

**File:** `apps/uploads/urls.py` (new file):

```python
from django.urls import path

from .views import UploadFilesView

urlpatterns = [
    path("upload-files", UploadFilesView.as_view(), name="upload-files"),
]
```

**File:** `config/urls.py` (whole file):

```python
from django.conf import settings
from django.conf.urls.static import static
from django.contrib import admin
from django.urls import include, path
from drf_spectacular.views import SpectacularAPIView, SpectacularSwaggerView

urlpatterns = [
    path("admin/", admin.site.urls),
    path("api/v1/", include("apps.accounts.urls")),
    path("api/schema/", SpectacularAPIView.as_view(), name="schema"),
    path("api/docs/", SpectacularSwaggerView.as_view(url_name="schema"), name="docs"),
    path("api/v1/", include("apps.salons.urls")),
    path("api/v1/", include("apps.uploads.urls")),
]

# Local dev only: serve uploaded files from MEDIA_ROOT. static() returns
# nothing when DEBUG is False, so production is not affected.
urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)
```

**What each part does:**

- `"upload-files"` with no trailing slash: same style as the other routes (`auth/login`, `booking`).
- `include("apps.uploads.urls")`: the app keeps its own URLs, like accounts and salons.
- `static(...)`: lets your browser open `http://127.0.0.1:8000/media/...` while developing. In production `DEBUG` is `False`, so it adds nothing.

**Check 1: start the server.**

```bash
python manage.py runserver
```

**Check 2: get a token** (in a second terminal). Use a verified account you already have:

```bash
python manage.py shell -c "from apps.accounts.models import ConsumerAccount as A; from apps.accounts.services import tokens_for; u = A.objects.filter(account_verified=True).first(); print(tokens_for(u)['access'] if u else 'no verified account')"
```

If it prints `no verified account`, make one (local database only):

```bash
python manage.py shell -c "from django.utils import timezone; from apps.accounts.models import ConsumerAccount as A; from apps.accounts.services import tokens_for; u = A.objects.create(phone='+8801700000001', account_verified=True, phone_verified_at=timezone.now()); print(tokens_for(u)['access'])"
```

Then:

```bash
export TOKEN=<paste the token>
```

**Check 3: upload two files.** Use any photo and PDF on your Mac:

```bash
curl -s -X POST http://127.0.0.1:8000/api/v1/upload-files \
  -H "Authorization: Bearer $TOKEN" \
  -F "files=@$HOME/Desktop/photo.jpg" \
  -F "files=@$HOME/Desktop/menu.pdf" | python -m json.tool
```

Do not set `Content-Type` yourself. `curl -F` sets `multipart/form-data` with the right boundary.

Expect a list of two items, `IMAGE` then `PDF`. Open one `file_url` in your browser. `ls media/uploads/` shows a year folder.

**Check 4: the errors.**

```bash
echo "hello" > /tmp/notes.txt
curl -s -X POST http://127.0.0.1:8000/api/v1/upload-files -H "Authorization: Bearer $TOKEN" -F "files=@/tmp/notes.txt" | python -m json.tool
curl -s -o /dev/null -w "%{http_code}\n" -X POST http://127.0.0.1:8000/api/v1/upload-files -F "files=@/tmp/notes.txt"
```

The first gives `422` with `field: "files"` and `code: "unsupported_file_type"`. The second prints `401`.

**Check 5: Swagger.** Open `http://127.0.0.1:8000/api/docs/`, click Authorize, paste the token. Open `POST /api/v1/upload-files`, click "Try it out", then "Add item" once per file. Each item should be a file picker.

**Check 6: the schema is valid.**

```bash
python manage.py spectacular --validate --file /tmp/schema.yml
grep -n "upload-files" -A12 /tmp/schema.yml
```

No new warning should mention uploads. The grep shows `multipart/form-data` with `format: binary`.

---

## Step 12. Tests

**File:** `apps/uploads/tests.py` (whole file):

```python
"""Tests for POST /api/v1/upload-files.

No test here talks to S3. The class swaps the file store for Django's
in-memory one, so each file lives in RAM for one test and then vanishes.
"""

from unittest import mock

from botocore.exceptions import EndpointConnectionError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import ConsumerAccount
from apps.accounts.services import tokens_for
from apps.uploads.models import UploadedFile
from apps.uploads.serializers import MAX_FILES

URL = "/api/v1/upload-files"

# The first bytes are what matter. The rest is padding.
PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 64
PDF = b"%PDF-1.4\n" + b"\0" * 64
TEXT = b"just some words, no magic bytes"

IN_MEMORY = {
    "default": {"BACKEND": "django.core.files.storage.InMemoryStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
}


@override_settings(STORAGES=IN_MEMORY)
class UploadFilesTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user = self.make_account(verified=True)
        self.login(self.user)

    def make_account(self, verified):
        return ConsumerAccount.objects.create(
            phone="+8801711111111" if verified else "+8801722222222",
            account_verified=verified,
            phone_verified_at=timezone.now() if verified else None,
        )

    def login(self, account):
        token = tokens_for(account)["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")

    def upload(self, *files):
        return self.client.post(URL, {"files": list(files)}, format="multipart")

    def assertRefused(self, resp, code):
        self.assertEqual(resp.status_code, 422, resp.data)
        self.assertEqual(resp.data["errors"][0]["field"], "files")
        self.assertEqual(resp.data["errors"][0]["code"], code)
        self.assertFalse(UploadedFile.objects.exists())

    # --- success ---------------------------------------------------------
    def test_one_file_comes_back_as_a_list(self):
        resp = self.upload(SimpleUploadedFile("cut.png", PNG))

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(len(resp.data), 1)
        item = resp.data[0]
        self.assertEqual(set(item), {"id", "file_url", "file_type"})
        self.assertEqual(item["file_type"], "IMAGE")
        self.assertTrue(item["file_url"].startswith("http://testserver/media/uploads/"))
        self.assertTrue(item["file_url"].endswith(f"{item['id']}.png"))

    def test_many_files_keep_their_order(self):
        resp = self.upload(
            SimpleUploadedFile("a.png", PNG),
            SimpleUploadedFile("b.pdf", PDF),
        )

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual([f["file_type"] for f in resp.data], ["IMAGE", "PDF"])
        self.assertEqual(UploadedFile.objects.filter(uploaded_by=self.user).count(), 2)

    def test_type_comes_from_the_bytes_not_the_name(self):
        resp = self.upload(SimpleUploadedFile("photo.jpg", PDF, content_type="image/jpeg"))

        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data[0]["file_type"], "PDF")
        self.assertTrue(resp.data[0]["file_url"].endswith(".pdf"))
        row = UploadedFile.objects.get()
        self.assertEqual(row.mime_type, "application/pdf")
        self.assertEqual(row.original_name, "photo.jpg")

    # --- refused ---------------------------------------------------------
    def test_unknown_type_is_refused_and_nothing_is_saved(self):
        resp = self.upload(
            SimpleUploadedFile("a.png", PNG),
            SimpleUploadedFile("notes.txt", TEXT),
        )
        self.assertRefused(resp, "unsupported_file_type")

    def test_empty_file_is_refused(self):
        self.assertRefused(self.upload(SimpleUploadedFile("a.png", b"")), "empty_file")

    @mock.patch("apps.uploads.serializers.MAX_FILE_BYTES", 10)
    def test_big_file_is_refused(self):
        self.assertRefused(self.upload(SimpleUploadedFile("a.png", PNG)), "file_too_large")

    def test_no_files_is_refused(self):
        self.assertRefused(self.client.post(URL, {}, format="multipart"), "required")

    def test_too_many_files_is_refused(self):
        files = [SimpleUploadedFile(f"{i}.png", PNG) for i in range(MAX_FILES + 1)]
        self.assertRefused(self.upload(*files), "max_length")

    def test_json_body_is_refused(self):
        resp = self.client.post(URL, {"files": []}, format="json")
        self.assertEqual(resp.status_code, 415)

    # --- who may upload --------------------------------------------------
    def test_login_required(self):
        self.client.credentials()
        resp = self.upload(SimpleUploadedFile("a.png", PNG))
        self.assertEqual(resp.status_code, 401)

    def test_unverified_account_is_refused(self):
        self.login(self.make_account(verified=False))
        resp = self.upload(SimpleUploadedFile("a.png", PNG))
        self.assertEqual(resp.status_code, 403)

    # --- storage down ----------------------------------------------------
    def test_storage_failure_is_a_503_and_nothing_is_saved(self):
        down = EndpointConnectionError(endpoint_url="https://s3.example")
        with (
            mock.patch("django.core.files.storage.InMemoryStorage.save", side_effect=down),
            self.assertLogs("apps.uploads.services", level="ERROR"),
        ):
            resp = self.upload(SimpleUploadedFile("a.png", PNG))

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(resp.data["code"], "service_unavailable")
        self.assertEqual(resp.data["errors"][0]["code"], "storage_unavailable")
        self.assertFalse(UploadedFile.objects.exists())
```

**What each part does:**

- `IN_MEMORY`: Django's own in-memory file store. `override_settings(STORAGES=IN_MEMORY)` on the class swaps it in for every test in the class. Django resets `default_storage` when this setting changes, so the model's `FileField` uses it too.
- Why this is enough to keep S3 out: CI has no AWS values, so the default is the disk (Step 2). The override moves it to RAM. boto3 is never even asked. No file is left in `media/` either.
- `staticfiles` is in the dict too, because `override_settings` replaces the whole `STORAGES` dict.
- `make_account` and `login`: the same pattern as `apps/accounts/tests/test_user_lookup.py`. A real account row, a real JWT, the real URL conf.
- `PNG`, `PDF`, `TEXT`: tiny fake files. Only the first bytes matter to `filetype`.
- `assertRefused`: every 422 must name the field `files`, carry our code, and save nothing.
- `mock.patch("apps.uploads.serializers.MAX_FILE_BYTES", 10)`: makes the limit 10 bytes for one test, so we do not build a 10 MB file.
- `mock.patch("...InMemoryStorage.save", side_effect=down)`: while this test runs, saving a file raises the same error boto3 raises when S3 cannot be reached. Same idea as the booking tests patching `urlopen`.
- `assertLogs(...)`: checks we logged the error, and keeps the traceback out of the test output.

**Commands:**

```bash
python manage.py test apps.uploads -v 2
python manage.py test --shuffle
python manage.py makemigrations --check --dry-run
```

**Check:** the first runs 12 tests, all OK. The second runs the whole suite, like CI (`--shuffle` catches tests that depend on order). The third prints `No changes detected`.

**Learn by breaking it (then undo):**

- In `serializers.py`, remove `allow_empty_file=True`. Run the tests. `test_empty_file_is_refused` fails with `field: 0`. That is the audit gotcha, live.
- In `views.py`, change `IsVerified` to `IsAuthenticated`. `test_unverified_account_is_refused` fails.

---

## Step 13. Docker compose

**File:** `docker-compose.yml`. In the `api` service, **add** under `environment:` (after the `WHATSAPP_...` lines):

```yaml
      # File uploads (apps/uploads). Same names as gostyle-platform.
      AWS_BUCKET_NAME: ${AWS_BUCKET_NAME}
      AWS_REGION: ${AWS_REGION}
      AWS_ACCESS_KEY_ID: ${AWS_ACCESS_KEY_ID}
      AWS_SECRET_ACCESS_KEY: ${AWS_SECRET_ACCESS_KEY}
```

**Why:**

- This compose file lists every variable by hand. A variable in the server's `.env` does not reach the container unless it is listed here.
- `${...}`: `deploy.sh` loads the server's `.env`, and `docker stack deploy` fills these in.
- Only the `api` service. The `grpc` service never uploads, so it does not get the keys.

**Check:** `git diff docker-compose.yml` shows only these 5 lines. The indent matches the lines above (6 spaces).

**Breaks without it:** production has no bucket, and every upload answers 503 (Step 3).

---

## Step 14. Try real S3 from your laptop (before any deploy)

**1. Find the bucket's real region** (D4). This is a public request and needs no key:

```bash
curl -sI https://entity-blob-storage.s3.amazonaws.com | grep -i x-amz-bucket-region
```

It prints something like `x-amz-bucket-region: eu-north-1`. That value is `AWS_REGION`.

**2. Put the 4 values in your local `.env`.** A dev bucket is best. The platform's own local `.env` uses a different bucket from production, so ask which one you may use. Then restart `runserver`. The Step 2 check now prints `S3Storage`.

**3. Upload** with the curl from Step 11. The `file_url` now starts with `https://<bucket>.s3.<region>.amazonaws.com/customers/uploads/`.

**4. Is it public?**

```bash
curl -sI "<paste file_url>" | head -1
curl -sI "<paste file_url>" | grep -i content-type
```

- `HTTP/1.1 200 OK`: public. The content type must be the real one, for example `image/jpeg`.
- `HTTP/1.1 403 Forbidden`: the upload worked, but the bucket policy does not give public read on `customers/`. Ask whoever owns the AWS account to add this statement to the bucket policy:

```json
{
  "Sid": "PublicReadCustomerUploads",
  "Effect": "Allow",
  "Principal": "*",
  "Action": "s3:GetObject",
  "Resource": "arn:aws:s3:::entity-blob-storage/customers/*"
}
```

If they make a new IAM user for us (D3), this is all it needs:

```json
{
  "Effect": "Allow",
  "Action": "s3:PutObject",
  "Resource": "arn:aws:s3:::entity-blob-storage/customers/*"
}
```

`PutObject` is enough, because our names are unique and django-storages then never checks if a file exists.

**5. Clean up.** Remove the AWS lines from your local `.env` if you want local disk again.

---

## Step 15. Server and deploy (you run these, one at a time)

Order matters. The variables must be on the server before the new code starts.

**1. Server `.env`.** Add the 4 values to `$DEPLOY_DIR/.env` on the manager node (the file `deploy.sh` reads). Use the region from Step 14. If you reuse the platform's keys, they are in `/opt/gostyle/.env.prod`.

**2. nginx.** In `nginx/api.gostyle.uk.conf`, change the limit and add a comment:

```nginx
    # POST /api/v1/upload-files: 5 files x 10 MB, plus multipart headers.
    # Keep in step with MAX_FILES and MAX_FILE_MB in apps/uploads/serializers.py.
    client_max_body_size 55m;
```

Then install it on the server the way you did the first time (`scripts/setup-nginx.sh`), or edit `/etc/nginx/sites-available/api.gostyle.uk` and run:

```bash
sudo nginx -t && sudo systemctl reload nginx
```

Why 55m: nginx refuses a bigger body with an HTML 413 page before Django sees it. nginx reads the whole body before passing it to gunicorn (`proxy_request_buffering` is on by default), so a slow phone does not hold a gunicorn worker. The 60 s gunicorn timeout still covers the upload to S3.

Option: to keep the other endpoints at 10m, put `55m` in its own `location = /api/v1/upload-files` block instead. That block must repeat all the `proxy_*` lines from `location /`, as the comment near the admin block explains.

**3. Deploy.** Merge to `main`. CI runs the tests, builds the image, and `deploy.sh` runs `migrate` (migration 0002) before the new tasks start.

**4. Live checks, one at a time:**

```bash
curl -s -o /dev/null -w "%{http_code}\n" -X POST https://api.gostyle.uk/api/v1/upload-files
```

Expect `401`. Then one real upload with a real token, then `curl -sI` on the `file_url` it returns (expect `200`).

---

## Later (not in this build)

- **Photo location data.** Phone photos carry EXIF, which can include the GPS spot where the photo was taken, and the URL is public. The platform strips EXIF for storefront images (sharp). Here that would be Pillow, plus `pillow-heif` for iPhone HEIC. Do it before profile or review photos go live.
- **Rate limit.** A daily cap per account, like `LOOKUP_PER_ACCOUNT_DAILY` in `apps/accounts/ratelimit.py`.
- **Orphans.** Objects left after a 503, and rows never linked to anything. A cleanup command.
- **Delete.** A delete endpoint, and deleting a customer's files when their account is deleted.
- **Links.** Connect a file to what it belongs to (review, booking note, profile image).
- **App docs.** `docs/UPLOAD_FILES_API.md` for the app team, like the other `docs/*_API.md`.

## Files touched when done

| File | Change |
|---|---|
| `requirements.txt` | 9 new pins |
| `config/settings/base.py` | AWS variables, `S3_STORAGE`, switch |
| `config/settings/production.py` | always S3 |
| `.env.example`, `.gitignore`, `.dockerignore` | variable names, `media/` |
| `apps/uploads/models.py` + `migrations/0002_*.py` | `FileType`, `mime_type`, `size` |
| `apps/uploads/filetypes.py` | new |
| `apps/uploads/serializers.py` | new |
| `apps/uploads/services.py` | new |
| `apps/uploads/views.py` | the view |
| `apps/uploads/urls.py` | new |
| `config/urls.py` | include, local media |
| `apps/uploads/tests.py` | 12 tests |
| `docker-compose.yml` | 4 variables |
| `nginx/api.gostyle.uk.conf` | `55m` |
