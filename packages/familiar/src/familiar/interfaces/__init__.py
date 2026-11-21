"""Stable contracts shared by Familiar and its plugins."""

from .action import ACTION_STATUSES, ActionSpec, Actuator, Keyboard, KeyboardUser
from .data import (
    DATA_DIR_ENV,
    DataSet,
    load_data,
    read_user_data,
    save_user_data,
    user_data_path,
)
from .errors import BackendError, ConfigError, PluginError
from .game import GameAdapter
from .llm import REQUIRE_TOOL, LLMProvider
from .ptt import PushToTalk
from .quantity import spoken
from .service import CompanionService, ServiceTool, SpeechState
from .stt import SpeechToText
from .tts import TextToSpeech
from .types import AudioChunk, AudioClip, GameEvent, LLMChunk, Message, ToolCall, ToolSpec
from .watch import Watchable, WatchSample, WatchSpec

PLUGIN_API_VERSION = "2.4"

__all__ = [
    "PLUGIN_API_VERSION",
    "DATA_DIR_ENV",
    "DataSet",
    "load_data",
    "read_user_data",
    "save_user_data",
    "user_data_path",
    "spoken",
    "ACTION_STATUSES",
    "ActionSpec",
    "Actuator",
    "Keyboard",
    "KeyboardUser",
    "PushToTalk",
    "BackendError",
    "ConfigError",
    "PluginError",
    "GameAdapter",
    "CompanionService",
    "ServiceTool",
    "SpeechState",
    "LLMProvider",
    "REQUIRE_TOOL",
    "SpeechToText",
    "TextToSpeech",
    "AudioChunk",
    "AudioClip",
    "GameEvent",
    "LLMChunk",
    "Message",
    "ToolCall",
    "ToolSpec",
    "Watchable",
    "WatchSample",
    "WatchSpec",
]
