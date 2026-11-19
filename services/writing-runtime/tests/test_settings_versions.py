"""Settings edits are conditional on their own resource and survive real failures."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
import pytest

from zhimeng_runtime.errors import install_exception_handlers
from zhimeng_runtime.provider_settings_api import (
    ProviderModelOverridePatch, ProviderProfileUpdate, ProviderSettingsUpdate, create_provider_settings_router,
)
from zhimeng_runtime.provider_settings_service import ProviderSettings, ProviderSettingsConflictError, ProviderSettingsService


def body(provider_id="first", **changes):
    return {"provider_id": provider_id, "name": provider_id, "base_url": "https://fixture.invalid/v1",
            "model": "fixture-model", "selected_models": ["fixture-model"], "api_key": "synthetic-private-key",
            **changes}


@pytest.fixture
def provider_client(tmp_path):
    service = ProviderSettingsService(tmp_path / "providers.json", fallback=ProviderSettings(None, None, None))
    app = FastAPI()
    install_exception_handlers(app)
    app.include_router(create_provider_settings_router(service))
    with TestClient(app) as client:
        yield client, service


def save(client, payload):
    response = client.put("/api/v1/settings/providers", json=payload)
    assert response.status_code == 200, response.text
    return response.json()


def test_two_editors_conflict_preserves_first_save_and_all_credentials(provider_client):
    client, service = provider_client
    first = save(client, body())["profile"]
    second = save(client, body(name="from first editor", expected_revision=first["revision"]))["profile"]
    assert first["revision"] != second["revision"]
    before = service._path.read_bytes()
    stale = client.put("/api/v1/settings/providers", json=body(name="stale editor", expected_revision=first["revision"]))
    assert stale.status_code == 409 and stale.json()["error"]["code"] == "settings_conflict"
    assert "synthetic-private-key" not in stale.text
    assert service._path.read_bytes() == before
    final = save(client, body(name="resolved edit", expected_revision=second["revision"]))["profile"]
    assert final["name"] == "resolved edit" and final["api_key_set"]
    assert final["source"] == "settings" and final["applies_to"] == "next_request"
    assert "synthetic-private-key" not in json.dumps(final)


def test_resource_revision_ignores_other_profiles_and_connection_tests(provider_client):
    client, service = provider_client
    first = save(client, body())["profile"]
    save(client, body("second", make_active=True))
    assert client.get("/api/v1/settings/providers").json()["providers"][0]["revision"] == first["revision"]
    result = save(client, body(name="independent edit", expected_revision=first["revision"]))
    assert result["catalog"]["active_provider_id"] == "second"
    assert service.resolve_profile("first")[0].name == "independent edit"


@pytest.mark.parametrize("operation", ["delete", "activate", "override", "legacy"])
def test_all_settings_writes_reject_stale_resource_versions(provider_client, operation):
    client, service = provider_client
    initial = save(client, body())
    profile_revision = initial["profile"]["revision"]
    global_revision = initial["active_settings"]["revision"]
    save(client, body(name="changed elsewhere"))
    before = service._path.read_bytes()
    if operation == "delete":
        response = client.delete(f"/api/v1/settings/providers/first?expected_revision={profile_revision}")
    elif operation == "activate":
        response = client.post(f"/api/v1/settings/providers/first/activate?expected_revision={profile_revision}")
    elif operation == "override":
        response = client.patch("/api/v1/settings/providers/first/model-overrides/fixture-model",
                                json={"base_url": "https://other.invalid/v1", "expected_revision": profile_revision})
    else:
        response = client.put("/api/v1/settings/provider", json={"model": "different", "expected_revision": global_revision})
    assert response.status_code == 409, response.text
    assert service._path.read_bytes() == before


def test_deleted_resource_is_not_resurrected_by_an_old_form(provider_client):
    client, service = provider_client
    profile = save(client, body())["profile"]
    assert client.delete("/api/v1/settings/providers/first").status_code == 200
    before = service._path.read_bytes()
    response = client.put("/api/v1/settings/providers", json=body(expected_revision=profile["revision"]))
    assert response.status_code == 409 and service._path.read_bytes() == before


def test_retried_provider_creation_does_not_duplicate_or_overwrite(provider_client):
    client, service = provider_client
    first = save(client, body(provider_id="provider-" + "a" * 32, expected_revision="missing"))
    before = service._path.read_bytes()
    response = client.put("/api/v1/settings/providers", json=body(provider_id=first["profile"]["provider_id"],
                         name="must not overwrite", expected_revision="missing"))
    assert response.status_code == 409 and service._path.read_bytes() == before
    assert len(client.get("/api/v1/settings/providers").json()["providers"]) == 1


def test_unknown_fields_survive_and_reset_still_removes_known_fields(provider_client):
    client, service = provider_client
    save(client, body(wire_format="responses"))
    document = json.loads(service._path.read_bytes())
    document["extension-root"] = {"preserve": [1, "中文"]}
    document["providers"][0]["extension-profile"] = {"preserve": True}
    service._path.write_text(json.dumps(document), encoding="utf-8")
    current = client.get("/api/v1/settings/providers").json()["providers"][0]
    save(client, body(wire_format="auto", expected_revision=current["revision"]))
    result = json.loads(service._path.read_bytes())
    assert result["extension-root"] == document["extension-root"]
    assert result["providers"][0]["extension-profile"] == {"preserve": True}
    assert "wire_format" not in result["providers"][0]


@pytest.mark.parametrize("broken", [b"{broken", b"[]", b'{"providers": null}', b'{"providers": [{"name": "lost id"}]}',
    b'{"providers": [{"provider_id": "bad/id"}]}',
    b'{"providers": [{"provider_id": " same "}, {"provider_id": "same"}]}',
    b'{"providers": [{"provider_id": "same"}, {"provider_id": "same"}]}'])
def test_unreadable_configuration_is_neither_empty_success_nor_overwritten(provider_client, broken):
    client, service = provider_client
    service._path.write_bytes(broken)
    for method, path, payload in [
        ("get", "/api/v1/settings/providers", None), ("get", "/api/v1/settings/provider", None),
        ("put", "/api/v1/settings/providers", body()), ("put", "/api/v1/settings/provider", {"model": "bad"}),
    ]:
        response = client.request(method, path, json=payload)
        assert response.status_code == 503 and response.json()["error"]["code"] == "provider_settings_unreadable"
        assert service._path.read_bytes() == broken


def test_external_write_during_temporary_file_flush_is_not_lost(provider_client, monkeypatch):
    client, service = provider_client
    save(client, body())
    external = json.loads(service._path.read_bytes())
    external["providers"][0]["name"] = "external editor"
    external_bytes = json.dumps(external).encode()
    original_fsync = __import__("os").fsync

    def external_edit(fd):
        original_fsync(fd)
        service._path.write_bytes(external_bytes)

    monkeypatch.setattr("zhimeng_runtime.provider_settings_service.os.fsync", external_edit)
    response = client.put("/api/v1/settings/providers", json=body(name="stale writer"))
    assert response.status_code == 409
    assert service._path.read_bytes() == external_bytes
    assert not list(service._path.parent.glob("*.tmp"))


def test_two_service_instances_share_read_modify_write_lock(tmp_path):
    services = [ProviderSettingsService(tmp_path / "providers.json") for _ in range(2)]
    args = dict(provider_id="one", name="initial", base_url="https://fixture.invalid/v1", model="fixture", api_key=None,
                selected_models=None, reasoning_effort=None, make_active=False)
    revision = services[0].save_profile(**args)["profile"]["revision"]
    barrier = threading.Barrier(2)

    def change(index):
        barrier.wait()
        try:
            return services[index].save_profile(**{**args, "name": str(index)}, expected_revision=revision)["profile"]["name"]
        except ProviderSettingsConflictError:
            return "conflict"

    with ThreadPoolExecutor(2) as pool:
        outcomes = list(pool.map(change, range(2)))
    assert outcomes.count("conflict") == 1
    assert services[0].catalog_view()["providers"][0]["name"] in {"0", "1"}


@pytest.mark.parametrize("broken", [b'{"broken', b'{"providers":[{"provider_id":"saved","max_retries":"invalid"}]}'])
def test_unreadable_provider_does_not_prevent_runtime_start_or_repair(settings, broken):
    from zhimeng_runtime.app import create_app

    path = settings.database_path.parent / "provider-settings.json"
    path.write_bytes(broken)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/health/ready").status_code == 200
        assert client.get("/api/v1/agent/sessions").status_code == 200
        assert client.get("/api/v1/settings/providers").status_code == 503
        failed = client.post("/api/v1/agent/sessions", json={"title": "read failure"})
        assert failed.status_code == 503 and "配置" in failed.json()["error"]["message"]
        assert path.read_bytes() == broken
        # An external repair restores the same running process; do not silently
        # discard the unreadable file to achieve this recovery.
        path.write_text('{"providers": [], "active_provider_id": null}', encoding="utf-8")
        result = save(client, body())
        assert result["catalog"]["active_settings"]["revision"] == result["catalog"]["revision"]
        created = client.post("/api/v1/agent/sessions", json={"title": "after repair"})
        assert created.status_code == 201, created.text


@pytest.mark.parametrize("filename,model", [
    ("provider-settings-update.schema.json", ProviderSettingsUpdate),
    ("provider-profile-update.schema.json", ProviderProfileUpdate),
    ("provider-model-override-patch.schema.json", ProviderModelOverridePatch),
])
def test_settings_write_contracts_match_models(filename, model):
    schema = json.loads((Path(__file__).resolve().parents[3] / "packages/contracts" / filename).read_bytes())
    Draft202012Validator.check_schema(schema)
    schema.pop("$schema")
    schema.pop("$id")
    assert schema == model.model_json_schema(by_alias=True)
