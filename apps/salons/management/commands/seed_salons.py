from django.core.management.base import BaseCommand
from apps.salons.models import Salon

SEEDS = [
    dict(id="10293", name="Velvet & Co", category="ladies", rating=4.1,
         reviews=274, open_now=True, hours="11:00 AM - 9:00 PM",
         logo="https://res.cloudinary.com/dkxy98g6p/image/upload/v1783000798/marker-45_wzoveb.png",
         latitude=29.3305, longitude=47.9997),
    dict(id="10294", name="Gents Lounge", category="gents", rating=4.8,
         reviews=150, open_now=True, hours="10:00 AM - 10:00 PM",
         logo="https://res.cloudinary.com/dkxy98g6p/image/upload/v1783000798/gents-barber.png",
         latitude=29.3500, longitude=48.0100),
    dict(id="10295", name="Royal Spa & Salon", category="unisex", rating=4.5,
         reviews=88, open_now=False, hours="09:00 AM - 08:00 PM",
         logo="https://res.cloudinary.com/dkxy98g6p/image/upload/v1783000798/royal-spa.png",
         latitude=29.3100, longitude=47.9800),
]

class Command(BaseCommand):
    def handle(self, *args, **options):
        for s in SEEDS:
            Salon.objects.update_or_create(id=s["id"], defaults=s)
        self.stdout.write(self.style.SUCCESS(f"Seeded {len(SEEDS)} salons"))