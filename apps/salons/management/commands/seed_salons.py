from django.core.management.base import BaseCommand
from apps.salons.models import Salon

SEEDS = [
    dict(id="velvet-co", name="Velvet & Co", category="ladies", rating=4.7,
         reviews=274, open_now=True, hours="11:00 AM - 9:00 PM",
         logo="https://res.cloudinary.com/dkxy98g6p/image/upload/v1783000798/marker-45_wzoveb.png",
         latitude=23.7185, longitude=90.4200),
    # ... rest of your salons with REAL coordinates
]

class Command(BaseCommand):
    def handle(self, *args, **options):
        for s in SEEDS:
            Salon.objects.update_or_create(id=s["id"], defaults=s)
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(SEEDS)} salons"))