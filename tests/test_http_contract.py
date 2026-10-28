import json
from pathlib import Path
import unittest

from tests.http_contract import snapshot


def assert_schema_compatible(expected, actual, path="schema"):
    """Allow additive optional fields, while preserving existing validation/defaults."""
    if isinstance(expected, dict):
        if not isinstance(actual, dict):
            raise AssertionError(f"{path}: schema type changed")
        if "const" in expected and "enum" in actual and expected["const"] in actual["enum"]:
            actual = {k: v for k, v in actual.items() if k != "enum"}
            actual["const"] = expected["const"]
        # Only properties/definitions can grow without changing old requests.
        new_keys = set(actual) - set(expected) - {"properties", "$defs", "required"}
        if new_keys:
            raise AssertionError(f"{path}: new validation keywords {sorted(new_keys)}")
        old_required, new_required = set(expected.get("required", [])), set(actual.get("required", []))
        if old_required != new_required:
            raise AssertionError(f"{path}: required fields changed: {old_required} -> {new_required}")
        for key, value in expected.items():
            if key == "required":
                continue
            if key == "enum" and set(value).issubset(actual.get("enum", [])):
                continue
            if key not in actual:
                raise AssertionError(f"{path}.{key}: removed")
            if key in {"properties", "$defs"}:
                for name, child in value.items():
                    if name not in actual[key]:
                        raise AssertionError(f"{path}.{key}.{name}: removed")
                    assert_schema_compatible(child, actual[key][name], f"{path}.{key}.{name}")
            else:
                assert_schema_compatible(value, actual[key], f"{path}.{key}")
    elif expected != actual:
        raise AssertionError(f"{path}: changed from {expected!r} to {actual!r}")


class HttpContractTests(unittest.TestCase):
    def test_routes_and_request_schemas_remain_compatible(self):
        expected = json.loads((Path(__file__).parent / "contracts" / "http_routes.json").read_text(encoding="utf-8"))
        actual = snapshot()
        for application, routes in expected.items():
            for route, contract in routes.items():
                with self.subTest(application=application, route=route):
                    self.assertIn(route, actual[application])
                    current = actual[application][route]
                    self.assertEqual(current["status_code"], contract["status_code"])
                    for location, parameters in contract["parameters"].items():
                        current_fields = {p["name"]: p for p in current["parameters"][location]}
                        for parameter in parameters:
                            self.assertIn(parameter["name"], current_fields)
                            field = current_fields.pop(parameter["name"])
                            self.assertEqual(parameter["required"], field["required"])
                            self.assertEqual(parameter.get("default"), field.get("default"))
                            assert_schema_compatible(parameter["schema"], field["schema"], route + "." + parameter["name"])
                        self.assertTrue(all(not p["required"] for p in current_fields.values()), "New required request parameter")

    def test_contract_gate_accepts_only_optional_additions(self):
        old = {"type": "object", "properties": {"prompt": {"type": "string", "maxLength": 20}}, "required": ["prompt"]}
        compatible = {**old, "properties": {**old["properties"], "extra": {"type": "string", "default": ""}}}
        assert_schema_compatible(old, compatible)
        with self.assertRaisesRegex(AssertionError, "required fields changed"):
            assert_schema_compatible(old, {**compatible, "required": ["prompt", "extra"]})
        with self.assertRaisesRegex(AssertionError, "changed"):
            assert_schema_compatible(old, {**old, "properties": {"prompt": {"type": "string", "maxLength": 5}}})
        with self.assertRaisesRegex(AssertionError, "removed"):
            assert_schema_compatible(old, {**old, "properties": {}})

    def test_enum_widening_preserves_all_old_inputs_but_narrowing_fails(self):
        assert_schema_compatible({"type": "string", "const": "h3"}, {"type": "string", "enum": ["h3", "online-2.0"]})
        assert_schema_compatible({"enum": ["mock", "codex"]}, {"enum": ["mock", "codex", "api"]})
        with self.assertRaises(AssertionError):
            assert_schema_compatible({"enum": ["mock", "codex"]}, {"enum": ["mock", "api"]})
        with self.assertRaises(AssertionError):
            assert_schema_compatible({"const": "h3"}, {"enum": ["online-2.0"]})
