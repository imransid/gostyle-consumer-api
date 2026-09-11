from django.db import models


class Salon(models.Model):
    class Category(models.TextChoices):
        GENTS = "gents"
        LADIES = "ladies"
        UNISEX = "unisex"

    id = models.SlugField(primary_key=True, max_length=64)  # "velvet-co"
    name = models.CharField(max_length=120)
    category = models.CharField(max_length=10, choices=Category.choices)
    rating = models.DecimalField(max_digits=2, decimal_places=1)
    reviews = models.PositiveIntegerField(default=0)
    open_now = models.BooleanField(default=True)   # "open" clashes with Python builtins style
    hours = models.CharField(max_length=64)
    logo = models.URLField()
    latitude = models.FloatField()
    longitude = models.FloatField()

    def __str__(self):
        return self.name




        