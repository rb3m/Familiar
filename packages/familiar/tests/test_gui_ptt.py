"""PTT API proposals, persistence, and session exclusion without sockets."""

import asyncio
from unittest.mock import AsyncMock

import pytest
from familiar.config import AppConfig, BackendConfig, load_config
from familiar.gui import server
from test_gui import DirectClient


@pytest.fixture
def ptt_client(tmp_path):
    path = tmp_path / "config.toml"
    config = AppConfig(ptt=BackendConfig("terminal"))
    app = server.create_app(config, config_path=path, token="secret")
    return DirectClient(app), app[server.API], path


async def test_get_and_save(ptt_client):
    client, service, path = ptt_client
    response = await client.get("/api/ptt")
    assert response.status == 200
    assert (await response.json())["label"] == "Terminal (Enter)"
    response = await client.put(
        "/api/ptt",
        json={
            "backend": "mouse",
            "settings": {"button": 4, "consume": False},
        },
    )
    assert response.status == 200
    assert (await response.json())["label"] == "Mouse button 4"
    assert load_config(path).ptt.settings == {"button": 4, "consume": False}
    assert service.sessions.config.ptt == service.config.ptt


async def test_detect_proposal_not_saved(ptt_client, monkeypatch):
    client, _, path = ptt_client
    detector = AsyncMock(return_value={"backend": "mouse", "button": 4})
    monkeypatch.setattr(server, "detect", detector)
    response = await client.post("/api/ptt/detect", json={"timeout": 12})
    assert response.status == 200
    assert (await response.json())["settings"] == {"button": 4}
    detector.assert_awaited_once_with(12)
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
    assert (await client.put("/api/ptt", json=settings)).status == 400
    assert not path.exists()


async def test_running_conflicts(ptt_client):
    client, service, path = ptt_client
    service.sessions._state["running"] = True
    assert (await client.post("/api/ptt/detect", json={})).status == 409
    assert (
        await client.put(
            "/api/ptt",
            json={
                "backend": "mouse",
                "settings": {"button": 4},
            },
        )
    ).status == 409
    assert not path.exists()


async def test_detection_excludes_detection_save_and_start(ptt_client, monkeypatch):
    client, _, _ = ptt_client
    entered, finish = asyncio.Event(), asyncio.Event()

    async def detect(timeout):
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
                    "backend": "mouse",
                    "settings": {"button": 4},
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
            "backend": "evdev",
            "settings": {"device": "Thrustmaster", "button": "BTN_TRIGGER"},
        },
    )
    assert response.status == 200
    assert (await response.json())["label"] == "BTN_TRIGGER on Thrustmaster"
    assert load_config(path).ptt.backend == "evdev"
