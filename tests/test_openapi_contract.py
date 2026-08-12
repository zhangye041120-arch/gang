from api_server import create_app
from tests.test_api_v1 import fake_agent


def spec():
    return create_app(fake_agent, api_token="test-token", test_mode=True).openapi()


def test_openapi_locks_bearer_security_and_v1_schemas():
    document = spec()
    assert document["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"
    path = document["paths"]["/v1/chat"]["post"]
    assert {"bearerAuth": []} in path["security"]
    request_schema = document["components"]["schemas"]["V1ChatRequest"]
    assert set(request_schema["required"]) == {"user_id", "session_id", "message"}
    assert request_schema["additionalProperties"] is False
    assert set(request_schema["properties"]) == {
        "user_id", "session_id", "message", "context", "conversation_history", "debug",
    }
    response_schema = document["components"]["schemas"]["V1ChatResponse"]
    assert response_schema["additionalProperties"] is False
    assert set(response_schema["required"]) == {
        "session_id", "reply", "intent", "action",
        "blocked", "crisis_detected", "safety_violation", "rewritten",
    }
    assert response_schema["properties"]["intent"]["enum"] == [
        "chat", "checkin", "game", "exercise", "assessment", "community",
    ]
    assert "debug" in response_schema["properties"]
    assert "V1DebugInfo" in document["components"]["schemas"]
    assert document["paths"]["/chat"]["post"]["deprecated"] is True
    assert "/v1/chat/stream" in document["paths"]
    assert "/v1/speech" in document["paths"]


def test_openapi_documents_stable_error_responses():
    document = spec()
    path = document["paths"]["/v1/chat"]["post"]
    for code in ("401", "403", "422", "429", "502", "504"):
        assert code in path["responses"]
        response = path["responses"][code]
        schema = next(iter(response["content"].values()))["schema"]
        assert schema["$ref"] == "#/components/schemas/ApiError"
    assert "ApiError" in document["components"]["schemas"]


def test_generated_openapi_file_matches_runtime_spec():
    import json
    from pathlib import Path

    generated = json.loads(
        (Path(__file__).resolve().parents[1] / "openapi.json").read_text(encoding="utf-8")
    )
    runtime = spec()
    assert generated["paths"] == runtime["paths"]
    assert generated["components"] == runtime["components"]


def test_error_code_document_covers_frozen_codes():
    from pathlib import Path

    from xiaoliao_agent import api_contract

    document = (
        Path(__file__).resolve().parents[1] / "docs" / "API错误码表.md"
    ).read_text(encoding="utf-8")
    for code in api_contract.ERROR_CODES:
        assert code in document
