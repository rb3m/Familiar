import pytest
import yaml
from familiar_voice_server.config import ConfigError, load_config
from familiar_voice_server.worker import validate_engine

from .conftest import FakeEngine


def test_defaults_and_relative_paths(config):
    assert config.listen.host == "0.0.0.0"
    assert config.listen.port == 8765
    assert config.token_env == "FAMILIAR_VOICE_TOKEN"
    assert config.model.streaming_interval_s == 0.32
    assert config.languages == {"en": "english", "es": "spanish", "ja": "japanese"}
    assert config.voices["sarah"].ref_audio == config.path.parent / "voice.wav"
    data = yaml.safe_load(config.path.read_text())
    data.pop("lease")
    data.pop("worker")
    config.path.write_text(yaml.safe_dump(data))
    default = load_config(config.path)
    assert default.lease.ttl_s == 30
    assert default.lease.grace_s == 60
    assert default.worker.stop_timeout_s == 10


@pytest.mark.parametrize(
    "key,value,message",
    [
        ("listen", {"port": True}, "listen.port"),
        ("listen", {"port": 0}, "listen.port"),
        ("listen", {"port": 65536}, "listen.port"),
        ("listen", {"host": ""}, "listen.host"),
        ("lease", {"ttl_s": 0}, "lease.ttl_s"),
        ("lease", {"ttl_s": True}, "lease.ttl_s"),
        ("lease", {"ttl_s": "30"}, "lease.ttl_s"),
        ("lease", {"ttl_s": float("nan")}, "lease.ttl_s"),
        ("lease", {"grace_s": -1}, "lease.grace_s"),
        ("worker", {"stop_timeout_s": float("inf")}, "worker.stop_timeout_s"),
        ("model", {}, "model.id"),
        ("model", {"id": "fake", "streaming_interval_s": 0}, "streaming_interval_s"),
        ("token_env", "", "token_env"),
        ("voices", {}, "at least one voice"),
        ("voices", {"test": {"mode": "custom"}}, "mode"),
        ("voices", {"test": {"mode": "clone"}}, "ref_audio"),
        ("voices", {"test": {"mode": "clone", "ref_audio": "x"}}, "ref_text"),
        ("voices", {"test": {"mode": "design"}}, "description"),
        ("languages", [], "mapping"),
        ("languages", {}, "at least one language"),
        ("languages", {"en": ""}, "languages.en"),
        ("unknown", 1, "unknown fields"),
    ],
)
def test_config_errors(config, key, value, message):
    data = yaml.safe_load(config.path.read_text())
    data[key] = value
    config.path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match=message):
        load_config(config.path)


@pytest.mark.parametrize("content", ["", "[1, 2]", "model: [bad yaml"])
def test_invalid_yaml(config, content):
    config.path.write_text(content)
    with pytest.raises(ConfigError):
        load_config(config.path)


def test_missing_config(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read config"):
        load_config(tmp_path / "missing.yaml")


def test_design_and_zero_grace(config):
    data = yaml.safe_load(config.path.read_text())
    data["voices"] = {"designed": {"mode": "design", "description": "A warm low voice"}}
    data["lease"]["grace_s"] = 0
    config.path.write_text(yaml.safe_dump(data))
    config = load_config(config.path)
    assert config.voices["designed"].description == "A warm low voice"
    engine = FakeEngine(config)
    engine.model_type = "voice_design"
    validate_engine(config, engine)
    with pytest.raises(ValueError, match="voice_design"):
        validate_engine(config, FakeEngine(config))
