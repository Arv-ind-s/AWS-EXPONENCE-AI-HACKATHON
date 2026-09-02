"""Contract coverage for the version-one explanation resource."""

from __future__ import annotations

from covenant_radar.api.v1.schemas.explain import ExplainRead


def test_schema_matches_implementation() -> None:
    schema = ExplainRead.model_json_schema()

    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"subject_type", "subject_id", "stages"}
    assert set(schema["properties"]) == {"subject_type", "subject_id", "stages"}

    stages = schema["properties"]["stages"]
    assert stages["minItems"] == 7
    assert stages["maxItems"] == 7
    stage_ref = stages["items"]["$ref"]
    stage_name = stage_ref.rsplit("/", maxsplit=1)[-1]
    stage_schema = schema["$defs"][stage_name]
    assert stage_schema["additionalProperties"] is False
    assert set(stage_schema["required"]) == {
        "stage",
        "name",
        "decider",
        "inputs",
        "outputs",
        "rule_or_prompt_version",
        "thresholds_compared",
        "confidence",
        "sources",
        "not_run",
        "row_id",
        "occurred_at",
    }
