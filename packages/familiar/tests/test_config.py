"""TOML settings, discovery precedence, paths, and environment secrets."""

import pytest
from familiar.config import load_config, resolve_secret
from familiar.interfaces import ConfigError


@pytest.fixture(autouse=True)
def isolate_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("FAMILIAR_CONFIG", raising=False)


def test_defaults(tmp_path):
    config = load_config()
    assert config.language == "en"
    assert config.persona == "mira"
    assert config.llm.backend == "gemini"
    assert config.llm.settings == {}
    assert config.stt is None and config.tts is None
    assert config.personas_dir is None
    assert config.transcript_dir == tmp_path / "data/transcripts"


def test_parse_and_relative_paths(tmp_path):
    directory = tmp_path / "settings"
    directory.mkdir()
    path = directory / "custom.toml"
    path.write_text("""language = "es"
persona = "mira"
personas_dir = "../characters"
transcript_dir = "logs"
[llm]
backend = "gemini"
[llm.gemini]
model = "test-model"
api_key_env = "TEST_SECRET"
temperature = 0.2
[tts]
backend = "speaker"
[tts.speaker]
volume = 0.5
""")
    config = load_config(path)
    assert config.language == "es"
    assert config.persona == "mira"
    assert config.personas_dir == tmp_path / "characters"
    assert config.transcript_dir == directory / "logs"
    assert config.llm.settings == {
        "model": "test-model",
        "api_key_env": "TEST_SECRET",
        "temperature": 0.2,
    }
    assert config.tts.backend == "speaker"
    assert config.tts.settings == {"volume": 0.5}


def test_precedence(tmp_path, monkeypatch):
    user = tmp_path / ".config/familiar/config.toml"
    user.parent.mkdir(parents=True)
    user.write_text('language = "ja"')
    assert load_config().language == "ja"
    (tmp_path / "config.toml").write_text('language = "es"')
    assert load_config().language == "es"
    env = tmp_path / "env.toml"
    env.write_text('language = "en"')
    monkeypatch.setenv("FAMILIAR_CONFIG", str(env))
    assert load_config().language == "en"
    assert load_config(user).language == "ja"


def test_invalid_or_missing_config(tmp_path):
    with pytest.raises(ConfigError):
        load_config(tmp_path / "missing.toml")
    path = tmp_path / "invalid.toml"
    path.write_text("invalid = [")
    with pytest.raises(ConfigError):
        load_config(path)


def test_resolve_secret(monkeypatch):
    monkeypatch.setenv("TEST_SECRET", "test-key")
    assert resolve_secret({"api_key_env": "TEST_SECRET"}) == "test-key"
    assert resolve_secret({"token_env": "TEST_SECRET"}, "token_env") == "test-key"
    monkeypatch.delenv("TEST_SECRET")
    with pytest.raises(ConfigError):
        resolve_secret({"api_key_env": "TEST_SECRET"})
    with pytest.raises(ConfigError):
        resolve_secret({})


@pytest.mark.parametrize("kind", ["tts", "stt"])
def test_fallback_list(tmp_path, kind):
    path = tmp_path / "chain.toml"
    path.write_text(f"""[{kind}]
backend = "local"
fallback = ["elevenlabs", "gemini"]
[{kind}.elevenlabs]
model = "second"
[{kind}.gemini]
model = "third"
""")
    spec = getattr(load_config(path), kind)
    assert spec.backend == "local"
    assert spec.fallback.backend == "elevenlabs"
    assert spec.fallback.settings == {"model": "second"}
    assert spec.fallback.fallback.backend == "gemini"
    assert spec.fallback.fallback.settings == {"model": "third"}
    assert spec.fallback.fallback.fallback is None


@pytest.mark.parametrize(
    "fallback",
    [
        '["local"]',
        '["gemini", "gemini"]',
        '[""]',
        '[" "]',
        "[42]",
        "42",
    ],
)
def test_invalid_fallback_list(tmp_path, fallback):
    path = tmp_path / "chain.toml"
    path.write_text(f'[tts]\nbackend = "local"\nfallback = {fallback}\n')
    with pytest.raises(ConfigError):
        load_config(path)


def test_fallback_settings_must_be_table(tmp_path):
    path = tmp_path / "chain.toml"
    path.write_text('[tts]\nbackend = "local"\nfallback = ["gemini"]\ngemini = 42\n')
    with pytest.raises(ConfigError, match="must be a table"):
        load_config(path)
