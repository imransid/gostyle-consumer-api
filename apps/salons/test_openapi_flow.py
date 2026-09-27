from django.test import SimpleTestCase
from drf_spectacular.generators import SchemaGenerator

from config.openapi_flow import FLOW, NOT_PLACED, order_by_flow


class OrderByFlowTests(SimpleTestCase):
    """config/openapi_flow.py: Swagger in the order the app uses it."""

    def test_each_endpoint_goes_to_its_section_with_a_step_title(self):
        schema = {"paths": {
            "/api/v1/auth/login": {"post": {"tags": ["v1"], "summary": ""}},
            "/api/v1/auth/register": {"post": {"tags": ["v1"]}},
        }}
        out = order_by_flow(schema)
        self.assertEqual(list(out["paths"]), ["/api/v1/auth/register", "/api/v1/auth/login"])
        self.assertEqual(
            out["paths"]["/api/v1/auth/register"]["post"]["summary"], "Step 1: Create an account"
        )
        self.assertEqual(out["paths"]["/api/v1/auth/login"]["post"]["tags"], ["1. Sign up and log in"])
        self.assertEqual(out["tags"][0]["name"], "1. Sign up and log in")

    def test_an_endpoint_not_in_the_flow_is_kept_in_a_last_section(self):
        schema = {"paths": {"/api/v1/new": {"get": {"tags": ["v1"]}, "parameters": [{"name": "x"}]}}}
        out = order_by_flow(schema)
        self.assertEqual(out["paths"]["/api/v1/new"]["get"]["tags"], [NOT_PLACED])
        self.assertEqual(out["paths"]["/api/v1/new"]["parameters"], [{"name": "x"}])
        self.assertEqual(out["tags"][-1]["name"], NOT_PLACED)

    def test_the_real_schema_has_every_endpoint_in_the_flow(self):
        schema = SchemaGenerator().get_schema(request=None, public=True)
        tagged = {
            (method, path): op.get("tags")
            for path, item in schema["paths"].items()
            for method, op in item.items()
            if isinstance(op, dict)
        }
        missing = [f"{m.upper()} {p}" for (m, p), t in tagged.items() if t == [NOT_PLACED]]
        self.assertEqual(missing, [], "Place these in FLOW, in config/openapi_flow.py")
        unknown = [
            f"{m} {p}" for _, _, steps in FLOW for m, p, _ in steps if (m.lower(), p) not in tagged
        ]
        self.assertEqual(unknown, [], "FLOW names endpoints the API does not have")
        self.assertEqual([t["name"] for t in schema["tags"]], [s for s, _, _ in FLOW])
