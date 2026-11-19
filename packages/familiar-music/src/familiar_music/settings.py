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
    blend = settings["blend"]
    valid_blend = (
        isinstance(blend, dict)
        and set(blend) == {"library", "persona", "discovery"}
        and all(type(value) is int and value >= 0 for value in blend.values())
        and any(blend.values())
    )
    if not valid_blend:
        raise ConfigError(
            "Music blend needs non-negative integer library/persona/discovery weights; "
            "at least one must be positive"
        )
    number(settings, "local_min_matches", integer=True, positive=True)
    if not isinstance(settings["library_enrich"], bool):
        raise ConfigError("Music library_enrich must be true or false")
    number(settings, "duck_level", maximum=1)
    number(settings, "volume_tolerance", maximum=100)
    number(settings, "play_volume", maximum=100)
    number(settings, "calm_volume", maximum=100)
    number(settings, "queue_top_up", integer=True, positive=True)
    number(settings, "search_limit", integer=True, positive=True)
    number(settings, "genre_batch", integer=True, positive=True)
    number(settings, "playlist_tracks", integer=True, positive=True)
    number(settings, "search_timeout_s", positive=True)
    number(settings, "autonomy_hold_s")
    for key in ("remark_every", "dj_turn_every"):
        settings[key] = song_range(settings, key)
    if not isinstance(settings["autonomous_picks"], bool):
        raise ConfigError("Music autonomous_picks must be true or false")
    number(settings, "reseed_cooldown_s")
    if not isinstance(settings["calmer_query"], str) or not settings["calmer_query"].strip():
        raise ConfigError("Music calmer_query must be a nonempty string")
    return settings


def song_range(settings, key):
    """[low, high] whole numbers of songs, 1 <= low <= high."""
    value = settings[key]
    valid = (
        isinstance(value, list | tuple)
        and len(value) == 2
        and all(type(n) is int and n >= 1 for n in value)
        and value[0] <= value[1]
    )
    if not valid:
        raise ConfigError(f"Music {key} must be [low, high] song counts with 1 <= low <= high")
    return tuple(value)


def pear_settings(overrides):
    defaults = load_data("familiar_music", "pear").value
    overrides = overrides or {}
    settings = {**defaults, **overrides}
    settings["endpoints"] = {**defaults["endpoints"], **overrides.get("endpoints", {})}
    keys = (
        "call_timeout_s",
        "auth_timeout_s",
        "poll_interval_s",
        "recovery_interval_s",
        "insert_wait_s",
        "insert_poll_s",
    )
    for key in keys:
        number(settings, key, positive=True)
    keys = ("base_url", "client_id", "token_path", "insert_now", "insert_end")
    strings = [settings[key] for key in keys] + list(settings["endpoints"].values())
    if any(not isinstance(value, str) or not value for value in strings):
        raise ConfigError("Music Pear paths, identifiers and endpoints must be nonempty strings")
    settings["volume_curve"] = volume_curve(settings["volume_curve"])
    return settings


def mpv_settings(overrides):
    settings = {**load_data("familiar_music", "mpv").value, **(overrides or {})}
    for key in ("call_timeout_s", "quit_timeout_s", "spawn_poll_s"):
        number(settings, key, positive=True)
    strings = [settings[key] for key in ("executable", "socket_dir", "socket_name")]
    if any(not isinstance(value, str) or not value for value in strings):
        raise ConfigError("Music mpv executable and socket names must be nonempty strings")
    args = settings["args"]
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        raise ConfigError("Music mpv args must be a list of strings")
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
