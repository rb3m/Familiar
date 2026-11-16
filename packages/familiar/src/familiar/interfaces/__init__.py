"""Stable contracts shared by Familiar and its plugins."""

from .errors import BackendError, ConfigError, PluginError
from .game import GameAdapter
from .llm import LLMProvider
from .ptt import PushToTalk
from .stt import SpeechToText
from .tts import TextToSpeech
from .types import AudioChunk, AudioClip, GameEvent, LLMChunk, Message, ToolCall, ToolSpec

PLUGIN_API_VERSION = "1.0"

__all__ = [
    "PLUGIN_API_VERSION",
    "PushToTalk",
    "BackendError",
    "ConfigError",
    "PluginError",
    "GameAdapter",
    "LLMProvider",
    "SpeechToText",
    "TextToSpeech",
    "AudioChunk",
    "AudioClip",
    "GameEvent",
    "LLMChunk",
    "Message",
    "ToolCall",
    "ToolSpec",
]
