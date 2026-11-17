"""Validated model defaults shared by the built-in backends."""

from familiar.interfaces import ConfigError, load_data


def model_defaults(backend: str) -> dict:
    dataset = load_data("familiar", "models")
    source = ", ".join(dataset.sources)
    if not isinstance(dataset.value, dict):
        raise ConfigError(f"Models must be a mapping in {source}")
    defaults = dataset.value.get(backend)
    required = {"gemini": ("llm", "stt", "tts"), "elevenlabs": ("stt", "tts")}[backend]
    if not isinstance(defaults, dict) or any(
        not isinstance(defaults.get(key), str) or not defaults[key].strip() for key in required
    ):
        raise ConfigError(f"{backend} model IDs must be non-empty strings in {source}")
    if backend == "gemini":
        fallbacks = defaults.get("fallback_models")
        if not isinstance(fallbacks, list) or any(
            not isinstance(model, str) or not model.strip() for model in fallbacks
        ):
            raise ConfigError(f"Gemini fallback_models must be a list of model IDs in {source}")
    return defaults
