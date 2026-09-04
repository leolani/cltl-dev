# Open-Source Framework Alternatives to CLTL Eliza App

**Research date:** 2026-06-12  
**Method:** Deep research — 106 agents · 23 sources · 112 claims extracted · 25 adversarially verified (16 confirmed, 9 killed)

---

## Research Question

What open-source frameworks exist that could replace or partially replace the CLTL Eliza App framework — a modular, event-driven robot communication framework that:

1. Routes data between modules (VAD, ASR, LLM, TTS, robot backends) via an event bus / message broker such as RabbitMQ
2. Records interaction data in a structured format (EMISSOR)
3. Supports distributed deployment across server + hardware client via Docker containers
4. Supports multiple simultaneous interactions in parallel
5. Must support many different hardware variants in a research/lab context

**Constraints:** Must be open-source and self-hostable. Language is no constraint. Replacement can be full or partial.

---

## Executive Summary

No single open-source framework matches all five requirements simultaneously. The most practical path is a **layered partial replacement**:

- **Wrapyfi** for hardware abstraction and multi-middleware routing
- **LiveKit Agents** or **Pipecat** for the conversational pipeline (VAD/ASR/LLM/TTS)
- A **custom layer** to replace EMISSOR (no existing equivalent was found)

---

## Candidate Frameworks

### 1. Wrapyfi — `high confidence`

**Best fit for: requirements 1, 3, 5 (event routing, distributed deployment, multi-hardware)**

A polyglot middleware wrapper that supports seven middleware systems — YARP, ROS, ROS 2, ZeroMQ, WebSocket, Zenoh, MQTT — via a `@MiddlewareCommunicator.register()` decorator API. It enables distributed multi-machine deployment without modifying Python business logic. Published at HRI 2024.

> "eases the development of scripts that run on multiple machines, thereby enabling cross-platform communication and workload distribution"

- GitHub: <https://github.com/modular-ml/wrapyfi>
- Paper: <https://arxiv.org/html/2302.09648v5>

| Requirement | Covered |
|---|---|
| (1) Event-driven routing | ✓ |
| (2) Structured recording | — |
| (3) Distributed deployment | ✓ |
| (4) Parallel sessions | — |
| (5) Multi-hardware / research lab | ✓ |

---

### 2. LiveKit Agents — `high confidence`

**Best fit for: requirements 1, 4 (modular pipeline, swappable VAD/ASR/LLM/TTS)**

A mature, actively maintained Python framework (v1.6.0 released June 11, 2026) with a fully swappable pipeline for VAD, STT, LLM, and TTS from multiple providers. Each stage is independently testable and optimizable.

> "A comprehensive ecosystem to mix and match the right STT, LLM, TTS, and Realtime API to suit your use case"

- GitHub: <https://github.com/livekit/agents>
- Blog: <https://livekit.io/blog/sequential-pipeline-architecture-voice-agents>

| Requirement | Covered |
|---|---|
| (1) Modular routing | ✓ |
| (2) Structured recording | — |
| (3) Distributed deployment | partial |
| (4) Parallel sessions | partial |
| (5) Multi-hardware | — |

---

### 3. Pipecat — `medium confidence`

**Best fit for: requirements 1, 3, 4 (pipeline, distributed deployment, parallel sessions)**

Supports distributed multi-agent deployment via RedisBus or PgmqBus across processes and machines. Hardware client SDKs exist for ESP32 and C++. One verifier dissented noting self-hosted distributed deployment outside Daily.co is less battle-tested.

> "hand off, fan out in parallel, and coordinate over a shared bus, locally or distributed across processes and machines"

- GitHub: <https://github.com/pipecat-ai/pipecat>
- Docs: <https://docs.pipecat.ai/subagents/fundamentals/agent-bus>

| Requirement | Covered |
|---|---|
| (1) Event routing | ✓ |
| (2) Structured recording | — |
| (3) Distributed deployment | ✓ |
| (4) Parallel sessions | ✓ |
| (5) Multi-hardware | partial |

**Caveat:** production maturity of self-hosted distributed mode (outside Daily.co) is unverified (2-1 vote).

---

### 4. OVOS (Open Voice OS) — `high confidence`

**Best fit for: requirements 1, 3 (message bus backbone, modular Docker microservices)**

Uses a message bus as its first-class inter-component backbone. VAD, ASR/STT, and TTS can run as standalone Docker microservices. The claim about broad robotics hardware support was **refuted 0-3** — hardware footprint is primarily consumer voice assistants, not research robots.

- Docs: <https://openvoiceos.github.io/ovos-technical-manual/>
- Docker STT: <https://github.com/OpenVoiceOS/ovos-docker-stt>
- Docker TTS: <https://github.com/OpenVoiceOS/ovos-docker-tts>

| Requirement | Covered |
|---|---|
| (1) Message bus | ✓ |
| (2) Structured recording | — |
| (3) Containerized deployment | ✓ |
| (4) Parallel sessions | — |
| (5) Robot hardware variety | unverified |

---

### 5. OM1 (OpenMind) — `high confidence`

**Best fit for: requirement 3 partial (Docker HALs with DDS/WebSocket)**

Go-based (Python version deprecated). Hardware abstraction layers are dockerized and interface via CycloneDDS, Zenoh, or WebSockets. Explicitly **has no conversation history export** (confirmed via open GitHub issue #1731, Jan 2026).

- GitHub: <https://github.com/OpenMind/OM1>

| Requirement | Covered |
|---|---|
| (1) Event bus | — |
| (2) Structured recording | — |
| (3) Distributed deployment | partial |
| (4) Parallel sessions | — |
| (5) Multi-hardware | partial |

**Note:** Go-based runtime limits Python interoperability for rapid research prototyping.

---

### 6. ESPnet-SDS — `high confidence`

**Best fit for: requirement 5 only (model breadth)**

A web-interface toolkit for spoken dialogue systems with modular wrapper classes for all pipeline stages (294+ ASR models, 36,464+ LLMs from Hugging Face). No event bus, no Docker multi-node deployment, no structured recording. Suitable only as a component model library, not as a framework replacement.

- Paper: <https://arxiv.org/abs/2503.08533> (NAACL 2025 Demo)

---

### 7. X-Talk — `medium confidence` ⚠️ public availability unverified

**Best fit for: requirements 1, 4 (event bus + per-session isolation)**

A December 2025 arXiv preprint describing a pub-sub event bus routing ASR/LLM/TTS modules with per-WebSocket-connection session isolation. Architecturally very close to CLTL's design.

> "inputs and outputs of all modules are uniformly encoded as structured events and propagated along a central event bus"

- Preprint: <https://arxiv.org/html/2512.18706>

**Caveat:** No public repository has been found. Production readiness and open-source status are unconfirmed.

---

### 8. ChipChat — design reference only (not open-source)

Architecturally the closest match to CLTL: uses RabbitMQ for inter-process message passing between ASR, LLM, TTS, and vocoder, with six modular deployment configurations. Apple-internal system (ASRU 2025 Best Demo Paper); no public repository exists.

- Paper: <https://arxiv.org/abs/2509.00078>

Included as a **design reference only** — cannot serve as an open-source replacement.

---

## Coverage Matrix

| Requirement | Wrapyfi | LiveKit Agents | Pipecat | OVOS | OM1 |
|---|:---:|:---:|:---:|:---:|:---:|
| (1) Event bus routing | ✓ | ✓ | ✓ | ✓ | — |
| (2) Structured recording (EMISSOR) | — | — | — | — | — |
| (3) Distributed Docker deployment | ✓ | partial | ✓ | ✓ | partial |
| (4) Parallel simultaneous sessions | — | partial | ✓ | — | — |
| (5) Multi-hardware / research lab | ✓ | — | partial | unverified | partial |

**EMISSOR gap:** No existing open-source framework provides structured multi-modal interaction recording comparable to EMISSOR's session/signal/annotation model. This layer would need to be custom-built regardless of which pipeline framework is chosen.

---

## Recommended Layered Strategy

```
Hardware Layer:   Wrapyfi
                  (middleware abstraction over YARP / ROS 2 / ZeroMQ / MQTT / Zenoh)

Pipeline Layer:   LiveKit Agents  or  Pipecat
                  (VAD → ASR → LLM → TTS, modular + distributed)

Recording Layer:  Custom
                  (no open-source EMISSOR equivalent exists)
```

Wrapyfi + LiveKit Agents is the lowest-risk combination: both are Python-native, actively maintained, and together cover requirements 1, 3, 4, and 5. Requirement 2 (structured recording) remains a gap across the entire landscape.

---

## Open Questions

1. Can Wrapyfi's decorator API be practically combined with LiveKit Agents' pipeline model without architectural friction?
2. What is X-Talk's public availability and license — the architecture matches but no repository has been located?
3. Does any project in the IEMOCAP/MultiWOZ/HRI annotation space provide an EMISSOR-compatible open recording format that could be adopted?
4. How does OM1's Go-based plugin architecture compare in practice to RabbitMQ-style buses for Python-heavy research lab workflows?

---

## Methodology Notes

- **Search angles:** broad robotics middleware, conversational AI pipeline orchestration, distributed multi-session voice agents, interaction data logging formats, HRI lab framework comparison, contrarian limitations analysis
- **Sources fetched:** 23 (after URL deduplication)
- **Claims extracted:** 112 → top 25 verified adversarially
- **Verification:** 3-vote adversarial consensus; 2/3 refutations required to kill a claim
- **Confirmed / killed:** 16 confirmed, 9 killed
- **Key refuted claims:** ROS 2 gateway multi-protocol support (0-3), OVOS robot hardware breadth (0-3), SROS plug-and-play ROS services (0-3), Pipecat as pure pipeline vs event-bus (0-3)
