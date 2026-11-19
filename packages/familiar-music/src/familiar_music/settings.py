"""Merged data defaults and bounded numeric settings."""

import math
from itertools import pairwise

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
    number(settings, "volume_tolerance", maximum=100)
    number(settings, "play_volume", maximum=100)
    number(settings, "calm_volume", maximum=100)
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
    settings["volume_curve"] = volume_curve(settings["volume_curve"])
    return settings


def volume_curve(points):
    """Pairs of [written, read back], strictly rising on both sides so the curve inverts."""
    try:
        curve = [(float(written), float(read)) for written, read in points]
    except (TypeError, ValueError) as exc:
        raise ConfigError("Music volume_curve must be a list of [written, read] pairs") from exc
    rising = all(a[0] < b[0] and a[1] < b[1] for a, b in pairwise(curve))
    if len(curve) < 2 or not rising:
        raise ConfigError("Music volume_curve must have two or more strictly rising pairs")
    return curve
