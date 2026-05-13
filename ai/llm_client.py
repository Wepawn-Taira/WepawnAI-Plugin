"""
HTTP client for local inference: llama.cpp ``llama-server`` (OpenAI chat) or Ollama ``/api/generate``.

Stdlib only (``urllib``). DIAG lines go to stderr for MO2 log forensics.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping, Sequence
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_OLLAMA_GENERATE_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "llama3.2"

_OPENAI_CHAT_PATH = "/v1/chat/completions"

_SYSTEM_PROMPT_TEMPLATE = """{system_game_version_fact}

당신은 스카이림 모딩을 처음 하는 사람도 따라갈 수 있게, 짧고 부드러운 한국어로 안내하는 도우미입니다.

[이 프로필에서 Mod Organizer가 잡아낸 상태 — Nexus와 별개]
{mo2_physical_context}

[공개 Nexus 페이지에서 가져온 글 일부 — 없을 수 있음]
{nexus_context}

--- 출력은 반드시 JSON 한 덩어리만 (앞뒤에 다른 말 금지, 전체를 코드 블록으로 감싸지 마세요) ---
키는 정확히 두 개: "tier", "reason" (둘 다 문자열).

"tier" 값은 반드시 영어 단어 하나만: Red, Yellow, Green (프로그램이 읽습니다).

--- 말투 규칙 (reason 안의 한국어) ---
- "Native DLL", "Loader", "SKSE plugin", "load order" 같은 어려운 영어·시스템 용어는 쓰지 마세요.
- 대신 초등~중학생도 이해할 수 있는 말로 바꿉니다. 예: "사전 모드가 빠져 있으면 게임이 제대로 안 켜지거나 중간에 멈출 수 있어요.", "다른 모드를 먼저 받아야 이 모드가 동작할 수 있어요.", "그림·소리 위주라서 상대적으로 부담이 적은 편이에요."
- 에러 로그처럼 딱딱한 문장 대신, 안내·위로 톤을 유지하세요.

--- 등급 의미 (내부 판단용; reason에서는 쉬운 말로 설명) ---
- Red: 꼭 필요한 다른 모드가 빠졌거나, 게임 실행에 큰 지장이 날 수 있는 경우.
- Yellow: 순서나 다른 모드와의 맞춤을 사용자가 한 번 확인해 보는 게 좋은 경우.
- Green: 위험 요소가 상대적으로 적고, 보통 부담이 적은 경우.

--- reason 작성 (링크·URL 금지) ---
- "reason"에는 순수 한국어 설명만 적습니다. http(s) 주소, 마크다운 링크, HTML, Nexus URL을 절대 넣지 마세요. (검색 링크는 프로그램이 따로 붙입니다.)
- 빠진 사전 모드·플러그인 이름은 글로만 정확히 짚어 주세요. 파일 이름이 있으면 그대로 적어도 됩니다.

--- 그 밖 ---
- 위 컨텍스트(MO2 진단, Nexus 글, 사용자에게 넘어온 목록)를 근거로 답하세요. Nexus 글이 없어도 MO2 진단과 목록만으로 답할 수 있습니다.

(참고) 이 게임의 Nexus 경로 조각은 {nexus_game_domain} 입니다. 답변 본문에는 쓰지 마세요."""


def _build_system_prompt(
    *,
    mo2_physical_context: str = "",
    nexus_context: str = "",
    nexus_game_domain: str = "skyrimspecialedition",
    current_game_version: str = "",
    current_game_name: str = "",
) -> str:
    """Fill system prompt blocks including managed game name + executable version facts."""
    domain = (nexus_game_domain or "").strip().strip("/").lower() or "skyrimspecialedition"
    gn = (current_game_name or "").strip()
    gv = (current_game_version or "").strip()
    if gn and gv:
        system_game_version_fact = (
            f"System Fact: 사용자의 현재 게임은 [{gn}]이며, 실행 파일 버전은 [{gv}]입니다. "
            "모드 호환성 판단의 절대 기준으로 삼으세요."
        )
    elif gn:
        system_game_version_fact = (
            f"System Fact: 사용자의 현재 게임은 [{gn}]입니다. "
            "실행 파일 버전은 확인하지 못했습니다. 모드·도구 호환 안내는 MO2 진단과 목록을 우선하세요."
        )
    elif gv:
        system_game_version_fact = (
            f"System Fact: 실행 파일 버전은 [{gv}]로 확인되었습니다. "
            "게임 이름은 MO2에서 읽지 못했습니다. 호환성 판단 시 이 버전을 기준으로 하세요."
        )
    else:
        system_game_version_fact = (
            "(System Fact: managedGame() 이름 또는 실행 파일 버전을 읽지 못했습니다. "
            "모드 호환 안내는 MO2 진단과 목록만 근거로 조심스럽게 판단하세요.)"
        )
    mc = (mo2_physical_context or "").strip()
    if not mc:
        mo2_block = (
            "(No MO2 physical diagnostics text was provided — treat plugin/master state as unknown from MO2's perspective.)"
        )
        _diag("PROMPT_INJECT mo2_physical_context empty")
    else:
        mo2_block = mc

    nc = (nexus_context or "").strip()
    if not nc:
        nexus_block = (
            "(No Nexus mod page excerpt — e.g. login wall, NSFW gate, HTTP error, timeout, parse failure, "
            "or no Nexus ID. Do not assume mod page text; use MO2 diagnostics above and the user prompt lists.)"
        )
        _diag("PROMPT_INJECT nexus_context empty (Nexus scrape unavailable or failed)")
    else:
        nexus_block = (
            "[Nexus public page excerpt — HTML stripped, truncated; may be incomplete]\n" + nc
        )

    prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        system_game_version_fact=system_game_version_fact,
        mo2_physical_context=mo2_block,
        nexus_context=nexus_block,
        nexus_game_domain=domain,
    )
    _diag(
        f"PROMPT_INJECT game_name={gn!r} game_version={gv!r} mo2_physical raw_len={len(mc)} "
        f"nexus raw_len={len(nc)} nexus_game_domain={domain!r} system_prompt total length={len(prompt)}"
    )
    return prompt


def _diag(msg: str) -> None:
    print(f"[WepawnAI DIAG] {msg}", flush=True)


class LLMConnectionError(OSError):
    """Local LLM server unreachable, timeout, or connection reset."""


class LLMParseError(ValueError):
    """HTTP error body, envelope JSON, or tier schema invalid."""

    def __init__(self, message: str, *, raw_text: str | None = None) -> None:
        super().__init__(message)
        self.raw_text = raw_text


def _build_user_prompt(
    mod_name: str,
    nexus_deps: Sequence[str],
    local_installed_mods: Sequence[str],
) -> str:
    deps_block = "\n".join(f"- {d}" for d in nexus_deps) if nexus_deps else "- (none listed)"
    mods_block = (
        "\n".join(f"- {m}" for m in local_installed_mods)
        if local_installed_mods
        else "- (none listed)"
    )
    return (
        f"Target mod (the one the user is asking about): {mod_name}\n\n"
        f"Nexus-declared dependencies (names / IDs / URLs, as reported by the mod manager):\n"
        f"{deps_block}\n\n"
        f"Currently enabled mods in this Mod Organizer profile (display names, in priority order):\n"
        f"{mods_block}\n\n"
        "Classify this mod into Red, Yellow, or Green and explain your reasoning in Korean in the JSON \"reason\" field."
    )


def _extract_json_object(text: str) -> dict[str, Any]:
    raw_stored = text
    text = text.strip()
    if not text:
        raise LLMParseError("Empty model response", raw_text=raw_stored)
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE)
    if fence:
        text = fence.group(1).strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            snippet = text[start : end + 1]
            try:
                obj = json.loads(snippet)
            except json.JSONDecodeError as exc:
                _diag(f"JSON decode failure on model content slice; dumping raw (truncated) in exception.raw_text")
                raise LLMParseError(
                    f"Model output is not valid JSON: {exc}",
                    raw_text=raw_stored[:12000],
                ) from exc
        else:
            raise LLMParseError(
                "Model output is not valid JSON (no object braces found)",
                raw_text=raw_stored[:12000],
            ) from None
    if not isinstance(obj, dict):
        raise LLMParseError("JSON root must be an object", raw_text=raw_stored[:12000])
    return obj


def _normalize_tier(raw: str, *, content_dump: str | None = None) -> str:
    t = str(raw).strip().lower()
    if t in ("red", "r"):
        return "Red"
    if t in ("yellow", "y"):
        return "Yellow"
    if t in ("green", "g"):
        return "Green"
    raise LLMParseError(
        f'Invalid tier value: {raw!r} (expected Red, Yellow, or Green)',
        raw_text=(content_dump or "")[:12000] or None,
    )


def _finalize_tier_payload(data: dict[str, Any], *, content_dump: str | None = None) -> dict[str, str]:
    tier_raw = data.get("tier")
    reason = data.get("reason")
    if tier_raw is None or reason is None:
        raise LLMParseError(
            "JSON must include 'tier' and 'reason' keys",
            raw_text=(content_dump or json.dumps(data, ensure_ascii=False))[:12000],
        )
    tier = _normalize_tier(str(tier_raw), content_dump=content_dump)
    reason_str = str(reason).strip()
    if not reason_str:
        raise LLMParseError(
            '"reason" must be a non-empty string',
            raw_text=(content_dump or "")[:12000] or None,
        )
    return {"tier": tier, "reason": reason_str}


def analyze_mod_tier(
    mod_name: str,
    nexus_deps: Sequence[str],
    local_installed_mods: Sequence[str],
    *,
    base_url: str,
    request_timeout: float = 90.0,
    nexus_context: str = "",
    mo2_physical_context: str = "",
    nexus_game_domain: str = "skyrimspecialedition",
    current_game_version: str = "",
    current_game_name: str = "",
) -> dict[str, str]:
    """
    POST OpenAI-compatible chat completions to llama.cpp ``llama-server``.

    Endpoint: ``{base_url}/v1/chat/completions``

    Raises:
        LLMConnectionError: socket / timeout / refused.
        LLMParseError: non-200 HTTP, bad envelope, invalid tier JSON (``raw_text`` may hold model output).
    """
    user_prompt = _build_user_prompt(mod_name, nexus_deps, local_installed_mods)
    system_prompt = _build_system_prompt(
        mo2_physical_context=mo2_physical_context,
        nexus_context=nexus_context,
        nexus_game_domain=nexus_game_domain,
        current_game_version=current_game_version,
        current_game_name=current_game_name,
    )
    url = f"{base_url.rstrip('/')}{_OPENAI_CHAT_PATH}"
    payload: dict[str, Any] = {
        "model": "local",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "temperature": 0.15,
        "max_tokens": 1024,
        "stream": False,
    }
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    _diag(f"LLM HTTP POST {url} timeout={request_timeout}s (OpenAI chat completions)")

    raw_resp: str
    status: int
    try:
        with urlopen(request, timeout=request_timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            raw_resp = response.read().decode("utf-8")
    except HTTPError as exc:
        fragment = exc.read().decode("utf-8", errors="replace")[:8000]
        _diag(f"LLM HTTPError status={exc.code} body_prefix={fragment[:400]!r}")
        raise LLMParseError(
            f"HTTP {exc.code} from LLM server at {url!r}",
            raw_text=fragment,
        ) from exc
    except URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        _diag(f"LLM URLError contacting {url!r}: {reason}")
        raise LLMConnectionError(
            f"Cannot reach local LLM at {url!r}: {reason}"
        ) from exc

    if status != 200:
        _diag(f"LLM unexpected status={status} body_prefix={raw_resp[:400]!r}")
        raise LLMParseError(
            f"HTTP {status} from LLM server",
            raw_text=raw_resp[:8000],
        )

    try:
        outer: Mapping[str, Any] = json.loads(raw_resp)
    except json.JSONDecodeError as exc:
        _diag("LLM envelope JSON decode failed; dumping raw response in exception.raw_text")
        raise LLMParseError(
            f"LLM server returned non-JSON envelope: {exc}",
            raw_text=raw_resp[:8000],
        ) from exc

    choices = outer.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMParseError(
            "Chat completion missing non-empty 'choices'",
            raw_text=raw_resp[:8000],
        )

    first = choices[0]
    if not isinstance(first, Mapping):
        raise LLMParseError("Invalid choice[0] shape", raw_text=raw_resp[:8000])

    message = first.get("message")
    if not isinstance(message, Mapping):
        raise LLMParseError("Invalid choice[0].message", raw_text=raw_resp[:8000])

    content = message.get("content")
    if not isinstance(content, str):
        raise LLMParseError(
            "choice[0].message.content is not a string",
            raw_text=raw_resp[:8000],
        )

    content_stripped = content.strip()
    try:
        data = _extract_json_object(content_stripped)
        return _finalize_tier_payload(data, content_dump=content_stripped)
    except LLMParseError:
        raise
    except Exception as exc:
        raise LLMParseError(
            f"Unexpected error parsing model JSON: {exc}",
            raw_text=content_stripped[:12000],
        ) from exc


def _sanitize_chat_history(
    prior: Sequence[Mapping[str, str]] | None,
    *,
    max_messages: int,
) -> list[dict[str, str]]:
    """Keep only user/assistant turns; retain at most ``max_messages`` from the tail."""
    out: list[dict[str, str]] = []
    for m in prior or ():
        role = m.get("role")
        content = m.get("content")
        if role not in ("user", "assistant") or not isinstance(content, str):
            continue
        out.append({"role": str(role), "content": content})
    if max_messages <= 0:
        return []
    if len(out) > max_messages:
        return out[-max_messages:]
    return out


def complete_chat_plain_text(
    *,
    base_url: str,
    system_prompt: str,
    user_prompt: str,
    prior_chat_messages: Sequence[Mapping[str, str]] | None = None,
    max_prior_messages: int = 20,
    request_timeout: float = 120.0,
    max_tokens: int = 2048,
    temperature: float = 0.45,
) -> tuple[str, int]:
    """
    POST OpenAI-compatible chat completions; return assistant ``content`` and wall latency (ms).

    Unlike :func:`analyze_mod_tier`, the model output is treated as plain text (no JSON parse).

    If ``prior_chat_messages`` is set, it is appended after ``system`` and before the final user
    turn (at most ``max_prior_messages`` items, oldest dropped first).
    """
    import time

    url = f"{base_url.rstrip('/')}{_OPENAI_CHAT_PATH}"
    history = _sanitize_chat_history(prior_chat_messages, max_messages=max_prior_messages)
    messages: list[dict[str, str]] = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_prompt})
    payload: dict[str, Any] = {
        "model": "local",
        "messages": messages,
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "stream": False,
    }
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    try:
        with urlopen(request, timeout=request_timeout) as response:
            status = int(getattr(response, "status", None) or response.getcode())
            raw_resp = response.read().decode("utf-8")
    except HTTPError as exc:
        fragment = exc.read().decode("utf-8", errors="replace")[:8000]
        _diag(f"LLM plain HTTPError status={exc.code} body_prefix={fragment[:400]!r}")
        raise LLMParseError(
            f"HTTP {exc.code} from LLM server at {url!r}",
            raw_text=fragment,
        ) from exc
    except URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        _diag(f"LLM plain URLError contacting {url!r}: {reason}")
        raise LLMConnectionError(
            f"Cannot reach local LLM at {url!r}: {reason}"
        ) from exc
    latency_ms = int((time.perf_counter() - t0) * 1000)

    if status != 200:
        _diag(f"LLM plain unexpected status={status} body_prefix={raw_resp[:400]!r}")
        raise LLMParseError(
            f"HTTP {status} from LLM server",
            raw_text=raw_resp[:8000],
        )

    try:
        outer: Mapping[str, Any] = json.loads(raw_resp)
    except json.JSONDecodeError as exc:
        raise LLMParseError(
            f"LLM server returned non-JSON envelope: {exc}",
            raw_text=raw_resp[:8000],
        ) from exc

    choices = outer.get("choices")
    if not isinstance(choices, list) or not choices:
        raise LLMParseError(
            "Chat completion missing non-empty 'choices'",
            raw_text=raw_resp[:8000],
        )
    first = choices[0]
    if not isinstance(first, Mapping):
        raise LLMParseError("Invalid choice[0] shape", raw_text=raw_resp[:8000])
    message = first.get("message")
    if not isinstance(message, Mapping):
        raise LLMParseError("Invalid choice[0].message", raw_text=raw_resp[:8000])
    content = message.get("content")
    if not isinstance(content, str):
        raise LLMParseError(
            "choice[0].message.content is not a string",
            raw_text=raw_resp[:8000],
        )
    return content.strip(), latency_ms


def analyze_mod_tier_ollama(
    mod_name: str,
    nexus_deps: Sequence[str],
    local_installed_mods: Sequence[str],
    *,
    endpoint_url: str = DEFAULT_OLLAMA_GENERATE_URL,
    model: str = DEFAULT_MODEL,
    timeout: float = 120.0,
    nexus_context: str = "",
    mo2_physical_context: str = "",
    nexus_game_domain: str = "skyrimspecialedition",
    current_game_version: str = "",
    current_game_name: str = "",
) -> dict[str, str]:
    """POST to Ollama ``/api/generate`` (optional fallback)."""
    user_prompt = _build_user_prompt(mod_name, nexus_deps, local_installed_mods)
    system_prompt = _build_system_prompt(
        mo2_physical_context=mo2_physical_context,
        nexus_context=nexus_context,
        nexus_game_domain=nexus_game_domain,
        current_game_version=current_game_version,
        current_game_name=current_game_name,
    )
    payload: dict[str, Any] = {
        "model": model,
        "prompt": user_prompt,
        "system": system_prompt,
        "stream": False,
        "format": "json",
    }
    body = json.dumps(payload).encode("utf-8")
    request = Request(
        endpoint_url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    _diag(f"LLM HTTP POST {endpoint_url} timeout={timeout}s (Ollama generate)")

    try:
        with urlopen(request, timeout=timeout) as response:
            raw_resp = response.read().decode("utf-8")
    except HTTPError as exc:
        fragment = exc.read().decode("utf-8", errors="replace")[:2000]
        _diag(f"Ollama HTTPError status={exc.code}")
        raise LLMParseError(
            f"HTTP {exc.code} from Ollama: {fragment}",
            raw_text=fragment,
        ) from exc
    except URLError as exc:
        reason = exc.reason if isinstance(exc.reason, str) else str(exc.reason)
        _diag(f"Ollama URLError: {reason}")
        raise LLMConnectionError(
            f"Cannot reach Ollama at {endpoint_url!r}: {reason}"
        ) from exc

    try:
        outer: Mapping[str, Any] = json.loads(raw_resp)
    except json.JSONDecodeError as exc:
        raise LLMParseError(
            f"Ollama returned non-JSON: {exc}",
            raw_text=raw_resp[:8000],
        ) from exc

    inner_text = outer.get("response")
    if not isinstance(inner_text, str):
        raise LLMParseError(
            "Ollama response missing string 'response' field",
            raw_text=raw_resp[:8000],
        )

    try:
        data = _extract_json_object(inner_text)
        return _finalize_tier_payload(data, content_dump=inner_text)
    except LLMParseError:
        raise
