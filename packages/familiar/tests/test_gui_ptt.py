"""PTT API proposals, persistence, and session exclusion without sockets."""

import asyncio
import sys
from unittest.mock import AsyncMock

import pytest
from familiar.config import AppConfig, BackendConfig, load_config
from familiar.gui import server
from test_gui import DirectClient


@pytest.fixture
def ptt_client(tmp_path):
    path = tmp_path / "config.toml"
    config = AppConfig(ptt=[BackendConfig("terminal")])
    app = server.create_app(config, config_path=path, token="secret")
    return DirectClient(app), app[server.API], path


async def test_get_and_save(ptt_client):
    client, service, path = ptt_client
    response = await client.get("/api/ptt")
    assert response.status == 200
    assert (await response.json())["bindings"][0]["label"] == "Terminal (Enter)"
    response = await client.put(
        "/api/ptt",
        json={
            "bindings": [
                {
                    "backend": "mouse",
                    "settings": {"button": 4, "consume": False},
                }
            ]
        },
    )
    assert response.status == 200
    assert (await response.json())["bindings"][0]["label"] == "Mouse button 4"
    assert load_config(path).ptt[0].settings == {"button": 4, "consume": False}
    assert service.sessions.config.ptt == service.config.ptt


async def test_detect_proposal_not_saved(ptt_client, monkeypatch):
    client, _, path = ptt_client
    detector = AsyncMock(return_value={"backend": "mouse", "button": 4})
    monkeypatch.setattr(server, "detect", detector)
    response = await client.post("/api/ptt/detect", json={"timeout": 12})
    assert response.status == 200
    assert (await response.json())["settings"] == {"button": 4}
    detector.assert_awaited_once_with(kind=None, timeout=12)
    assert not path.exists()


@pytest.mark.parametrize(
    "settings",
    [
        {"backend": "mouse", "settings": {"button": 1}},
        {"backend": "mouse", "settings": {"button": True}},
        {"backend": "mouse", "settings": {"button": "4"}},
        {"backend": "mouse", "settings": {"button": 4, "consume": "yes"}},
        {"backend": "evdev", "settings": {"device": "Stick"}},
        {"backend": "evdev", "settings": {"device": 4, "button": "BTN_TRIGGER"}},
        {"backend": "evdev", "settings": {"device": "Stick", "button": ""}},
        {"backend": "bogus", "settings": {}},
        {"backend": "mouse", "settings": None},
    ],
)
async def test_put_validation(ptt_client, settings):
    client, _, path = ptt_client
    assert (await client.put("/api/ptt", json={"bindings": [settings]})).status == 400
    assert not path.exists()


async def test_running_conflicts(ptt_client):
    client, service, path = ptt_client
    service.sessions._state["running"] = True
    assert (await client.post("/api/ptt/detect", json={})).status == 409
    assert (
        await client.put(
            "/api/ptt",
            json={
                "bindings": [
                    {
                        "backend": "mouse",
                        "settings": {"button": 4},
                    }
                ]
            },
        )
    ).status == 409
    assert not path.exists()


async def test_detection_excludes_detection_save_and_start(ptt_client, monkeypatch):
    client, _, _ = ptt_client
    entered, finish = asyncio.Event(), asyncio.Event()

    async def detect(kind=None, timeout=30):
        entered.set()
        await finish.wait()
        return {"backend": "mouse", "button": 4}

    monkeypatch.setattr(server, "detect", detect)
    task = asyncio.create_task(client.post("/api/ptt/detect", json={}))
    await entered.wait()
    try:
        assert (await client.post("/api/ptt/detect", json={})).status == 409
        assert (
            await client.put(
                "/api/ptt",
                json={
                    "bindings": [
                        {
                            "backend": "mouse",
                            "settings": {"button": 4},
                        }
                    ]
                },
            )
        ).status == 409
        assert (await client.post("/api/session/start", json={})).status == 409
    finally:
        finish.set()
        assert (await task).status == 200


async def test_timeout_and_recovery(ptt_client, monkeypatch):
    client, service, _ = ptt_client
    monkeypatch.setattr(server, "detect", AsyncMock(side_effect=TimeoutError))
    assert (await client.post("/api/ptt/detect", json={})).status == 408
    assert not service._ptt_lock.locked()


@pytest.mark.parametrize("timeout", [0, -1, 61, True, "30", None])
async def test_timeout_validation(ptt_client, timeout):
    client, _, _ = ptt_client
    assert (await client.post("/api/ptt/detect", json={"timeout": timeout})).status == 400


@pytest.mark.parametrize(
    "method,path,data",
    [
        ("get", "/api/ptt", None),
        ("post", "/api/ptt/detect", {}),
        ("put", "/api/ptt", {"backend": "mouse", "settings": {"button": 4}}),
    ],
)
async def test_auth_and_origin(ptt_client, method, path, data):
    client, _, _ = ptt_client
    kwargs = {} if data is None else {"json": data}
    request = getattr(client, method)
    assert (await request(path, headers={"X-Familiar-Token": "wrong"}, **kwargs)).status == 403
    assert (await request(path, headers={"Origin": "https://example.com"}, **kwargs)).status == 403


async def test_evdev_save(ptt_client):
    client, _, path = ptt_client
    response = await client.put(
        "/api/ptt",
        json={
            "bindings": [
                {
                    "backend": "evdev",
                    "settings": {"device": "Thrustmaster", "button": "BTN_TRIGGER"},
                }
            ]
        },
    )
    assert response.status == 200
    assert (await response.json())["bindings"][0]["label"] == "BTN_TRIGGER on Thrustmaster"
    assert load_config(path).ptt[0].backend == "evdev"


@pytest.mark.parametrize(
    "platform,kinds",
    [("darwin", ["mouse", "key"]), ("linux", ["joystick", "key", "mouse"]), ("win32", [])],
)
async def test_kinds(ptt_client, monkeypatch, platform, kinds):
    monkeypatch.setattr(sys, "platform", platform)
    client, _, _ = ptt_client
    data = await (await client.get("/api/ptt")).json()
    assert data["kinds"] == kinds
    assert data["kind"] == next(iter(kinds), None)
    assert data["detectable"] == bool(kinds)


async def test_detect_key_kind(ptt_client, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    client, _, path = ptt_client
    detector = AsyncMock(return_value={"backend": "key", "key": 105})
    monkeypatch.setattr(server, "detect", detector)
    response = await client.post("/api/ptt/detect", json={"kind": "key"})
    assert response.status == 200
    assert (await response.json())["label"] == "Key F13"
    detector.assert_awaited_once_with(kind="key", timeout=30)
    assert not path.exists()


@pytest.mark.parametrize("kind", ["joystick", "bogus", 1, None, [], {}])
async def test_invalid_detect_kind(ptt_client, monkeypatch, kind):
    monkeypatch.setattr(sys, "platform", "darwin")
    client, _, _ = ptt_client
    assert (await client.post("/api/ptt/detect", json={"kind": kind})).status == 400


@pytest.mark.parametrize(
    "settings",
    [
        {"key": 0},
        {"key": True},
        {"key": "105"},
        {},
        {"key": 105, "consume": 1},
        {"key": 105, "other": 1},
    ],
)
async def test_invalid_key_settings(ptt_client, settings):
    client, _, path = ptt_client
    response = await client.put(
        "/api/ptt", json={"bindings": [{"backend": "key", "settings": settings}]}
    )
    assert response.status == 400
    assert not path.exists()


@pytest.mark.parametrize("consume", [True, False])
async def test_key_config_round_trip(ptt_client, consume):
    client, service, path = ptt_client
    settings = {"key": 105, "consume": consume}
    response = await client.put(
        "/api/ptt", json={"bindings": [{"backend": "key", "settings": settings}]}
    )
    assert response.status == 200
    assert (await response.json())["bindings"][0]["label"] == "Key F13"
    assert load_config(path).ptt[0].settings == settings
    assert load_config(path).ptt[0].backend == "key"
    assert service.sessions.config.ptt == service.config.ptt


async def test_list_add_remove_and_invalid_lists(ptt_client):
    client, service, path = ptt_client
    bindings = [
        {"backend": "mouse", "settings": {"button": 4}},
        {"backend": "key", "settings": {"key": 105}},
    ]
    response = await client.put("/api/ptt", json={"bindings": bindings})
    assert response.status == 200
    assert len((await response.json())["bindings"]) == 2
    for invalid in (
        [],
        [bindings[0], bindings[0]],
        [bindings[0], {"backend": "terminal", "settings": {}}],
    ):
        assert (await client.put("/api/ptt", json={"bindings": invalid})).status == 400
    response = await client.put("/api/ptt", json={"bindings": bindings[1:]})
    assert response.status == 200
    assert (await (await client.get("/api/ptt")).json())["bindings"] == [
        {**bindings[1], "label": "Key F13"}
    ]
    assert len(load_config(path).ptt) == 1
    assert service.sessions.config.ptt == service.config.ptt
