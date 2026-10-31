from drf_spectacular.utils import extend_schema_field
from rest_framework import serializers

from .models import Category, Notification

CATEGORY_CHOICES = ["all", *Category.values]


class TargetSerializer(serializers.Serializer):
    type = serializers.CharField()
    id = serializers.CharField()


class NotificationSerializer(serializers.ModelSerializer):
    is_read = serializers.SerializerMethodField()
    target = serializers.SerializerMethodField()
    data = serializers.DictField(child=serializers.CharField())

    class Meta:
        model = Notification
        fields = [
            "id",
            "category",
            "type",
            "title",
            "body",
            "is_read",
            "read_at",
            "created_at",
            "occurred_at",
            "target",
            "data",
        ]

    def get_is_read(self, obj) -> bool:
        return obj.read_at is not None

    @extend_schema_field(TargetSerializer(allow_null=True))
    def get_target(self, obj):
        if not obj.target_type:
            return None
        return {"type": obj.target_type, "id": obj.target_id}


class ListParamsSerializer(serializers.Serializer):
    """GET /notifications query. page_size and cursor are read by the
    paginator; page_size is checked here so a bad one is a 422, not a
    silent default."""

    category = serializers.ChoiceField(choices=CATEGORY_CHOICES, required=False, default="all")
    unread = serializers.BooleanField(required=False, allow_null=True, default=None)
    page_size = serializers.IntegerField(required=False, min_value=1, max_value=50)


class ReadAllSerializer(serializers.Serializer):
    category = serializers.ChoiceField(choices=CATEGORY_CHOICES, required=False, default="all")
    # Only items created at or before this: the newest one the app has
    # shown. One that arrives while the customer taps stays unread.
    before = serializers.DateTimeField(required=False, allow_null=True, default=None)


class UnreadCountSerializer(serializers.Serializer):
    total = serializers.IntegerField()
    orders = serializers.IntegerField()
    updates = serializers.IntegerField()
    payments = serializers.IntegerField()
    promotions = serializers.IntegerField()


class ReadAllResultSerializer(serializers.Serializer):
    updated = serializers.IntegerField()
    unread = UnreadCountSerializer()
