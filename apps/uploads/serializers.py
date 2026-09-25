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




class UploadedFileSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = UploadedFile
        fields = ["id", "file_url", "file_type"]

    def get_file_url(self, obj) -> str:
        # S3 already gives a full https URL, and this returns it unchanged.
        # Local disk gives "/media/...", and this adds the host in front.
        return self.context["request"].build_absolute_uri(obj.file.url)