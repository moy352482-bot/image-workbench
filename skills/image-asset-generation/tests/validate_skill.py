"""Validate this Skill's local contracts and synthetic fixtures, without generation.

No application imports, network retrieval, subprocesses, real image reads, or credits.
Only --output writes a file. Passing fixtures does not establish NLP or image quality.
"""

from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import sys
from urllib.parse import urldefrag, urljoin

from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError
from referencing import Registry, Resource
from referencing.exceptions import NoSuchResource, Unresolvable


ROOT = Path(__file__).resolve().parents[1]
SCHEMAS = {
    "asset-spec", "asset-version", "character", "common", "generation-plan",
    "product", "quality-check", "skill-input", "skill-output", "style-index", "style",
}
LAYERS = (
    "asset_identity", "subject_description", "scene", "action_state", "composition",
    "camera", "lighting", "material_texture", "style", "reference_constraints",
    "consistency_constraints", "negative_constraints",
)
STYLE_IDS = {
    "cinematic-realism", "commercial-product", "editorial-fashion", "documentary",
    "anime-manga", "illustration", "watercolor", "3d-stylized", "retro-film",
    "futuristic-sci-fi", "minimal", "graphic-poster", "lifestyle", "architectural",
    "food-photography",
}
ASSET_TYPES = {
    "character", "product", "location", "environment", "prop", "vehicle", "wardrobe",
    "style_reference", "key_visual", "logo", "food", "architecture",
}
RESERVED_VARIABLES = {"asset_name", "asset_type", "scene_summary"}
PLACEHOLDER = re.compile(r"\{\{\s*([a-z][a-z0-9_]*)\s*\}\}")
RATIOS = {"auto", "1:1", "3:2", "2:3"}
CHECKS = {
    "file_exists", "readable", "dimensions", "format", "generation_succeeded",
    "content_integrity", "artifact_registered", "version_registered",
}
BINDING_ROLES = {
    "primary_reference": "primary_identity", "face_reference": "face",
    "full_body_reference": "full_body", "clothing_reference": "clothing",
}
MANDATORY_BLOCKERS = {"HOST_ADAPTER_REQUIRED", "COST_UNQUOTED", "MODEL_UNRESOLVED"}


class InvalidContract(ValueError):
    """An expected data/schema/semantic validation failure, not a test crash."""


def require(condition, message):
    if not condition:
        raise InvalidContract(message)


def read_json(path):
    def unique_pairs(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"duplicate JSON key: {key}")
            result[key] = value
        return result

    return json.loads(path.read_text(encoding="utf-8-sig"), object_pairs_hook=unique_pairs)


def deny_retrieval(uri):
    # All resources must have been loaded explicitly below. Never fetch a URL.
    raise NoSuchResource(ref=uri)


def walk(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def unique(values, label):
    require(len(values) == len(set(values)), f"{label}: duplicate values")


def version_number(value):
    return int(value[1:])


def pointer(document, path):
    current = document
    for key in path[1:].split("/"):
        key = key.replace("~1", "/").replace("~0", "~")
        require(isinstance(current, dict) and key in current, f"unresolved fixed path: {path}")
        current = current[key]
    return current


def assemble_prompt(layers):
    return "\n\n".join(f"[{index:02d} {item['layer']}]\n{item['text']}"
                       for index, item in enumerate(layers, 1))


def sha256_json(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def report_destination(path, root=ROOT):
    target = Path(path).expanduser().resolve()
    skill_root = root.resolve()
    report_root = (skill_root / "tests" / "reports").resolve()
    require(not target.is_relative_to(skill_root) or target.is_relative_to(report_root),
            "reports inside this Skill must be under tests/reports; input resources are protected")
    require(target.suffix.lower() == ".json", "report output must be a .json file")
    return target


def check_output_policy(root):
    # Path validation only: none of these synthetic destinations is written.
    accepted = [root / "tests" / "reports" / "synthetic-check.json", root.parent / "synthetic-external-report.json"]
    for path in accepted:
        report_destination(path, root)
    rejected = [root / "examples" / "asset-generation-examples.json", root / "schemas" / "asset-spec.schema.json",
                root / "styles" / "index.json", root / "SKILL.md", root / "references" / "research.md"]
    for path in rejected:
        try:
            report_destination(path, root)
        except InvalidContract:
            continue
        raise InvalidContract(f"report output could overwrite protected resource: {path.name}")
    return {"accepted": len(accepted), "rejected": len(rejected), "writes": 0}


class Contracts:
    def __init__(self, root):
        self.root = root
        self.schemas = {path.name: read_json(path) for path in sorted((root / "schemas").glob("*.json"))}
        require(set(self.schemas) == {name + ".schema.json" for name in SCHEMAS},
                "expected the eleven v1 schema resources")
        ids = [schema["$id"] for schema in self.schemas.values()]
        unique(ids, "schema $id")
        self.registry = Registry(retrieve=deny_retrieval).with_resources(
            (schema["$id"], Resource.from_contents(schema)) for schema in self.schemas.values())
        for name, schema in self.schemas.items():
            Draft202012Validator.check_schema(schema)
            require(schema.get("$schema") == "https://json-schema.org/draft/2020-12/schema",
                    f"{name}: wrong dialect")
            for node in walk(schema):
                if "$ref" in node:
                    absolute = urljoin(schema["$id"], node["$ref"])
                    require(urldefrag(absolute).url in ids, f"{name}: nonlocal $ref: {absolute}")
                    try:
                        self.registry.resolver(base_uri=schema["$id"]).lookup(node["$ref"])
                    except Unresolvable as exc:
                        raise InvalidContract(f"{name}: unresolved local $ref: {node['$ref']}") from exc
        self.styles = {}

    def validate(self, name, value):
        schema = self.schemas[name + ".schema.json"]
        validator = Draft202012Validator(schema, registry=self.registry, format_checker=FormatChecker())
        errors = list(validator.iter_errors(value))
        if errors:
            error = errors[0]
            location = "/".join(str(part) for part in error.absolute_path) or "<root>"
            raise InvalidContract(f"{name}/{location}: {error.message[:240]}")

    def check_variables(self, style, values, label):
        definitions = style["variables"]
        require(set(values) == set(definitions), f"{label}: missing or unknown style variables")
        for name, value in values.items():
            require(isinstance(value, str) and bool(value.strip()), f"{label}/{name}: non-text/empty value")
            require("{{" not in value and "}}" not in value, f"{label}/{name}: nested placeholder")
            if definitions[name]["type"] == "enum":
                require(value in definitions[name]["choices"], f"{label}/{name}: value outside choices")

    def render_style(self, style, values, *, asset_name, asset_type, scene_summary):
        self.check_variables(style, values, style["style_id"])
        environment = {**values, "asset_name": asset_name, "asset_type": asset_type,
                       "scene_summary": scene_summary}
        rendered = PLACEHOLDER.sub(lambda match: environment[match.group(1)], style["prompt_template"])
        require("{{" not in rendered and "}}" not in rendered, "unexpanded style placeholder")
        return rendered

    def check_style(self, style):
        self.validate("style", style)
        variables = set(style["variables"])
        require(not variables.intersection(RESERVED_VARIABLES), "style variable shadows reserved name")
        placeholders = set(PLACEHOLDER.findall(style["prompt_template"]))
        require(placeholders <= variables | RESERVED_VARIABLES, "unknown style template placeholder")
        require(variables <= placeholders, "unused declared style variable")
        residue = PLACEHOLDER.sub("", style["prompt_template"])
        require("{{" not in residue and "}}" not in residue, "invalid template expression")
        self.check_variables(style, {name: definition["default"] for name, definition in
                                    style["variables"].items()}, "style defaults")
        anchors = style["style_fidelity_anchors"]
        unique([anchor["id"] for anchor in anchors], "style anchor IDs")
        require(any(anchor["importance"] == "core" for anchor in anchors), "no core style anchors")
        unique([example["example_id"] for example in style["examples"]], "style example IDs")
        for example in style["examples"]:
            require(example["asset_type"] in style["asset_types"], "example type outside style applicability")
            self.render_style(style, example["variables"], asset_name=example["brief"],
                              asset_type=example["asset_type"], scene_summary=example["brief"])

    def load_styles(self):
        index = read_json(self.root / "styles" / "index.json")
        self.validate("style-index", index)
        ids = [entry["style_id"] for entry in index["styles"]]
        unique(ids, "style index IDs")
        require(set(ids) == STYLE_IDS, "index must contain the specified fifteen styles")
        paths = []
        for entry in index["styles"]:
            require(entry["path"] == entry["style_id"] + "/style.json", "style index path/id mismatch")
            path = (self.root / "styles" / entry["path"]).resolve()
            require(path.is_relative_to((self.root / "styles").resolve()), "style path escapes directory")
            paths.append(path)
            style = read_json(path)
            require(style["style_id"] == entry["style_id"], "style file/index ID mismatch")
            self.check_style(style)
            self.styles[style["style_id"]] = style
        require(set(paths) == {path.resolve() for path in (self.root / "styles").glob("*/style.json")},
                "style files missing from index or indexed files absent")
        return sum(len(style["examples"]) for style in self.styles.values())

    def check_references(self, references):
        unique([ref["reference_id"] for ref in references], "reference IDs")
        for ref in references:
            verification = ref["verification"]
            if ref["source"]["kind"] == "fixture":
                require(set(verification.values()) == {"fixture_only"}, "fixture falsely claims verification")
            else:
                require("fixture_only" not in verification.values(), "nonfixture masquerades as fixture")
        return {ref["reference_id"]: ref for ref in references}

    def check_asset(self, asset):
        self.validate("asset-spec", asset)
        if asset["asset_type"] in {"character", "product"}:
            self.validate(asset["asset_type"], asset)
        if asset["asset_type"] == "character":
            require(asset["identity"]["character_id"] == asset["asset_id"], "character identity differs from asset_id")
        for path in asset["property_policy"]["fixed_paths"]:
            pointer(asset, path)
        refs = self.check_references(asset["references"])
        for binding, role in BINDING_ROLES.items():
            rid = asset["consistency"]["reference_bindings"][binding]
            if rid is not None:
                require(rid in refs, f"{binding}: missing reference")
                require(role in refs[rid]["roles"], f"{binding}: incompatible reference role")
        canonical = asset["consistency"]["canonical_version"]
        if not refs:
            require(canonical is None, "no references but canonical identity claimed")
        if canonical:
            require(version_number(canonical) <= version_number(asset["version"]), "future canonical version")
        base = asset["generation_strategy"]["base_version"]
        if base:
            require(version_number(asset["version"]) > version_number(base), "target version does not advance base")
        style_id = asset["style"]["style_id"]
        require(style_id in self.styles, "unknown asset style")
        style = self.styles[style_id]
        require(asset["asset_type"] in style["asset_types"], "style does not support this asset type")
        self.check_variables(style, asset["style"]["variables"], "asset style variables")

    def check_case(self, case):
        require(set(case) == {"case_id", "label", "input", "expected_output"}, "unexpected case fields")
        require(isinstance(case["case_id"], str) and case["case_id"].strip(), "missing case ID")
        require(isinstance(case["label"], str) and case["label"].strip(), "missing case label")
        request, output = case["input"], case["expected_output"]
        self.validate("skill-input", request)
        self.validate("skill-output", output)
        asset = output["asset_spec"]
        self.check_asset(asset)
        for key in ("asset_id", "asset_type", "status", "version"):
            require(output[key] == asset[key], f"output/{key} differs from AssetSpec")
        if request.get("asset_type") is not None:
            require(request["asset_type"] == asset["asset_type"], "input asset type silently changed")
        require(output["selected_style"] == asset["style"]["style_id"], "selected style mismatch")
        style = self.styles[output["selected_style"]]
        require(output["style_selection"]["category"] == style["category"], "selected style category mismatch")
        if request.get("style") in self.styles:
            require(request["style"] == output["selected_style"], "explicit style silently changed")
        for alternative in output["style_selection"]["alternatives"]:
            require(alternative in self.styles and alternative != output["selected_style"], "invalid alternative style")
            require(asset["asset_type"] in self.styles[alternative]["asset_types"], "alternative style type mismatch")
        unique(output["style_selection"]["alternatives"], "alternative styles")
        require(output["references"] == asset["references"] == request.get("reference_images", []),
                "input/spec/output references differ")
        refs = self.check_references(output["references"])
        strategies = output["reference_strategy"]
        unique([strategy["reference_id"] for strategy in strategies], "reference strategy IDs")
        require({strategy["reference_id"] for strategy in strategies} == set(refs), "reference strategy coverage mismatch")
        for strategy in strategies:
            require(set(strategy["roles"]) == set(refs[strategy["reference_id"]]["roles"]), "reference strategy roles mismatch")
            unique(strategy["roles"], "reference strategy roles")
        scene = request.get("scene", {})
        require(set(scene) <= set(asset["property_policy"]["shot_overridable_fields"]), "scene overrides non-permitted property")
        existing = request.get("existing_asset")
        if existing:
            require(existing["asset_id"] == asset["asset_id"], "existing asset identity changed")
            require(asset["generation_strategy"]["base_version"] == existing["version"], "existing asset base version lost")
        layers = output["prompt_layers"]
        require(tuple(layer["layer"] for layer in layers) == LAYERS, "wrong twelve-layer order")
        require(output["prompt"] == assemble_prompt(layers), "final prompt differs from ordered layer assembly")
        require("{{" not in output["prompt"] and "}}" not in output["prompt"], "prompt retains unresolved placeholders")
        rendered = self.render_style(style, asset["style"]["variables"], asset_name=asset["asset_name"],
                                     asset_type=asset["asset_type"], scene_summary=layers[2]["text"])
        require(rendered in layers[8]["text"], "style layer omits or changes resolved template")
        require(asset["asset_id"] in layers[0]["text"] and asset["version"] in layers[0]["text"], "identity layer missing fixed asset/version")
        for rid in refs:
            require(rid in layers[9]["text"], "reference layer omits a reference ID")
        unique(output["negative_constraints"], "negative constraints")
        for text in output["negative_constraints"]:
            require(text in layers[11]["text"], "negative layer omits declared constraint")
        exact = asset["constraints"]["exact_text"]
        if exact is not None:
            require(exact in output["prompt"], "exact text absent from final prompt")
        self.check_plan(request, output, refs)

    def check_plan(self, request, output, refs):
        asset, plan = output["asset_spec"], output["generation_plan"]
        self.validate("generation-plan", plan)
        intent = request.get("generation_intent", "generate")
        require(plan["intent"] == asset["generation_strategy"]["classification"] == intent, "generation intent changed")
        for key in ("preferred_model", "fallback_model"):
            require(plan["model_route"][key] == asset[key] == request.get(key), f"{key} mismatch")
        expected_budget = {"max_model_calls": request.get("budget", {}).get("max_model_calls", 1),
                           "max_credit": request.get("budget", {}).get("max_credit")}
        require(plan["budget"] == expected_budget, "budget changed during planning")
        steps = plan["steps"]
        step_ids = [step["step_id"] for step in steps]
        unique(step_ids, "plan step IDs")
        require(plan["calls_planned"] == len(steps), "planned calls do not equal steps")
        require(len(steps) <= asset["generation_strategy"]["max_model_calls"],
                "steps exceed AssetSpec's declared plan upper bound")
        codes = [item["code"] for item in plan["blockers"]]
        unique(codes, "blocker codes")
        required_codes = set(MANDATORY_BLOCKERS)
        if len(steps) > plan["budget"]["max_model_calls"]:
            required_codes.add("BUDGET_EXCEEDED")
        parameters = plan["parameters"]
        requested, applied = parameters["requested"], parameters["applied"]
        ratio = request.get("aspect_ratio", "auto")
        resolution = request.get("resolution", {"mode": "unspecified", "width": None, "height": None})
        require(requested == {"aspect_ratio": ratio, "resolution": resolution, "quality": request.get("quality", "standard")},
                "requested parameters differ from input defaults")
        require(asset["aspect_ratio"] == ratio and asset["resolution"] == resolution, "AssetSpec parameters differ from request")
        require(applied["aspect_ratio"] == (ratio if ratio in RATIOS else None), "ratio silently substituted or dropped")
        assessments = parameters["assessments"]
        names = [item["parameter"] for item in assessments]
        unique(names, "parameter assessments")
        require(set(names) == {"operation", "aspect_ratio", "resolution", "quality", "model", "references"}, "incomplete capability assessments")
        disposition = {item["parameter"]: item["disposition"] for item in assessments}
        if ratio not in RATIOS:
            required_codes.add("UNSUPPORTED_RATIO")
            require(disposition["aspect_ratio"] == "unsupported", "unsupported ratio labeled supported")
        else:
            require(disposition["aspect_ratio"] in {"supported", "prompt_only"}, "supported ratio has inconsistent assessment")
        if resolution["mode"] == "exact":
            required_codes.add("EXACT_RESOLUTION_UNSUPPORTED")
            require(disposition["resolution"] == "unsupported", "exact resolution labeled supported")
        require(disposition["quality"] == "prompt_only", "quality falsely claims native parameter support")
        require(disposition["model"] in {"requires_adapter", "unverified"}, "unresolved model labeled supported")
        if any(ref["source"]["kind"] == "fixture" or set(ref["verification"].values()) != {"verified"} for ref in refs.values()):
            required_codes.add("REFERENCE_UNVERIFIED")
        if any(ref["rights"]["status"] in {"unknown", "restricted"} for ref in refs.values()):
            required_codes.add("REFERENCE_RIGHTS_UNRESOLVED")
        used_refs, previous = set(), set()
        for step in steps:
            for key in ("reference_ids", "references_from_steps", "depends_on"):
                unique(step[key], f"step {step['step_id']}/{key}")
            require(set(step["depends_on"]) <= previous, "dependency is self, future, or missing")
            require(set(step["references_from_steps"]) <= set(step["depends_on"]), "upstream reference lacks dependency")
            constraints = step["step_reference_constraints"]
            source_steps = [constraint["source_step"] for constraint in constraints]
            unique(source_steps, "step reference constraints")
            require(set(source_steps) == set(step["references_from_steps"]),
                    "upstream references lack matching role/preservation constraints")
            prompt_headers = re.findall(r"^\[(\d{2}) ([a-z_]+)\]$", step["step_prompt"], flags=re.MULTILINE)
            require(prompt_headers == [(f"{index:02d}", layer) for index, layer in enumerate(LAYERS, 1)],
                    "step prompt is not a complete twelve-layer instruction")
            require("{{" not in step["step_prompt"] and "}}" not in step["step_prompt"],
                    "step prompt retains unresolved placeholders")
            require(set(step["reference_ids"]) <= set(refs), "step uses unknown external reference")
            used_refs.update(step["reference_ids"])
            base = step["edit_base_reference"]
            if base is not None:
                require(base in step["reference_ids"], "edit base not among step references")
                require("edit_base" in refs[base]["roles"], "edit base reference lacks edit_base role")
                source = refs[base]["source"]
                if source["kind"] != "artifact" or source.get("artifact_kind") != "output":
                    required_codes.add("EDIT_BASE_REQUIRED")
            elif step["operation"] in {"edit", "inpaint"}:
                required_codes.add("EDIT_BASE_REQUIRED")
            if step["operation"] == "inpaint":
                required_codes.add("UNSUPPORTED_OPERATION")
                require(disposition["operation"] == "unsupported", "inpaint labeled supported")
                if not any("mask" in refs[rid]["roles"] for rid in step["reference_ids"]):
                    required_codes.add("MASK_REQUIRED")
            extras = len(step["reference_ids"]) + len(step["references_from_steps"]) - int(base is not None)
            if extras > 8:
                required_codes.add("REFERENCE_LIMIT_EXCEEDED")
            previous.add(step["step_id"])
        require(used_refs == set(refs), "declared reference silently dropped from all steps")
        if intent == "inpaint":
            require(any(step["operation"] == "inpaint" for step in steps), "inpaint silently converted to another operation")
            required_codes.add("UNSUPPORTED_OPERATION")
        if intent == "edit":
            require(any(step["operation"] == "edit" for step in steps), "edit silently converted to generation")
        require(required_codes <= set(codes), "required blockers absent: " + ", ".join(sorted(required_codes - set(codes))))
        cost = output["estimated_cost"]
        require([item["step_id"] for item in cost["items"]] == step_ids, "cost lines do not align with steps")
        for item, step in zip(cost["items"], steps):
            require(item["operation"] == step["operation"], "cost operation differs from plan")
            require(item["reference_count"] == len(step["reference_ids"]) + len(step["references_from_steps"]),
                    "cost reference count differs (edit base must be counted once)")

    def check_quality(self, quality):
        self.validate("quality-check", quality)
        names = [item["check"] for item in quality["checks"]]
        unique(names, "quality check names")
        require(set(names) == CHECKS, "quality checks are incomplete")
        statuses = {item["status"] for item in quality["checks"]}
        overall = "fail" if "fail" in statuses else ("pass" if statuses == {"pass"} else "not_run")
        require(quality["overall"] == overall, "quality overall contradicts individual checks")

    def check_version(self, version):
        self.validate("asset-version", version)
        for key in ("artifact_ids", "task_ids"):
            unique(version[key], "version " + key)
        parent = version["parent_version"]
        if parent:
            require(version_number(version["version"]) > version_number(parent), "version does not advance parent")
        quality = version["quality_check"]
        if quality is not None:
            self.check_quality(quality)
            require((quality["asset_id"], quality["version"]) == (version["asset_id"], version["version"]), "quality belongs to another asset/version")
        if version["status"] in {"REVIEW", "APPROVED"}:
            require(quality is not None and quality["overall"] == "pass", "review/approval lacks passing technical checks")
        if version["status"] == "PLANNED":
            require(quality is None or quality["overall"] == "not_run", "planned proposal claims completed quality checks")
        approval = version["approval"]
        if approval is not None:
            require(len(approval["artifact_sha256"]) == len(version["artifact_ids"]), "approval hashes do not cover artifacts")
            # jsonschema's date-time checker is optional without format extras.
            timestamp = approval["decided_at"]
            require(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})", timestamp) is not None,
                    "approval timestamp must be RFC3339 with timezone")
            try:
                parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            except ValueError as exc:
                raise InvalidContract("approval timestamp is not a real date/time") from exc
            require(parsed.utcoffset() is not None, "approval timestamp has no timezone")


def add_blocker(output, code):
    blockers = output["generation_plan"]["blockers"]
    if not any(item["code"] == code for item in blockers):
        blockers.append({"code": code, "detail": "Synthetic offline fixture blocker; no execution."})


def remove_blocker(case, code):
    blockers = case["expected_output"]["generation_plan"]["blockers"]
    blockers[:] = [item for item in blockers if item["code"] != code]


def make_reference_case(case, *, count=1, operation="generate", rights="cleared", source="fixture"):
    """Build an explicitly synthetic valid starting point for adversarial mutations."""
    case = deepcopy(case)
    request, output = case["input"], case["expected_output"]
    asset, plan = output["asset_spec"], output["generation_plan"]
    refs = []
    for index in range(count):
        rid = f"synthetic_reference_{index}"
        roles = ["primary_identity"] if index == 0 else ["style"]
        if operation in {"edit", "inpaint"} and index == 0:
            roles.append("edit_base")
        refs.append({"reference_id": rid, "source": {"kind": "fixture", "fixture_id": rid} if source == "fixture" else
                     {"kind": "artifact", "artifact_id": f"{index + 1:032x}", "artifact_kind": "output"},
                     "roles": roles, "preserve": ["Synthetic identity intent"], "avoid_copy": [],
                     "rights": {"status": rights, "basis": "Synthetic contract test; not a rights assertion about a real image."},
                     "verification": {"scope": "fixture_only" if source == "fixture" else "unverified",
                                      "file": "fixture_only" if source == "fixture" else "unverified"}})
    request["reference_images"] = deepcopy(refs)
    request["generation_intent"] = operation
    output["references"] = deepcopy(refs)
    asset["references"] = deepcopy(refs)
    asset["generation_strategy"]["classification"] = operation
    asset["consistency"]["reference_bindings"] = {key: refs[0]["reference_id"] if key == "primary_reference" else None for key in BINDING_ROLES}
    output["reference_strategy"] = [{"reference_id": ref["reference_id"], "roles": ref["roles"], "instruction": "Use only the stated synthetic role."} for ref in refs]
    output["prompt_layers"][9]["text"] = "Synthetic references: " + ", ".join(ref["reference_id"] for ref in refs)
    output["prompt"] = assemble_prompt(output["prompt_layers"])
    plan["intent"] = operation
    plan["steps"] = [{"step_id": "synthetic_step", "operation": operation, "purpose": "Synthetic fixture only",
                       "output_count": 1, "reference_ids": [ref["reference_id"] for ref in refs],
                       "edit_base_reference": refs[0]["reference_id"] if operation in {"edit", "inpaint"} else None,
                       "references_from_steps": [], "depends_on": [], "step_prompt": output["prompt"],
                       "step_reference_constraints": []}]
    plan["calls_planned"] = 1
    output["estimated_cost"]["items"] = [{"step_id": "synthetic_step", "operation": operation, "backend_key": "codex", "image_count": 1, "reference_count": count}]
    add_blocker(output, "REFERENCE_UNVERIFIED")
    if rights in {"unknown", "restricted"}:
        add_blocker(output, "REFERENCE_RIGHTS_UNRESOLVED")
    if operation in {"edit", "inpaint"} and source != "artifact":
        add_blocker(output, "EDIT_BASE_REQUIRED")
    if operation == "inpaint":
        add_blocker(output, "UNSUPPORTED_OPERATION")
        add_blocker(output, "MASK_REQUIRED")
        for assessment in plan["parameters"]["assessments"]:
            if assessment["parameter"] == "operation":
                assessment["disposition"] = "unsupported"
    if count - int(operation in {"edit", "inpaint"}) > 8:
        add_blocker(output, "REFERENCE_LIMIT_EXCEEDED")
    return case


def synthetic_quality(overall="pass"):
    return {"schema_version": "1.0.0", "asset_id": "synthetic_asset", "version": "v2",
            "overall": overall, "checks": [{"check": name, "status": overall,
            "evidence": "Synthetic contract fixture only; no file, model or registry inspected."}
            for name in sorted(CHECKS)], "visual_evaluation": "human_only"}


def synthetic_version(status="APPROVED"):
    produced = status in {"DRAFT", "REVIEW", "APPROVED"}
    return {"schema_version": "1.0.0", "asset_id": "synthetic_asset", "version": "v2", "parent_version": "v1",
            "status": status, "artifact_ids": ["a" * 32] if produced else [], "task_ids": ["b" * 32] if produced else [],
            "spec_sha256": sha256_json({"synthetic": "spec"}), "style_id": "minimal", "style_schema_version": "1.0.0",
            "style_content_sha256": sha256_json({"synthetic": "style"}),
            "quality_check": synthetic_quality("pass") if produced else None,
            "approval": {"decision": "approved", "reviewer_id": "synthetic_human_reviewer", "decided_at": "2026-10-16T00:00:00Z",
                         "plan_sha256": "c" * 64, "artifact_sha256": ["d" * 64]} if status == "APPROVED" else None,
            "registration": "registered" if produced else "proposal"}


def run_negative_tests(contracts, cases):
    # Every negative is derived from an independently valid positive fixture.
    first = deepcopy(cases[0])
    character = next(case for case in cases if case["expected_output"]["asset_type"] == "character")
    product = next(case for case in cases if case["expected_output"]["asset_type"] == "product")
    refcase = make_reference_case(character)
    cross_scope = make_reference_case(character, source="artifact")
    rights = make_reference_case(character, rights="unknown")
    edit = make_reference_case(character, operation="edit")
    inpaint = make_reference_case(character, operation="inpaint")
    too_many = make_reference_case(character, count=9)
    baselines = [first, character, product, refcase, cross_scope, rights, edit, inpaint, too_many]
    for baseline in baselines:
        contracts.check_case(baseline)
    results = []

    def rejects(name, original, mutation, validator):
        value = deepcopy(original)
        mutation(value)
        try:
            validator(value)
        except InvalidContract:
            results.append(name)
            return
        raise InvalidContract(f"negative mutation accepted: {name}")

    def case_test(name, baseline, mutation):
        rejects(name, baseline, mutation, contracts.check_case)

    case_test("character_missing_face", character, lambda c: c["expected_output"]["asset_spec"]["appearance"].pop("face"))
    case_test("product_empty_material", product, lambda c: c["expected_output"]["asset_spec"].update(material=[]))
    case_test("character_identity_mismatch", character, lambda c: c["expected_output"]["asset_spec"]["identity"].update(character_id="wrong_character"))
    case_test("unresolved_fixed_path", first, lambda c: c["expected_output"]["asset_spec"]["property_policy"].update(fixed_paths=["/identity/does_not_exist"]))
    case_test("reference_binding_missing", refcase, lambda c: c["expected_output"]["asset_spec"]["consistency"]["reference_bindings"].update(face_reference="missing_reference"))
    case_test("reference_binding_wrong_role", refcase, lambda c: c["expected_output"]["asset_spec"]["consistency"]["reference_bindings"].update(face_reference="synthetic_reference_0"))
    case_test("duplicate_reference_id", refcase, lambda c: c["expected_output"]["asset_spec"]["references"].append(deepcopy(c["expected_output"]["asset_spec"]["references"][0])))
    case_test("fixture_claims_real_verification", refcase, lambda c: c["expected_output"]["asset_spec"]["references"][0]["verification"].update(scope="verified", file="verified"))
    case_test("unverified_scope_without_blocker", cross_scope, lambda c: remove_blocker(c, "REFERENCE_UNVERIFIED"))
    case_test("unknown_rights_without_blocker", rights, lambda c: remove_blocker(c, "REFERENCE_RIGHTS_UNRESOLVED"))
    case_test("output_asset_identity_mismatch", first, lambda c: c["expected_output"].update(asset_id="wrong_asset"))
    case_test("output_version_mismatch", first, lambda c: c["expected_output"].update(version="v99"))
    case_test("missing_prompt_layer", first, lambda c: c["expected_output"]["prompt_layers"].pop())
    case_test("wrong_prompt_layer_order", first, lambda c: c["expected_output"]["prompt_layers"].reverse())
    case_test("prompt_differs_from_layers", first, lambda c: c["expected_output"].update(prompt=c["expected_output"]["prompt"] + "\nAltered"))
    case_test("unknown_style_variable", first, lambda c: c["expected_output"]["asset_spec"]["style"]["variables"].update(unknown_variable="value"))
    case_test("reference_strategy_wrong_role", refcase, lambda c: c["expected_output"]["reference_strategy"][0].update(roles=["mask"]))
    case_test("duplicate_step_id", first, lambda c: c["expected_output"]["generation_plan"]["steps"].append(deepcopy(c["expected_output"]["generation_plan"]["steps"][0])))
    case_test("dependency_self_cycle", first, lambda c: c["expected_output"]["generation_plan"]["steps"][0].update(depends_on=[c["expected_output"]["generation_plan"]["steps"][0]["step_id"]]))
    case_test("missing_upstream_dependency", first, lambda c: c["expected_output"]["generation_plan"]["steps"][0].update(references_from_steps=["missing_step"]))
    case_test("step_prompt_missing", first, lambda c: c["expected_output"]["generation_plan"]["steps"][0].pop("step_prompt"))
    case_test("step_prompt_incomplete", first, lambda c: c["expected_output"]["generation_plan"]["steps"][0].update(step_prompt="Only a target without complete identity and constraints"))
    multistep = next(case for case in cases if len(case["expected_output"]["generation_plan"]["steps"]) > 1)
    case_test("upstream_reference_lacks_role_constraints", multistep, lambda c: c["expected_output"]["generation_plan"]["steps"][1].update(step_reference_constraints=[]))
    case_test("unknown_step_reference", first, lambda c: c["expected_output"]["generation_plan"]["steps"][0].update(reference_ids=["missing_reference"]))
    case_test("edit_base_not_in_references", edit, lambda c: c["expected_output"]["generation_plan"]["steps"][0].update(edit_base_reference="missing_reference"))
    case_test("edit_fixture_not_registered_output", edit, lambda c: remove_blocker(c, "EDIT_BASE_REQUIRED"))
    def missing_edit_base(c):
        c["expected_output"]["generation_plan"]["steps"][0]["edit_base_reference"] = None
        remove_blocker(c, "EDIT_BASE_REQUIRED")
    case_test("edit_missing_base_without_blocker", edit, missing_edit_base)
    case_test("calls_do_not_equal_steps", first, lambda c: c["expected_output"]["generation_plan"].update(calls_planned=8))
    case_test("cost_reference_double_count", refcase, lambda c: c["expected_output"]["estimated_cost"]["items"][0].update(reference_count=2))
    case_test("inpaint_unsupported_without_blocker", inpaint, lambda c: remove_blocker(c, "UNSUPPORTED_OPERATION"))
    case_test("inpaint_mask_missing_without_blocker", inpaint, lambda c: remove_blocker(c, "MASK_REQUIRED"))
    case_test("reference_limit_missing_blocker", too_many, lambda c: remove_blocker(c, "REFERENCE_LIMIT_EXCEEDED"))
    for code in sorted(MANDATORY_BLOCKERS):
        case_test("missing_" + code.lower(), first, lambda c, code=code: remove_blocker(c, code))

    def excess_budget(c):
        c["input"].setdefault("budget", {})["max_model_calls"] = 0
        c["expected_output"]["generation_plan"]["budget"]["max_model_calls"] = 0
        remove_blocker(c, "BUDGET_EXCEEDED")
    case_test("over_budget_missing_blocker", first, excess_budget)

    def ratio_without_blocker(c):
        c["input"]["aspect_ratio"] = "16:9"
        out = c["expected_output"]
        out["asset_spec"]["aspect_ratio"] = "16:9"
        out["generation_plan"]["parameters"]["requested"]["aspect_ratio"] = "16:9"
        out["generation_plan"]["parameters"]["applied"]["aspect_ratio"] = None
        for assessment in out["generation_plan"]["parameters"]["assessments"]:
            if assessment["parameter"] == "aspect_ratio":
                assessment["disposition"] = "unsupported"
        remove_blocker(c, "UNSUPPORTED_RATIO")
    case_test("unsupported_ratio_missing_blocker", first, ratio_without_blocker)

    def exact_without_blocker(c):
        exact = {"mode": "exact", "width": 1920, "height": 1080}
        c["input"]["resolution"] = exact
        c["expected_output"]["asset_spec"]["resolution"] = exact
        parameters = c["expected_output"]["generation_plan"]["parameters"]
        parameters["requested"]["resolution"] = exact
        for assessment in parameters["assessments"]:
            if assessment["parameter"] == "resolution":
                assessment["disposition"] = "unsupported"
        remove_blocker(c, "EXACT_RESOLUTION_UNSUPPORTED")
    case_test("exact_resolution_missing_blocker", first, exact_without_blocker)
    case_test("execution_enabled_in_planning", first, lambda c: c["expected_output"]["generation_plan"].update(execution_enabled=True))
    case_test("planning_claims_real_artifact", first, lambda c: c["expected_output"].update(artifact_ids=["a" * 32]))
    case_test("planning_claims_approval", first, lambda c: c["expected_output"].update(status="APPROVED"))
    case_test("target_does_not_advance_base", first, lambda c: c["expected_output"]["asset_spec"]["generation_strategy"].update(base_version=c["expected_output"]["asset_spec"]["version"], purpose="edit_version"))

    style = contracts.styles["cinematic-realism"]
    rejects("style_unknown_placeholder", style, lambda s: s.update(prompt_template=s["prompt_template"] + " {{unknown_variable}}"), contracts.check_style)
    rejects("style_unused_variable", style, lambda s: s["variables"].update(unused={"type": "string", "description": "test", "default": "test"}), contracts.check_style)
    enum_name = next(name for name, definition in style["variables"].items() if definition["type"] == "enum")
    rejects("style_enum_default_outside_choices", style, lambda s: s["variables"][enum_name].update(default="invalid_choice"), contracts.check_style)
    rejects("style_example_missing_variable", style, lambda s: s["examples"][0]["variables"].pop(enum_name), contracts.check_style)
    invalid_type = next(value for value in ASSET_TYPES if value not in style["asset_types"])
    rejects("style_example_type_not_supported", style, lambda s: s["examples"][0].update(asset_type=invalid_type), contracts.check_style)

    quality = synthetic_quality()
    contracts.check_quality(quality)
    rejects("quality_duplicate_check", quality, lambda q: q["checks"][1].update(check=q["checks"][0]["check"]), contracts.check_quality)
    rejects("quality_pass_with_unrun_check", quality, lambda q: q["checks"][0].update(status="not_run"), contracts.check_quality)
    rejects("quality_fail_without_failed_check", quality, lambda q: q.update(overall="fail"), contracts.check_quality)
    rejects("quality_missing_check", quality, lambda q: q["checks"].pop(), contracts.check_quality)
    rejects("quality_automatic_aesthetic_scoring", quality, lambda q: q.update(visual_evaluation="model_score"), contracts.check_quality)
    version = synthetic_version()
    contracts.check_version(version)
    rejects("approved_without_human_record", version, lambda v: v.update(approval=None), contracts.check_version)
    rejects("approved_without_artifact", version, lambda v: v.update(artifact_ids=[]), contracts.check_version)
    rejects("approved_without_registered_version", version, lambda v: v.update(registration="proposal"), contracts.check_version)
    rejects("quality_wrong_asset", version, lambda v: v["quality_check"].update(asset_id="another_asset"), contracts.check_version)
    rejects("quality_wrong_version", version, lambda v: v["quality_check"].update(version="v3"), contracts.check_version)
    rejects("version_does_not_advance_parent", version, lambda v: v.update(parent_version="v2"), contracts.check_version)
    rejects("approval_hash_count_mismatch", version, lambda v: v["approval"]["artifact_sha256"].append("e" * 64), contracts.check_version)
    rejects("approval_invalid_timestamp", version, lambda v: v["approval"].update(decided_at="not-a-date"), contracts.check_version)
    proposal = synthetic_version("PLANNED")
    contracts.check_version(proposal)
    rejects("proposal_claims_artifact", proposal, lambda v: v.update(artifact_ids=["a" * 32]), contracts.check_version)
    rejects("proposal_claims_finished_quality", proposal, lambda v: v.update(quality_check=synthetic_quality()), contracts.check_version)
    return results, len(baselines)


def run(root):
    output_policy = check_output_policy(root)
    contracts = Contracts(root)
    examples_count = contracts.load_styles()
    examples = read_json(root / "examples" / "asset-generation-examples.json")
    require(isinstance(examples, dict) and isinstance(examples.get("cases"), list), "examples must contain cases[]")
    cases = examples["cases"]
    require(len(cases) >= 15, "at least fifteen complete request/output fixtures required")
    unique([case["case_id"] for case in cases], "case IDs")
    errors = []
    for case in cases:
        try:
            contracts.check_case(case)
        except InvalidContract as exc:
            errors.append(f"{case['case_id']}: {exc}")
    require(not errors, "case validation failed: " + " | ".join(errors[:20]))
    represented = {case["expected_output"]["asset_type"] for case in cases}
    require({"character", "product"} <= represented, "character/product fixtures required for regression checks")
    quality_fixtures = [synthetic_quality("pass"), synthetic_quality("not_run"), synthetic_quality("fail")]
    for fixture in quality_fixtures:
        contracts.check_quality(fixture)
    version_fixtures = [synthetic_version(status) for status in ("PLANNED", "DRAFT", "REVIEW", "APPROVED")]
    for fixture in version_fixtures:
        contracts.check_version(fixture)
    negatives, supplemental = run_negative_tests(contracts, cases)
    require(len(negatives) >= 20, "insufficient adversarial coverage")
    # This deliberately uses an unregistered URI to prove retrieval fails closed.
    try:
        contracts.registry.resolver().lookup("https://not-registered.invalid/never-fetch.json")
    except Unresolvable:
        denied = True
    else:
        raise InvalidContract("unregistered schema URI unexpectedly resolved")
    return {"status": "passed", "scope": "offline_contract_and_fixture_validation",
            "schemas": len(contracts.schemas), "styles": len(contracts.styles), "style_examples": examples_count,
            "request_output_cases": len(cases), "asset_types_in_cases": sorted(represented),
            "supplemental_synthetic_cases": supplemental, "quality_contract_fixtures": len(quality_fixtures),
            "version_contract_fixtures": len(version_fixtures), "rejected_negative_mutations": len(negatives),
            "output_path_checks": output_policy,
            "remote_schema_retrieval_denied": denied, "model_calls": 0, "credit_operations": 0,
            "limitations": ["No NLP interpretation validation", "No image generation or visual-quality validation",
                            "No real file, account-scope, artifact registry or version registry verification"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON report destination; this is the only write.")
    args = parser.parse_args()
    destination = None
    try:
        if args.output:
            destination = report_destination(args.output)
        report = run(ROOT)
    except (InvalidContract, OSError, json.JSONDecodeError, SchemaError) as exc:
        report = {"status": "failed", "scope": "offline_contract_and_fixture_validation", "error": str(exc),
                  "model_calls": 0, "credit_operations": 0}
    serialized = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if destination is not None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
