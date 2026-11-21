"""Stage directions never reach the voice; emphasis and transcripts keep their words."""

import pytest
from familiar.fallback import FallbackTTS
from familiar.interfaces import AudioChunk
from familiar.persona import Persona
from familiar.pipeline.sentences import SentenceSplitter
from familiar.speech import MARKUP_RULE, is_direction, speakable


class Voice:
    def __init__(self, name, tags=False):
        self.name = name
        self.capabilities = frozenset({"streaming", *(["audio_tags"] if tags else [])})
        self.texts = []

    async def synthesize(self, text, voice, language):
        self.texts.append(text)
        yield AudioChunk(b"\0\0", 16000)


@pytest.mark.parametrize(
    ("text", "plain", "v3"),
    [
        # From live transcripts (Ruslana, 2026-11-21).
        (
            "*Sighs, smoke curling past the microphone.* Oh, *darling*, you ask about weather.",
            "Oh, darling, you ask about weather.",
            "Oh, darling, you ask about weather.",
        ),
        ("*Okh,* listen... *da*, *Bozhe moy*!", "Okh, listen... da, Bozhe moy!", None),
        ("*Let's Dance One Last Time.* is next.", "Let's Dance One Last Time. is next.", None),
        (
            "[sighs] Oh, *darling*, not again.",
            "Oh, darling, not again.",
            "[sighs] Oh, darling, not again.",
        ),
        ("[adjusts vinyl] Here.", "Here.", "Here."),
        ("Play [Track 2] now.", "Play [Track 2] now.", None),
        ("*She slowly exhales.* Fine.", "Fine.", None),
        ("Oh, *takes a drag*, darling.", "Oh, darling.", None),
        ("*clearly* not, a *humid* night.", "clearly not, a humid night.", None),
        ("[sighs].", "", "[sighs]."),
        # Review regressions (Codex).
        ("-10 degrees Celsius.", "-10 degrees Celsius.", None),
        ("Check the *nodes* before the burn.", "Check the nodes before the burn.", None),
        ("*Siphon* the fuel.", "Siphon the fuel.", None),
        ("**Sighs. Leans back.** Fine.", "Fine.", None),
        ("*nodding slowly* Yes.", "Yes.", None),
    ],
)
def test_speakable(text, plain, v3):
    assert speakable(text) == plain
    assert speakable(text, Voice("v3", tags=True)) == (v3 if v3 is not None else plain)


def test_direction_needs_an_action_word():
    assert is_direction("Leaning closer to the mic")
    assert not is_direction("dorogaya")
    assert not is_direction("pff")


@pytest.mark.parametrize(
    "direction", ["*She sighs. Leans back.*", "**Sighs. Leans back.**", "*She sighs.\nLeans back.*"]
)
def test_splitter_keeps_a_direction_whole(direction):
    splitter = SentenceSplitter()
    assert splitter.feed(direction + " Fine. Next one ") == [direction + " Fine."]


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_fails", [False, True])
async def test_fallback_cuts_tags_per_backend(primary_fails):
    from familiar.interfaces import BackendError

    v3, plain = Voice("v3", tags=True), Voice("gemini")
    if primary_fails:

        async def broken(text, voice, language):
            raise BackendError("down", retryable=False)
            yield

        v3.synthesize = broken
    tts = FallbackTTS(v3, plain, lambda name: None)
    assert "audio_tags" in tts.capabilities
    text = speakable("[sighs] *Okh*, fine.", tts)
    assert text == "[sighs] Okh, fine."
    chunks = [chunk async for chunk in tts.synthesize(text, None, "en")]
    assert chunks
    assert (plain.texts if primary_fails else v3.texts) == [
        "Okh, fine." if primary_fails else "[sighs] Okh, fine."
    ]


def test_markup_rule_reaches_every_persona_prompt():
    persona = Persona(
        id="ruslana",
        display_name="Ruslana",
        source="original",
        chattiness=0.5,
        prompt="You are a radio DJ.",
        style_rules=[],
        languages={"en": {}},
        game_hooks={},
    )
    assert MARKUP_RULE in persona.system_prompt("en")


def test_voice_skips_direction_only_sentences_but_keeps_the_transcript():
    from familiar.interfaces import LLMChunk
    from m1_support import run
    from test_m1_voice import make_session

    class LLM:
        closed = False

        async def stream(self, *args):
            yield LLMChunk("*Sighs, smoke curling.*\n[laughs] *Da*, darling. ")

        async def aclose(self):
            self.closed = True

    async def scenario():
        session = make_session()
        session.conversation.llm = LLM()
        await session.run()
        assert session.player.audio == ["Da, darling."]
        records = session.transcript.records
        said = [fields for kind, fields in records if kind == "assistant"][-1]
        assert said["heard"] is True  # a skipped direction doesn't make the answer unheard
        assert " ".join(said["text"].split()) == "*Sighs, smoke curling.* [laughs] *Da*, darling."

    run(scenario())
