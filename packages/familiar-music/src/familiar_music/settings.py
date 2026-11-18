"""Merged data defaults and bounded numeric settings."""

import math

from familiar.interfaces import ConfigError, load_data


def number(settings, key, *, minimum=0, maximum=float("inf"), positive=False, integer=False):
    value = settings[key]
    if type(value) not in {int, float} or not math.isfinite(value):
        raise ConfigError(f"Music {key} must be finite")
    if value < minimum or value > maximum or (positive and value == 0):
        raise ConfigError(f"Music {key} is outside its allowed range")
    if integer and type(value) is not int:
        raise ConfigError(f"Music {key} must be an integer")
    return value


def music_settings(overrides):
    settings = {**load_data("familiar_music", "music").value, **(overrides or {})}
    number(settings, "duck_level", maximum=1)
    number(settings, "queue_top_up", integer=True, positive=True)
    number(settings, "search_limit", integer=True, positive=True)
    number(settings, "search_timeout_s", positive=True)
    number(settings, "track_remark_cooldown_s")
    number(settings, "reseed_cooldown_s")
    if not isinstance(settings["calmer_query"], str) or not settings["calmer_query"].strip():
        raise ConfigError("Music calmer_query must be a nonempty string")
    return settings


def pear_settings(overrides):
    defaults = load_data("familiar_music", "pear").value
    overrides = overrides or {}
    settings = {**defaults, **overrides}
    settings["endpoints"] = {**defaults["endpoints"], **overrides.get("endpoints", {})}
    for key in ("call_timeout_s", "auth_timeout_s", "poll_interval_s", "recovery_interval_s"):
        number(settings, key, positive=True)
    keys = ("base_url", "client_id", "token_path", "insert_now", "insert_end")
    strings = [settings[key] for key in keys] + list(settings["endpoints"].values())
    if any(not isinstance(value, str) or not value for value in strings):
        raise ConfigError("Music Pear paths, identifiers and endpoints must be nonempty strings")
    return settings
