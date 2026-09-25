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