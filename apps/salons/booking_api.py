import os
import requests

BASE_URL = os.environ.get("BOOKING_API_URL", "")
TOKEN = os.environ.get("BOOKING_API_TOKEN", "")


def get_branch_services(tenant_id, branch_id):
    """Call booking-api. Returns a list, or None if it failed."""
    try:
        response = requests.get(
            f"{BASE_URL}/v1/services-directory/services",
            params={"tenantId": tenant_id, "branchId": branch_id},
            timeout=5,
        )
        response.raise_for_status()
        return response.json()
    except requests.RequestException:
        return None