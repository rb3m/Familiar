# Roadmap

| Milestone | Goal | Done when |
|---|---|---|
| **M0 Foundation** | Repo, interfaces, plugin loader, config, personas, Gemini LLM, text chat | `uv run familiar chat` streams a persona reply from Gemini; tests and ruff pass |
| **M1 Voice loop** | Push-to-talk → Gemini STT → LLM → Gemini TTS, ElevenLabs as 2nd TTS; Linux + Mac | Spoken round trip under ~2 s on both machines |
| **M2 ATS** | ATS adapter (TruckSim GPS server :31377), Director, first knowledge pack | Companion mentions a POI and low fuel during a real drive without spamming |
| **M3 Memory** | Session summaries + embeddings, recall tool (SwiftBlade-style) | "Remember last trip to Reno?" works |
| **M4 More games** | ETS2 (shares ATS adapter), KSP (Telemachus/kRPC), then MSFS, Elite (journal + EDSM) | Each has an adapter + knowledge pack |
| **M5 Hands & languages** | uinput actions as LLM tools, Touch Portal sync, Japanese practice mode | Voice command toggles something in-game |

Later options: Gemini Live speech-to-speech `Conversation` backend, local backends (whisper.cpp
Vulkan, Kokoro, Ollama), Mac desk assistant / note taker to the Vault, standard SCS telemetry
plugin for users without the TruckSim GPS port.
