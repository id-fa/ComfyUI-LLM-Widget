"""A standalone LLM widget node for ComfyUI.

The node asks a local or remote LLM a question, optionally with a connected
image or video as supporting material, and keeps the answer in a widget. It is
meant to run **from the editor**, before (and independently of) any graph
execution, so it is normally left bypassed or muted in the workflow.

The generation backends are the ones ComfyUI-MiniMaxH3-Easy uses for its prompt
optimizer: an OpenAI-compatible chat completions endpoint, Gemini's native
`generateContent`, or a local GGUF loaded through llama-cpp-python. What is
deliberately *not* carried over is the prompt-guide machinery: this node has no
templates, the system prompt is whatever the user types.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import mimetypes
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping, Sequence
from typing import Any

import folder_paths


ROUTE_PREFIX = "/llm_widget"
ANSWER_EVENT = "llm_widget/answer"
CONFIG_FILENAME = "llm_widget.json"
CONFIG_VERSION = 1
LOG_PREFIX = "LLM Widget: "

FORMAT_OPENAI = "openai"
FORMAT_GEMINI = "gemini"
FORMAT_GGUF = "gguf"
FORMATS = (FORMAT_OPENAI, FORMAT_GEMINI, FORMAT_GGUF)

# Ask the model not to reason, and drop any block it emits anyway.
THINKING_OFF = "off"
# Leave the request alone and show whatever the model returns, reasoning
# included. For a model whose reasoning is not tagged, this is the honest
# setting: nothing downstream can tell where the thinking stopped.
THINKING_KEEP = "keep"
THINKING_MODES = (THINKING_OFF, THINKING_KEEP)

HTTP_TIMEOUT_SECONDS = 600
# Freeing a model is quick; a request that hangs here should not sit for the ten
# minutes a generation is allowed.
UNLOAD_TIMEOUT_SECONDS = 60
HTTP_MAX_OUTPUT_TOKENS = 50000
MAX_LENGTH_DEFAULT = 1024
MAX_LENGTH_MIN = 16
MAX_LENGTH_LIMIT = 32768

# The same folders ComfyUI-QwenVL-F and MiniMax H3 Easy scan, so a GGUF
# installed for one of them is offered here too.
GGUF_DIRS = ("text_encoders", "LLM")
GGUF_MMPROJ_AUTO = "auto"
GGUF_MMPROJ_NONE = "none"
GGUF_CONTEXT = 16384
GGUF_CONTEXT_MIN = 512
GGUF_CONTEXT_LIMIT = 1048576
GGUF_GPU_LAYERS = -1

# A chat message has no video channel, so a connected video is described from
# evenly spaced stills instead.
VIDEO_STILLS = 4
STILL_MAX_SIDE = 768
# How many frames of a connected IMAGE batch are attached during execution.
IMAGE_BATCH_STILLS = 4
MEDIA_MAX_BYTES = 32 * 1024 * 1024
CANCEL_LIMIT = 64

DESCRIBE_LENGTH = 256
DESCRIBE_SYSTEM = (
    "You describe one piece of media so another model can reason about it from your words alone.\n"
    "Report only what is actually present: subject, appearance, clothing, pose, setting, lighting, "
    "colour, on-screen text and visual style; for video also motion, action and camera movement.\n"
    "Never guess, never invent, and never describe media you cannot perceive.\n"
    "Answer with one dense factual paragraph of at most 100 words. No headings, no lists, no commentary."
)
DESCRIBE_REQUESTS = {
    "image": "Describe this image.",
    "video": "Describe this video.",
}
VIDEO_STILLS_REQUEST = (
    "Describe this video. The {count} attached images are frames sampled from it in chronological "
    "order, not separate pictures: describe the clip as a whole, including the action and camera "
    "movement the frames show."
)

DEFAULT_SYSTEM_PROMPT = (
    "You are a helpful assistant.\n"
    "Reply with the answer only: no preamble, no explanation, no commentary."
)

CONFIG_DEFAULTS = {
    "version": CONFIG_VERSION,
    "api_format": FORMAT_OPENAI,
    "api_url": "",
    "api_key": "",
    "model": "",
    "read_media": True,
    "thinking": THINKING_OFF,
    "temperature": 0.35,
    "max_length": MAX_LENGTH_DEFAULT,
    "gguf_model": "",
    "gguf_mmproj": GGUF_MMPROJ_AUTO,
    "gguf_context": GGUF_CONTEXT,
    "gguf_gpu_layers": GGUF_GPU_LAYERS,
    "gguf_unload_after": False,
    "gguf_describe_media": False,
}

_CONFIG_LOCK = threading.RLock()


def _log(message: str, *args: Any) -> None:
    """Progress lines for a step that can take minutes and shows little UI."""
    logging.info(LOG_PREFIX + message, *args)


class _Cancelled(Exception):
    """The editor asked for this generation to stop."""


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #


def _config_path() -> str:
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), CONFIG_FILENAME)


def _as_bool(value: Any, fallback: bool = False) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return fallback if value is None else bool(value)


def _normalize_config(value: Mapping[str, Any] | None) -> dict[str, Any]:
    source = value if isinstance(value, Mapping) else {}
    api_format = str(source.get("api_format") or FORMAT_OPENAI).strip().lower()
    if api_format not in FORMATS:
        api_format = FORMAT_OPENAI

    def integer(key: str, fallback: int, low: int, high: int) -> int:
        try:
            number = int(float(source.get(key, fallback)))
        except (TypeError, ValueError):
            number = fallback
        return min(high, max(low, number))

    try:
        temperature = float(source.get("temperature", CONFIG_DEFAULTS["temperature"]))
    except (TypeError, ValueError):
        temperature = float(CONFIG_DEFAULTS["temperature"])
    temperature = min(2.0, max(0.0, temperature))

    thinking = str(source.get("thinking") or THINKING_OFF).strip().lower()
    if thinking not in THINKING_MODES:
        thinking = THINKING_OFF
    mmproj = str(source.get("gguf_mmproj") or GGUF_MMPROJ_AUTO).strip() or GGUF_MMPROJ_AUTO
    return {
        "version": CONFIG_VERSION,
        "api_format": api_format,
        "api_url": str(source.get("api_url") or "").strip(),
        "api_key": str(source.get("api_key") or ""),
        "model": str(source.get("model") or "").strip(),
        "read_media": _as_bool(source.get("read_media"), True),
        "thinking": thinking,
        "temperature": round(temperature, 3),
        "max_length": integer("max_length", MAX_LENGTH_DEFAULT, MAX_LENGTH_MIN, MAX_LENGTH_LIMIT),
        "gguf_model": str(source.get("gguf_model") or "").strip(),
        "gguf_mmproj": mmproj,
        "gguf_context": integer("gguf_context", GGUF_CONTEXT, GGUF_CONTEXT_MIN, GGUF_CONTEXT_LIMIT),
        "gguf_gpu_layers": integer("gguf_gpu_layers", GGUF_GPU_LAYERS, -1, 1024),
        "gguf_unload_after": _as_bool(source.get("gguf_unload_after"), False),
        "gguf_describe_media": _as_bool(source.get("gguf_describe_media"), False),
    }


def _read_config() -> dict[str, Any]:
    path = _config_path()
    with _CONFIG_LOCK:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, ValueError):
            return dict(CONFIG_DEFAULTS)
    return _normalize_config(payload)


def _write_config(value: Mapping[str, Any] | None) -> dict[str, Any]:
    normalized = _normalize_config(value)
    path = _config_path()
    temporary_path = ""
    with _CONFIG_LOCK:
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=os.path.dirname(path),
                prefix=f".{CONFIG_FILENAME}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = handle.name
                json.dump(normalized, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
            os.replace(temporary_path, path)
        finally:
            if temporary_path and os.path.exists(temporary_path):
                try:
                    os.remove(temporary_path)
                except OSError:
                    pass
    return normalized


# --------------------------------------------------------------------------- #
# URL handling
# --------------------------------------------------------------------------- #

_KNOWN_ENDPOINT_SUFFIXES = ("/v1/chat/completions", "/chat/completions")
_GEMINI_ENDPOINT_RE = re.compile(
    r"/(v1beta|v1)/models/[^/?:#]+?:(generateContent|streamGenerateContent)$",
    flags=re.I,
)


def _normalize_base_url(api_url: str) -> str:
    base = str(api_url or "").strip().rstrip("/")
    if not base:
        raise ValueError("API URL is required")
    if not re.match(r"^https?://", base, flags=re.I):
        base = "https://" + base
    return base.rstrip("/")


def _normalize_gemini_model_id(model: str) -> str:
    """Accept a bare model ID, ``models/<id>``, or a full Gemini model URL."""
    raw = urllib.parse.unquote(str(model or "").strip())
    if not raw:
        raise ValueError("Model is required")
    if "://" in raw:
        raw = urllib.parse.urlsplit(raw).path
    raw = raw.split("?", 1)[0].split("#", 1)[0].strip().strip("/")
    match = re.search(r"(?:^|/)models/([^/:]+)(?::[A-Za-z]+)?$", raw, flags=re.I)
    if match:
        raw = match.group(1)
    else:
        if raw.lower().startswith("models/"):
            raw = raw[7:]
        raw = raw.rsplit("/", 1)[-1]
        raw = re.sub(r":(?:generateContent|streamGenerateContent)$", "", raw, flags=re.I)
    raw = raw.strip()
    if not raw:
        raise ValueError("Model is required")
    return raw


def _gemini_url_with_query(url: str, query: str) -> str:
    # ``alt=sse`` belongs to streamGenerateContent and would corrupt the JSON
    # response expected from generateContent. Preserve other proxy parameters.
    pairs = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(query, keep_blank_values=True)
        if key.lower() != "alt"
    ]
    encoded = urllib.parse.urlencode(pairs)
    return url + (f"?{encoded}" if encoded else "")


def _normalize_gemini_url(api_url: str, model: str) -> str:
    base = _normalize_base_url(api_url)
    parsed = urllib.parse.urlsplit(base)
    clean = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path.rstrip("/"), "", ""))
    lower = clean.lower()
    model_id = urllib.parse.quote(_normalize_gemini_model_id(model), safe=".-_")

    endpoint_match = _GEMINI_ENDPOINT_RE.search(lower)
    if endpoint_match and lower.endswith(endpoint_match.group(0)):
        version = endpoint_match.group(1)
        clean = clean[: endpoint_match.start()].rstrip("/")
        return _gemini_url_with_query(f"{clean}/{version}/models/{model_id}:generateContent", parsed.query)

    if lower.endswith("/v1beta/models") or lower.endswith("/v1/models") or lower.endswith("/models"):
        url = f"{clean}/{model_id}:generateContent"
    elif lower.endswith("/v1beta") or lower.endswith("/v1"):
        url = f"{clean}/models/{model_id}:generateContent"
    else:
        url = f"{clean}/v1beta/models/{model_id}:generateContent"
    return _gemini_url_with_query(url, parsed.query)


def _strip_known_endpoint(base: str) -> str:
    lower = base.lower()
    for suffix in _KNOWN_ENDPOINT_SUFFIXES:
        if lower.endswith(suffix):
            return base[: len(base) - len(suffix)].rstrip("/")
    match = _GEMINI_ENDPOINT_RE.search(lower)
    if match and lower.endswith(match.group(0)):
        return base[: match.start()].rstrip("/")
    return base


def _normalize_url(api_url: str, api_format: str, model: str) -> str:
    """Turn a forgiving user-typed URL into the endpoint the format needs."""
    if api_format == FORMAT_GEMINI:
        return _normalize_gemini_url(api_url, model)
    base = _normalize_base_url(api_url)
    if base.lower().endswith("/chat/completions"):
        return base
    base = _strip_known_endpoint(base)
    endpoint = "/v1/chat/completions"
    if base.lower().endswith("/v1"):
        endpoint = endpoint[3:]
    elif base.lower().endswith("/v1beta"):
        endpoint = "/chat/completions"
    return base + endpoint


# --------------------------------------------------------------------------- #
# Cancellation
# --------------------------------------------------------------------------- #

_CANCEL_LOCK = threading.RLock()
_CANCELLED: list[str] = []


def _cancel(request_id: str) -> bool:
    """Mark an in-flight generation as cancelled.

    The id list is capped because a cancel that arrives after its request has
    already finished has nothing left to clear it.
    """
    request_id = str(request_id or "").strip()
    if not request_id:
        return False
    with _CANCEL_LOCK:
        if request_id not in _CANCELLED:
            _CANCELLED.append(request_id)
        del _CANCELLED[:-CANCEL_LIMIT]
    return True


def _is_cancelled(request_id: str) -> bool:
    request_id = str(request_id or "").strip()
    if not request_id:
        return False
    with _CANCEL_LOCK:
        return request_id in _CANCELLED


def _forget_cancel(request_id: str) -> None:
    request_id = str(request_id or "").strip()
    with _CANCEL_LOCK:
        while request_id in _CANCELLED:
            _CANCELLED.remove(request_id)


def _raise_if_cancelled(request_id: str) -> None:
    if _is_cancelled(request_id):
        raise _Cancelled("Generation was cancelled")


# --------------------------------------------------------------------------- #
# Output cleanup
# --------------------------------------------------------------------------- #

_THINK_TAGS = "think|thinking|reasoning|thought|analysis"
# Greedy on purpose: everything up to and including the LAST closing tag goes.
# The opening tag is optional because most Qwen-family chat templates *pre-open*
# `<think>` in the assistant turn, so what the model actually returns starts
# with bare reasoning prose and the first tag in the string is the closing one.
# Requiring the opening tag let that whole reasoning block through.
_THINK_CLOSE_RE = re.compile(rf"\A.*</(?:{_THINK_TAGS})>", flags=re.I | re.S)
_OPEN_THINK_RE = re.compile(rf"\A\s*<(?:{_THINK_TAGS})>", flags=re.I)
# Channel markers instead of tags. gpt-oss writes Harmony's `<|channel|>`, but the
# pipes move: Gemma 4 opens with `<|channel>thought` and closes with `<channel|>`,
# so every spelling of the same marker has to be accepted.
_CHANNEL_MARK = r"<\|?channel\|?>"
_MESSAGE_MARK = r"<\|?message\|?>"
# Both are greedy: the answer is whatever follows the LAST marker.
_FINAL_CHANNEL_RE = re.compile(rf"\A.*{_CHANNEL_MARK}\s*final\s*{_MESSAGE_MARK}", flags=re.I | re.S)
# A thinking channel that is closed by a bare marker, with no `final` role and no
# `<|message|>` after it — the answer simply starts there.
_THOUGHT_CHANNEL_RE = re.compile(
    rf"\A.*{_CHANNEL_MARK}\s*(?:{_THINK_TAGS})\b.*{_CHANNEL_MARK}\s*(?:{_MESSAGE_MARK})?",
    flags=re.I | re.S,
)
_OPEN_CHANNEL_RE = re.compile(rf"\A\s*{_CHANNEL_MARK}\s*(?:{_THINK_TAGS})\b", flags=re.I)
_TRAILING_TOKEN_RE = re.compile(r"<\|(?:return|end|endoftext|im_end)\|>\s*\Z", flags=re.I)
_WHOLE_FENCE_RE = re.compile(r"\A```([A-Za-z0-9_+.-]*)[ \t]*\r?\n(.*?)\r?\n?```\Z", flags=re.S)
# Fences that only wrap prose are unwrapped; a ```python block is the answer
# itself and is left exactly as the model wrote it.
_PLAIN_FENCE_LANGS = {"", "text", "txt", "plain", "plaintext", "prompt", "output"}


def _clean_output(text: Any, strip_thinking: bool = True) -> str:
    """Reduce a model's answer to the answer itself.

    Reasoning models emit a thinking block before the answer and it must never
    reach the widget. Thinking is switched off per backend where that is
    possible; this is the backstop for models that ignore it, or whose template
    opens the block for them so only its closing tag is ever emitted.

    Untagged reasoning cannot be removed here — nothing marks where it ends —
    so the switches in the request are what actually has to work.
    """
    value = str(text or "").strip()
    if strip_thinking:
        value = _FINAL_CHANNEL_RE.sub("", value).strip()
        value = _THOUGHT_CHANNEL_RE.sub("", value).strip()
        value = _TRAILING_TOKEN_RE.sub("", value).strip()
        value = _THINK_CLOSE_RE.sub("", value).strip()
        if _OPEN_THINK_RE.match(value) or _OPEN_CHANNEL_RE.match(value):
            # An unterminated block means the answer was cut off mid-thought,
            # so there is no answer in here at all.
            return ""
    match = _WHOLE_FENCE_RE.match(value)
    if match and match.group(1).lower() in _PLAIN_FENCE_LANGS:
        value = match.group(2)
    return value.strip()


# --------------------------------------------------------------------------- #
# Media
# --------------------------------------------------------------------------- #


def _asset_path(asset: Mapping[str, Any]) -> str | None:
    """Resolve an editor-reported asset to a file inside ComfyUI's own folders."""
    filename = str(asset.get("filename") or "").strip()
    if not filename or os.path.isabs(filename):
        return None
    storage = str(asset.get("storage") or "input").lower()
    roots = {
        "input": folder_paths.get_input_directory(),
        "output": folder_paths.get_output_directory(),
        "temp": folder_paths.get_temp_directory(),
    }
    root = os.path.realpath(roots.get(storage, roots["input"]))
    subfolder = str(asset.get("subfolder") or "").replace("\\", "/").strip("/")
    candidate = os.path.realpath(os.path.join(root, subfolder, filename))
    if candidate != root and not candidate.startswith(root + os.sep):
        return None
    return candidate if os.path.isfile(candidate) else None


def _image_part(data: bytes, mime: str, api_format: str) -> dict[str, Any]:
    encoded = base64.b64encode(data).decode("ascii")
    if api_format == FORMAT_GEMINI:
        return {"inlineData": {"mimeType": mime, "data": encoded}}
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{encoded}"}}


def _encode_pil(image, max_side: int = STILL_MAX_SIDE) -> bytes:
    longest = max(image.width, image.height)
    if longest > max_side:
        scale = max_side / longest
        image = image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))))
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=88)
    return buffer.getvalue()


def _video_still_parts(path: str, api_format: str, count: int = VIDEO_STILLS) -> list[dict[str, Any]]:
    """Sample evenly spaced frames from a video file as image parts.

    A chat completion has no video channel, so this is the only way a connected
    video reaches an OpenAI-compatible API or llama-cpp. Frames are seeked
    rather than decoded in full, since a clip can be long and only a handful of
    stills are wanted; files whose duration is unknown (or that refuse to seek)
    fall back to a strided sequential decode.
    """
    if not path:
        return []
    try:
        import av
    except ImportError:
        logging.warning(LOG_PREFIX + "PyAV is unavailable, so videos cannot be sampled.")
        return []
    frames: list[Any] = []
    try:
        with av.open(path) as container:
            streams = container.streams.video
            if not streams:
                return []
            stream = streams[0]
            stream.thread_type = "AUTO"
            duration = 0.0
            if stream.duration and stream.time_base:
                duration = float(stream.duration * stream.time_base)
            elif container.duration:
                duration = float(container.duration) / float(av.time_base)
            if duration > 0 and stream.time_base:
                for index in range(count):
                    # Aim at the middle of each slice so the first and last
                    # frames (often black) are not what gets described.
                    seconds = duration * (index + 0.5) / count
                    try:
                        container.seek(int(seconds / stream.time_base), stream=stream)
                        frame = next(container.decode(stream), None)
                    except Exception:
                        frame = None
                    if frame is not None:
                        frames.append(frame)
            if not frames:
                container.seek(0)
                total = int(stream.frames or 0)
                stride = max(1, total // count) if total else 1
                for index, frame in enumerate(container.decode(stream)):
                    if index % stride == 0:
                        frames.append(frame)
                    if len(frames) >= count:
                        break
    except Exception as exc:
        logging.warning(LOG_PREFIX + "could not sample %s (%s).", os.path.basename(path), exc)
        return []
    parts: list[dict[str, Any]] = []
    for frame in frames:
        try:
            parts.append(_image_part(_encode_pil(frame.to_image()), "image/jpeg", api_format))
        except Exception as exc:
            logging.warning(LOG_PREFIX + "could not encode a sampled frame (%s).", exc)
    if parts:
        _log("sampled %d frames from %s", len(parts), os.path.basename(path))
    return parts


def _tensor_still_parts(frames, api_format: str, count: int) -> list[dict[str, Any]]:
    """Encode evenly spaced frames of an IMAGE/VIDEO tensor as image parts.

    Used only during execution, where media arrives as tensors instead of the
    file paths the editor route reports.
    """
    try:
        import numpy
        from PIL import Image
    except ImportError:
        return []
    try:
        total = int(frames.shape[0])
    except (AttributeError, IndexError, TypeError):
        return []
    if total <= 0:
        return []
    indexes = [min(total - 1, int(index * total / max(1, min(count, total)))) for index in range(min(count, total))]
    parts: list[dict[str, Any]] = []
    for index in indexes:
        try:
            array = frames[index][..., :3].detach().cpu().numpy()
            image = Image.fromarray(numpy.clip(array * 255.0, 0, 255).astype(numpy.uint8))
            parts.append(_image_part(_encode_pil(image), "image/jpeg", api_format))
        except Exception as exc:
            logging.warning(LOG_PREFIX + "could not encode a frame (%s).", exc)
    return parts


def _media_items(resources: Sequence[Mapping[str, Any]], api_format: str) -> list[dict[str, Any]]:
    """Resolve the editor's media list into taggable request parts.

    Every readable asset is kept, including the ones this format cannot carry:
    its `parts` is then empty so a caller can say what it skipped instead of
    quietly dropping the media. A video that cannot be sent whole becomes
    `sampled` stills, which is the only way it reaches a chat API at all.
    """
    items: list[dict[str, Any]] = []
    ordinals: dict[str, int] = {}
    for resource in resources:
        if not isinstance(resource, Mapping):
            continue
        raw_asset = resource.get("asset")
        asset: Mapping[str, Any] = raw_asset if isinstance(raw_asset, Mapping) else {}
        media_type = str(resource.get("type") or "").lower()
        if media_type not in {"image", "video"}:
            continue
        path = _asset_path(asset)
        ordinals[media_type] = ordinals.get(media_type, 0) + 1
        tag = str(resource.get("tag") or "").strip() or f"{media_type} {ordinals[media_type]}"
        parts: list[dict[str, Any]] = []
        if not path:
            # Connected but not a file on disk (a mid-graph tensor, an unknown
            # loader). Kept so the caller can report the skip instead of
            # letting the model answer about media it never received.
            items.append({"tag": tag, "type": media_type, "path": "", "parts": [], "sampled": False})
            continue
        try:
            # Only whole-file embedding is size-capped. The item survives either
            # way, so an oversized video can still be sampled into stills.
            if os.path.getsize(path) <= MEDIA_MAX_BYTES:
                mime = mimetypes.guess_type(path)[0] or {"image": "image/jpeg", "video": "video/mp4"}[media_type]
                if api_format == FORMAT_GEMINI or media_type == "image":
                    with open(path, "rb") as handle:
                        parts = [_image_part(handle.read(), mime, api_format)]
        except (OSError, ValueError):
            parts = []
        sampled = not parts and media_type == "video"
        if sampled:
            parts = _video_still_parts(path, api_format)
            sampled = bool(parts)
        items.append({"tag": tag, "type": media_type, "path": path, "parts": parts, "sampled": sampled})
    return items


def _media_manifest(items: Sequence[Mapping[str, Any]]) -> str:
    """Name what each attached part actually is.

    Without this the model receives an unlabelled pile of images and has to
    guess which input each one belongs to — and a sampled video looks like
    several unrelated pictures rather than one clip.
    """
    lines = []
    for item in items:
        parts = item.get("parts") or []
        if not parts:
            continue
        if item.get("sampled"):
            lines.append(
                f"- {item['tag']}: {len(parts)} still frames sampled in chronological order from one "
                "video clip. They are that single video, not separate images."
            )
        elif len(parts) > 1:
            lines.append(f"- {item['tag']}: {len(parts)} images from one batch")
        else:
            lines.append(f"- {item['tag']}: 1 {item['type']}")
    if not lines:
        return ""
    return "Attached media parts, in order:\n" + "\n".join(lines)


def _system_prompt(base: str, attached: Sequence[Mapping[str, Any]], described_count: int) -> str:
    """The user's system prompt plus a rule about the media, when there is any.

    A pure text task (translating, rewriting) gets the system prompt untouched;
    only a request that carries media needs to be told what it may claim to see.
    """
    text = str(base or "").strip()
    if described_count:
        # The media itself was perceived in a separate pass; this request only
        # carries the resulting descriptions.
        rule = (
            f"Descriptions of {described_count} connected media are listed under CONNECTED MEDIA in the "
            "user message. You produced them yourself from the actual media, so treat them as observed "
            "evidence. Do not invent any detail beyond those descriptions and the user's question."
        )
    elif attached:
        manifest = _media_manifest(attached)
        rule = (
            f"Media connected to this request: {len(attached)}.\n"
            + (f"{manifest}\n" if manifest else "")
            + "The presence of a media part does not prove that you can perceive it. Use visual details "
            "only when they are directly observable to you in the attached parts. If your model cannot "
            "read that modality, say so instead of inventing a subject, appearance, action or setting."
        )
    else:
        return text
    return (text + "\n\n" if text else "") + "=== MEDIA RULE ===\n" + rule


# --------------------------------------------------------------------------- #
# HTTP backends
# --------------------------------------------------------------------------- #


def _thinking_off_payload(api_format: str) -> dict[str, Any]:
    """The extra request fields that switch a reasoning model's thinking off.

    There is no standard field for this. `chat_template_kwargs.enable_thinking`
    is what llama.cpp's server, vLLM, SGLang, LM Studio and Ollama all read for
    the Qwen family, and Gemini uses a zero thinking budget. Both are sent as a
    *separate* dict so the request can be retried without them: an endpoint that
    rejects unknown fields (OpenAI itself does) must still work.
    """
    if api_format == FORMAT_GEMINI:
        return {"generationConfig": {"thinkingConfig": {"thinkingBudget": 0}}}
    return {"chat_template_kwargs": {"enable_thinking": False}}


def _merge_payload(base: Mapping[str, Any], extra: Mapping[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in extra.items():
        if isinstance(value, Mapping) and isinstance(merged.get(key), Mapping):
            merged[key] = {**merged[key], **value}
        else:
            merged[key] = value
    return merged


def _http_post(
    url: str,
    headers: Mapping[str, str],
    payload: Mapping[str, Any],
    extra: Mapping[str, Any],
) -> Any:
    """POST the payload, retrying without `extra` if the endpoint rejects it.

    The thinking switches are the only optional fields, and a server that has
    never heard of them answers 400 rather than ignoring them. Dropping them and
    trying again is better than failing outright — the output cleanup is still
    there to catch a tagged reasoning block.
    """
    attempts: list[Mapping[str, Any]] = [_merge_payload(payload, extra)] if extra else []
    attempts.append(payload)
    last: Exception | None = None
    for index, body in enumerate(attempts):
        request = urllib.request.Request(
            url, data=json.dumps(body, ensure_ascii=False).encode("utf-8"), headers=dict(headers), method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            last = RuntimeError(f"API error ({exc.code}): {detail[:1000]}")
            if index + 1 < len(attempts) and exc.code in {400, 404, 422}:
                _log("the endpoint rejected the thinking-off fields (%d); retrying without them", exc.code)
                continue
            raise last from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Request failed: {exc.reason}") from exc
    raise last or RuntimeError("Request failed")


def _server_root(api_url: str) -> str:
    """The server root an OpenAI-compatible URL hangs off.

    A local server's own management API sits *beside* the OpenAI-compatible one
    — under `/api/v1` for LM Studio, `/api` for Ollama — so the
    `/v1/chat/completions` part has to come off first. Only the API path is
    removed, not the whole path: a reverse proxy that serves the server under a
    prefix keeps it.
    """
    base = _strip_known_endpoint(_normalize_base_url(api_url))
    lower = base.lower()
    for suffix in ("/api/v1", "/api/v0", "/v1beta", "/v1"):
        if lower.endswith(suffix):
            return base[: -len(suffix)].rstrip("/")
    return base


def _api_request(url: str, headers: Mapping[str, str], payload: Any = None, method: str = "GET") -> Any:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=data, headers=dict(headers), method=method)
    with urllib.request.urlopen(request, timeout=UNLOAD_TIMEOUT_SECONDS) as response:
        body = response.read().decode("utf-8", errors="replace")
    return json.loads(body) if body.strip() else {}


def _lmstudio_instances(root: str, headers: Mapping[str, str]) -> list[tuple[str, str]] | None:
    """`(model key, instance id)` for everything the server has resident.

    None means the question could not be asked — an older LM Studio, a llama.cpp
    server, a cloud endpoint. The caller then tries the unload blind rather than
    giving up, because the error from that call is the informative one.
    """
    try:
        data = _api_request(f"{root}/api/v1/models", headers)
    except Exception:
        return None
    models = data.get("models") if isinstance(data, Mapping) else None
    if not isinstance(models, list):
        return None
    loaded: list[tuple[str, str]] = []
    for entry in models:
        if not isinstance(entry, Mapping):
            continue
        key = str(entry.get("key") or entry.get("id") or "")
        for instance in entry.get("loaded_instances") or []:
            if isinstance(instance, Mapping) and instance.get("id"):
                loaded.append((key, str(instance["id"])))
    return loaded


def _ollama_running(root: str, headers: Mapping[str, str]) -> list[str] | None:
    """The model names Ollama currently has in memory, or None if that is not it.

    `/api/ps` is Ollama's and `/api/v1/models` is LM Studio's; neither server has
    the other's route, so which one answers *is* the detection. Both are plain
    reads, so probing costs nothing but a local round trip.
    """
    try:
        data = _api_request(f"{root}/api/ps", headers)
    except Exception:
        return None
    models = data.get("models") if isinstance(data, Mapping) else None
    if not isinstance(models, list):
        return None
    return [
        str(entry.get("model") or entry.get("name"))
        for entry in models
        if isinstance(entry, Mapping) and (entry.get("model") or entry.get("name"))
    ]


def _same_model(configured: str, name: str) -> bool:
    """Whether a resident model is the configured one.

    The tag is optional on both sides: Ollama reports `llama3.2:latest` for what
    the settings call `llama3.2`, and LM Studio gives a second instance of a
    model the id `key:2`.
    """
    left = configured.strip().lower()
    right = str(name).strip().lower()
    if not left or not right:
        return False
    return left == right or right.startswith(f"{left}:") or left.startswith(f"{right}:")


def _not_loaded(model: str, resident: Sequence[str]) -> tuple[int, str]:
    listed = ", ".join(sorted({str(name) for name in resident if name}))
    return 0, f"{model} is not loaded" + (f" (loaded: {listed})" if listed else "")


def _remote_unload(settings: Mapping[str, Any]) -> tuple[int, str]:
    """Unload the configured model from whichever local server is behind the URL.

    Only local servers have anything to free, and the two that people actually
    run this against say so in their own way: LM Studio through
    `/api/v1/models/unload`, Ollama by asking for a generation with
    `keep_alive: 0`. Which is which is answered by the listing probes rather than
    by a setting — the two backends are the same OpenAI-compatible endpoint for
    every other purpose, and making the user declare the vendor for one button is
    not worth a third API format.
    """
    model = str(settings.get("model") or "").strip()
    root = _server_root(str(settings.get("api_url") or ""))
    api_key = str(settings.get("api_key") or "")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    if not model:
        raise ValueError("Set the model in the settings first")

    instances = _lmstudio_instances(root, headers)
    if instances is not None:
        targets = [
            instance for key, instance in instances
            if _same_model(model, instance) or _same_model(model, key)
        ]
        if not targets:
            return _not_loaded(model, [instance for _, instance in instances])
        return _unload_each(
            root, headers, targets,
            lambda instance: (f"{root}/api/v1/models/unload", {"instance_id": instance}),
        ), ""

    running = _ollama_running(root, headers)
    if running is not None:
        targets = [name for name in running if _same_model(model, name)]
        if not targets:
            return _not_loaded(model, running)
        # An empty prompt with keep_alive 0 is how Ollama is told to drop a model;
        # there is no dedicated route for it.
        return _unload_each(
            root, headers, targets,
            lambda name: (f"{root}/api/generate", {"model": name, "keep_alive": 0}),
        ), ""

    raise RuntimeError(
        "This server has no unload endpoint. Freeing a model is LM Studio's "
        "(/api/v1/models/unload) or Ollama's (keep_alive 0); neither answered at "
        f"{root}, and plain OpenAI-compatible servers have nothing of the kind."
    )


def _unload_each(root: str, headers: Mapping[str, str], targets: Sequence[str], build) -> int:
    unloaded = 0
    for target in targets:
        url, payload = build(target)
        try:
            _api_request(url, headers, payload, method="POST")
        except urllib.error.HTTPError as exc:
            # The server listed this model moments ago, so a 404 now means it is
            # already gone — which is the wanted end state either way.
            if exc.code == 404:
                continue
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Unload failed ({exc.code}): {detail[:500]}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Request failed: {exc.reason}") from exc
        _log("unloaded %s from %s", target, root)
        unloaded += 1
    return unloaded


def _http_generate(
    settings: Mapping[str, Any],
    system_prompt: str,
    user_prompt: str,
    media_parts: Sequence[Mapping[str, Any]] | None = None,
) -> str:
    api_format = str(settings.get("api_format") or FORMAT_OPENAI)
    api_url = str(settings.get("api_url") or "")
    api_key = str(settings.get("api_key") or "")
    model = str(settings.get("model") or "")
    temperature = float(settings.get("temperature", CONFIG_DEFAULTS["temperature"]))
    no_think = str(settings.get("thinking") or THINKING_OFF).lower() != THINKING_KEEP
    url = _normalize_url(api_url, api_format, model)
    media_parts = list(media_parts or [])
    started = time.perf_counter()
    _log(
        "asking %s (model=%s, system=%d chars, question=%d chars, media parts=%d, thinking=%s)",
        api_format, model or "?", len(system_prompt), len(user_prompt), len(media_parts),
        "off" if no_think else "kept",
    )
    if api_format == FORMAT_GEMINI:
        headers = {"Content-Type": "application/json", "Accept": "application/json", "x-goog-api-key": api_key}
        # Some Gemini-compatible channels accept the native payload and return
        # candidates but silently ignore `systemInstruction`, which would drop
        # the user's system prompt without any error. Keeping both in the same
        # user text part is the shape that works everywhere.
        text = (
            f"{system_prompt}\n\n=== QUESTION ===\n{user_prompt}"
            if system_prompt.strip() else user_prompt
        )
        payload: dict[str, Any] = {
            "contents": [{"role": "user", "parts": [{"text": text}, *media_parts]}],
            "generationConfig": {"temperature": temperature, "maxOutputTokens": HTTP_MAX_OUTPUT_TOKENS},
        }
    else:
        headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
        content: Any = [{"type": "text", "text": user_prompt}, *media_parts] if media_parts else user_prompt
        messages: list[dict[str, Any]] = []
        if system_prompt.strip():
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content})
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "temperature": temperature,
            "max_tokens": HTTP_MAX_OUTPUT_TOKENS,
        }
    data = _http_post(url, headers, payload, _thinking_off_payload(api_format) if no_think else {})
    if api_format == FORMAT_GEMINI:
        candidates = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(candidates, list) or not candidates:
            feedback = data.get("promptFeedback") if isinstance(data, dict) else None
            reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
            raise RuntimeError("Gemini API returned no candidates" + (f": {reason}" if reason else ""))
        candidate = candidates[0] if isinstance(candidates[0], dict) else {}
        parts = candidate.get("content", {}).get("parts", []) if isinstance(candidate.get("content"), dict) else []
        text = "".join(str(part.get("text", "")) for part in parts if isinstance(part, dict) and part.get("text") is not None)
        if not text.strip():
            finish = candidate.get("finishReason") or candidate.get("finish_reason") or "unknown"
            raise RuntimeError(f"Gemini API returned no text (finish reason: {finish})")
    else:
        message = ((data.get("choices") or [{}])[0].get("message", {}) or {}) if isinstance(data, dict) else {}
        content = message.get("content", "")
        text = content if isinstance(content, str) else "".join(
            str(item.get("text", "")) for item in (content or []) if isinstance(item, dict)
        )
    text = _clean_output(text, no_think)
    if not text:
        raise RuntimeError("The API returned an empty response")
    _log("finished in %.1fs (%d chars)", time.perf_counter() - started, len(text))
    return text


# --------------------------------------------------------------------------- #
# GGUF backend (llama-cpp-python)
# --------------------------------------------------------------------------- #


def _category_paths(category: str) -> list[str]:
    try:
        entry = folder_paths.folder_names_and_paths.get(category)
        if not entry:
            return []
        paths = entry[0]
        if isinstance(paths, (str, os.PathLike)):
            paths = [paths]
        return [os.fspath(path) for path in paths]
    except Exception:
        return []


def _gguf_roots() -> list[str]:
    roots: list[str] = []
    for name in GGUF_DIRS:
        for path in _category_paths(name):
            if path and path not in roots:
                roots.append(path)
    return roots


def _is_mmproj_name(name: str) -> bool:
    return "mmproj" in os.path.basename(str(name or "")).lower()


def _gguf_catalog() -> tuple[dict[str, str], dict[str, str]]:
    """Every .gguf under the text encoder / LLM folders, keyed by relative path."""
    models: dict[str, str] = {}
    projectors: dict[str, str] = {}
    for root in _gguf_roots():
        for directory, _dirs, filenames in os.walk(root):
            for filename in filenames:
                if not filename.lower().endswith(".gguf"):
                    continue
                full = os.path.join(directory, filename)
                key = os.path.relpath(full, root).replace(os.sep, "/")
                target = projectors if _is_mmproj_name(filename) else models
                target.setdefault(key, full)
    return models, projectors


def _gguf_lookup(catalog: Mapping[str, str], name: str) -> str:
    wanted = str(name or "").strip().replace("\\", "/")
    if not wanted:
        return ""
    if wanted in catalog:
        return catalog[wanted]
    lowered = wanted.lower()
    for key, path in catalog.items():
        if key.lower() == lowered or os.path.basename(key).lower() == lowered:
            return path
    return ""


def _gguf_mmproj_path(model_path: str, requested: str, projectors: Mapping[str, str]) -> str:
    choice = str(requested or GGUF_MMPROJ_AUTO).strip()
    if choice.lower() == GGUF_MMPROJ_NONE:
        return ""
    if choice and choice.lower() != GGUF_MMPROJ_AUTO:
        path = _gguf_lookup(projectors, choice)
        if not path:
            raise ValueError(f"Vision projector not found: {choice}")
        return path
    # Auto: the first mmproj sitting next to the model.
    directory = os.path.dirname(model_path)
    for filename in sorted(os.listdir(directory) if os.path.isdir(directory) else []):
        if filename.lower().endswith(".gguf") and _is_mmproj_name(filename):
            return os.path.join(directory, filename)
    return ""


def _is_gemma_name(model_name: str) -> bool:
    return "gemma" in os.path.basename(str(model_name or "")).lower()


def _gguf_chat_handler(model_name: str, mmproj_path: str):
    """Pick the llama_cpp vision handler that matches the model family.

    Handler classes differ between llama-cpp-python builds and forks, so the
    candidates are tried in order and a missing one is simply skipped.
    """
    lowered = os.path.basename(model_name).lower().replace("_", "-")
    if "gemma" in lowered:
        candidates = ("Gemma4ChatHandler", "Gemma3ChatHandler")
    elif "qwen3" in lowered:
        candidates = ("Qwen3VLChatHandler", "Qwen25VLChatHandler")
    elif "qwen" in lowered:
        candidates = ("Qwen25VLChatHandler", "Qwen3VLChatHandler")
    elif "minicpm" in lowered:
        candidates = ("MiniCPMv26ChatHandler", "Llava15ChatHandler")
    else:
        candidates = ("Llava16ChatHandler", "Llava15ChatHandler")
    from llama_cpp import llama_chat_format

    for name in (*candidates, "Llava15ChatHandler"):
        handler = getattr(llama_chat_format, name, None)
        if handler is None:
            continue
        kwargs: dict[str, Any] = {"clip_model_path": mmproj_path, "verbose": False}
        # Qwen-style handlers reason by default; the widget only wants the
        # answer. Gemma's handler rejects the flag, and older builds of the
        # others do not know it either, hence the retry without it.
        if not _is_gemma_name(model_name):
            kwargs["force_reasoning"] = False
        try:
            return handler(**kwargs)
        except TypeError:
            kwargs.pop("force_reasoning", None)
            try:
                return handler(**kwargs)
            except Exception as exc:
                logging.warning(LOG_PREFIX + "%s could not load the vision projector (%s).", name, exc)
        except Exception as exc:
            logging.warning(LOG_PREFIX + "%s could not load the vision projector (%s).", name, exc)
    return None


_GGUF_LOCK = threading.RLock()
_GGUF_STATE: dict[str, Any] = {"signature": None, "llm": None, "vision": False}
# How many runs are inside the local backend right now. The Unload button can
# arrive from the editor at any moment, and closing a `Llama` that a worker
# thread is still generating with takes llama-cpp down with it.
_GGUF_BUSY = 0


def _gguf_hold(delta: int) -> None:
    global _GGUF_BUSY
    with _GGUF_LOCK:
        _GGUF_BUSY = max(0, _GGUF_BUSY + delta)


def _gguf_release() -> None:
    with _GGUF_LOCK:
        llm = _GGUF_STATE.get("llm")
        _GGUF_STATE["llm"] = None
        _GGUF_STATE["signature"] = None
        _GGUF_STATE["vision"] = False
    if llm is not None:
        try:
            llm.close()
        except Exception:
            pass
        del llm
        try:
            import comfy.model_management

            comfy.model_management.soft_empty_cache()
        except Exception:
            pass


def _gguf_unload_now() -> str:
    """Free the cached model on request. Returns `busy`, `idle` or `unloaded`.

    The whole decision happens under the lock, `llm.close()` included: checking
    and then releasing would leave a window for a run to start in between and
    have its model closed underneath it. `_gguf_hold` takes the same lock, so a
    run that starts here simply waits and then loads its own.
    """
    with _GGUF_LOCK:
        if _GGUF_BUSY > 0:
            return "busy"
        if _GGUF_STATE.get("llm") is None:
            return "idle"
        _gguf_release()
    return "unloaded"


def _gguf_model(settings: Mapping[str, Any], want_vision: bool):
    """Load (or reuse) the configured GGUF through llama-cpp-python."""
    try:
        from llama_cpp import Llama
    except ImportError as exc:
        raise ValueError(
            "llama-cpp-python is not installed. Install it in ComfyUI's Python environment "
            "to use the GGUF format."
        ) from exc

    models, projectors = _gguf_catalog()
    requested = str(settings.get("gguf_model") or "").strip()
    if not requested:
        raise ValueError("Select a GGUF model in the settings")
    model_path = _gguf_lookup(models, requested)
    if not model_path:
        roots = "\n".join(f"  - {path}" for path in _gguf_roots()) or "  - (no model folder found)"
        raise ValueError(f"GGUF model not found: {requested}\nSearched:\n{roots}")
    mmproj_path = _gguf_mmproj_path(model_path, str(settings.get("gguf_mmproj") or ""), projectors) if want_vision else ""
    context = int(settings.get("gguf_context") or GGUF_CONTEXT)
    gpu_layers = int(settings.get("gguf_gpu_layers", GGUF_GPU_LAYERS))
    signature = (model_path, mmproj_path, context, gpu_layers)

    with _GGUF_LOCK:
        if _GGUF_STATE.get("llm") is not None and _GGUF_STATE.get("signature") == signature:
            # The cached flag matters: a projector that failed to load leaves a
            # text-only model behind even though one was requested.
            return _GGUF_STATE["llm"], bool(_GGUF_STATE.get("vision"))
    _gguf_release()

    handler = _gguf_chat_handler(requested, mmproj_path) if mmproj_path else None
    kwargs: dict[str, Any] = {
        "model_path": model_path,
        "n_ctx": context,
        "n_gpu_layers": gpu_layers,
        "verbose": False,
    }
    if handler is not None:
        kwargs["chat_handler"] = handler
    _log(
        "loading GGUF %s (ctx=%d, gpu_layers=%d, vision=%s)...",
        os.path.basename(model_path), context, gpu_layers, bool(handler),
    )
    started = time.perf_counter()
    llm = Llama(**kwargs)
    _log("GGUF loaded in %.1fs", time.perf_counter() - started)
    with _GGUF_LOCK:
        _GGUF_STATE["llm"] = llm
        _GGUF_STATE["signature"] = signature
        _GGUF_STATE["vision"] = handler is not None
    return llm, handler is not None


def _gguf_call(llm, request: Mapping[str, Any], **extra):
    """Call llama-cpp, dropping the template kwargs an old build cannot take.

    `chat_template_kwargs` only reaches the Jinja renderer on recent builds;
    older ones raise `TypeError` for the unknown argument instead of ignoring
    it. Losing the thinking switch is better than losing the answer.
    """
    try:
        return llm.create_chat_completion(**request, **extra)
    except TypeError:
        if "chat_template_kwargs" not in request:
            raise
        _log("this llama-cpp build does not accept chat_template_kwargs; retrying without it")
        fallback = {key: value for key, value in request.items() if key != "chat_template_kwargs"}
        return llm.create_chat_completion(**fallback, **extra)


def _gguf_stream(llm, request: dict[str, Any], should_stop) -> str:
    """Generate token by token so a cancel lands within one token.

    llama-cpp's `stopping_criteria` exists on `create_completion` but not on
    `create_chat_completion`, so the chat API can only be interrupted by walking
    its stream and closing it. Prompt evaluation still runs to completion before
    the first token arrives; nothing in llama-cpp interrupts that.
    """
    pieces: list[str] = []
    tokens = 0
    started = time.perf_counter()
    stream = _gguf_call(llm, request, stream=True)
    try:
        for chunk in stream:
            choice = ((chunk.get("choices") or [{}])[0]) if isinstance(chunk, Mapping) else {}
            delta = choice.get("delta") if isinstance(choice.get("delta"), Mapping) else choice.get("message")
            piece = (delta or {}).get("content") if isinstance(delta, Mapping) else None
            if piece:
                pieces.append(str(piece))
                tokens += 1
            if should_stop():
                raise _Cancelled("Generation was cancelled")
    finally:
        # Closing the generator unwinds llama-cpp's own sampling loop, which is
        # what actually frees the GPU when the user stops early.
        try:
            stream.close()
        except Exception:
            pass
    elapsed = time.perf_counter() - started
    _log("generated %d tokens in %.1fs (%.1f tok/s)", tokens, elapsed, tokens / max(elapsed, 1e-6))
    return "".join(pieces)


def _gguf_chat(
    llm,
    system_prompt: str,
    user_text: str,
    parts: Sequence[Mapping[str, Any]],
    max_tokens: int,
    temperature: float,
    gemma: bool,
    should_stop=None,
    no_think: bool = True,
) -> str:
    """One llama-cpp turn with reasoning off and cancellation wired in."""
    # Qwen switches reasoning off with an inline token; Gemma has no equivalent
    # and relies on the handler flag plus the output cleanup.
    text = user_text if gemma or not no_think else f"/no_think\n{user_text}"
    stop = ["<|turn>", "<|channel>", "<end_of_turn>", "<start_of_turn>"] if gemma else ["<|im_end|>", "<|im_start|>"]
    content: Any = [{"type": "text", "text": text}, *parts] if parts else text
    messages: list[dict[str, Any]] = []
    if str(system_prompt or "").strip():
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": content})
    request = {
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "top_p": 0.95,
        "seed": 0,
        "stop": stop,
    }
    if no_think:
        # Newer Qwen templates dropped the `/no_think` token for a template
        # variable. llama-cpp only forwards this when it renders a Jinja
        # template, and ignores it otherwise, so sending both is the only way to
        # cover the whole family.
        request["chat_template_kwargs"] = {"enable_thinking": False}
    if should_stop is not None:
        return _clean_output(_gguf_stream(llm, request, should_stop), no_think)
    step = time.perf_counter()
    result = _gguf_call(llm, request)
    _log("generation finished in %.1fs", time.perf_counter() - step)
    choices = result.get("choices") if isinstance(result, Mapping) else None
    message = (choices or [{}])[0].get("message") if choices else None
    return _clean_output((message or {}).get("content"), no_think)


def _gguf_describe(
    settings: Mapping[str, Any],
    media_items: Sequence[Mapping[str, Any]],
    should_stop=None,
) -> tuple[str, int]:
    """Describe every attachable media on its own, then return one text block.

    Two benefits that matter for a local model: each pass carries one image and
    a short system prompt instead of the whole media set behind a long question,
    and cancellation is honoured between assets as well as during generation.
    """
    describable = [item for item in media_items if str(item.get("type")) in DESCRIBE_REQUESTS]
    if not describable:
        return "", 0
    llm, vision = _gguf_model(settings, True)
    if not vision:
        logging.warning(LOG_PREFIX + "no vision projector for this GGUF; answering from text only.")
        return "", 0
    gemma = _is_gemma_name(str(settings.get("gguf_model") or ""))
    length = min(int(settings.get("max_length") or MAX_LENGTH_DEFAULT), DESCRIBE_LENGTH)
    temperature = float(settings.get("temperature", CONFIG_DEFAULTS["temperature"]))
    started = time.perf_counter()
    _log("describing %d connected media with the GGUF, one at a time", len(describable))
    lines: list[str] = []
    try:
        for index, item in enumerate(describable, start=1):
            if should_stop is not None and should_stop():
                raise _Cancelled("Generation was cancelled")
            media_type = str(item.get("type"))
            label = str(item.get("tag") or f"{media_type} {index}")
            parts = list(item.get("parts") or [])
            # llama-cpp has no video channel, so a clip arrives as ordered stills.
            request = (
                VIDEO_STILLS_REQUEST.format(count=len(parts))
                if item.get("sampled") else DESCRIBE_REQUESTS[media_type]
            )
            if not parts:
                # Saying nothing is better than letting the answer imagine it.
                _log("  %s (%d/%d): skipped, llama-cpp takes no %s", label, index, len(describable), media_type)
                continue
            _log(
                "  %s (%d/%d): describing the %s from %d image%s...",
                label, index, len(describable), media_type, len(parts), "" if len(parts) == 1 else "s",
            )
            step = time.perf_counter()
            try:
                description = _gguf_chat(llm, DESCRIBE_SYSTEM, request, parts, length, temperature, gemma, should_stop)
            except _Cancelled:
                raise
            except Exception as exc:
                logging.warning(LOG_PREFIX + "could not describe %s (%s).", label, exc)
                continue
            if not description:
                _log("  %s (%d/%d): skipped, the model returned nothing", label, index, len(describable))
                continue
            _log("  %s (%d/%d): %.1fs, %d chars", label, index, len(describable), time.perf_counter() - step, len(description))
            lines.append(f"{label}: {description}")
    except _Cancelled:
        _gguf_release()
        raise
    _log("described %d of %d connected media in %.1fs", len(lines), len(describable), time.perf_counter() - started)
    return "\n".join(lines), len(lines)


def _gguf_generate(
    settings: Mapping[str, Any],
    system_prompt: str,
    user_prompt: str,
    media_parts: Sequence[Mapping[str, Any]] | None = None,
    should_stop=None,
    context: str = "",
    keep_vision: bool = False,
) -> str:
    """Run the question through a local GGUF and return the answer."""
    images = [part for part in (media_parts or []) if part.get("type") == "image_url"]
    started = time.perf_counter()
    # keep_vision holds the projector in the signature after a describe pass, so
    # the text-only final pass reuses that model instead of reloading it.
    llm, vision = _gguf_model(settings, bool(images) or keep_vision)
    _log(
        "asking GGUF %s (system=%d chars, question=%d chars, images=%d, descriptions=%d chars)",
        str(settings.get("gguf_model") or ""), len(system_prompt), len(user_prompt), len(images), len(context),
    )
    if images and not vision:
        logging.warning(LOG_PREFIX + "no vision projector for this GGUF; answering from text only.")
        images = []
    gemma = _is_gemma_name(str(settings.get("gguf_model") or ""))
    user_text = (
        f"=== CONNECTED MEDIA ===\n{context.strip()}\n\n=== QUESTION ===\n{user_prompt}"
        if context.strip() else user_prompt
    )
    max_tokens = int(settings.get("max_length") or MAX_LENGTH_DEFAULT)
    temperature = float(settings.get("temperature", CONFIG_DEFAULTS["temperature"]))
    no_think = str(settings.get("thinking") or THINKING_OFF).lower() != THINKING_KEEP
    _log("generating (max_tokens=%d, images=%d, thinking=%s)...", max_tokens, len(images), "off" if no_think else "kept")
    try:
        text = _gguf_chat(llm, system_prompt, user_text, images, max_tokens, temperature, gemma, should_stop, no_think)
    except _Cancelled:
        # Stopping hands the VRAM back. Keeping a model resident for a
        # generation the user abandoned is the opposite of what they asked for.
        _gguf_release()
        raise
    finally:
        if _as_bool(settings.get("gguf_unload_after")):
            _gguf_release()
    if not text:
        raise ValueError("The GGUF model returned an empty answer")
    _log("finished in %.1fs (%d chars)", time.perf_counter() - started, len(text))
    return text


# --------------------------------------------------------------------------- #
# Shared pipeline
# --------------------------------------------------------------------------- #


def _generate(
    settings: Mapping[str, Any],
    system_prompt: str,
    question: str,
    media_items: Sequence[Mapping[str, Any]],
    should_stop=None,
) -> str:
    """Run one question through the configured backend.

    `media_items` are already-resolved parts, so the same pipeline serves the
    editor route (file paths) and node execution (tensors).
    """
    api_format = str(settings.get("api_format") or FORMAT_OPENAI).lower()
    local = api_format == FORMAT_GGUF
    # Held for the whole local run, describe pass included, so an Unload pressed
    # mid-generation is refused instead of freeing a model that is in use.
    if local:
        _gguf_hold(1)
    try:
        describe = local and _as_bool(settings.get("gguf_describe_media")) and bool(media_items)
        described, described_count = "", 0
        if describe:
            described, described_count = _gguf_describe(settings, media_items, should_stop)
        attached = [] if describe else [item for item in media_items if item.get("parts")]
        media_parts = [part for item in attached for part in item["parts"]]
        system = _system_prompt(system_prompt, attached, described_count)
        if local:
            return _gguf_generate(settings, system, question, media_parts, should_stop, described, describe)
        return _http_generate(settings, system, question, media_parts)
    finally:
        if local:
            _gguf_hold(-1)


def _validate(settings: Mapping[str, Any]) -> str:
    """Return an error message when the settings cannot answer anything."""
    api_format = str(settings.get("api_format") or FORMAT_OPENAI).lower()
    if api_format not in FORMATS:
        return "Unsupported API format"
    if api_format == FORMAT_GGUF:
        if not str(settings.get("gguf_model") or "").strip():
            return "Select a GGUF model in the settings"
        return ""
    missing = [
        name for name, key in (("API URL", "api_url"), ("model", "model"), ("API key", "api_key"))
        if not str(settings.get(key) or "").strip()
    ]
    return f"The settings are incomplete: {', '.join(missing)} required" if missing else ""


def _notify_answer(node_id: Any, answer: str) -> None:
    """Mirror an execution-time answer back into the node's widget.

    Best effort only: a headless or API run has no listener, and the answer is
    already on the node's output either way.
    """
    if node_id is None:
        return
    try:
        from server import PromptServer

        instance = getattr(PromptServer, "instance", None)
        if instance is None:
            return
        instance.send_sync(ANSWER_EVENT, {"node_id": str(node_id), "answer": str(answer)})
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# HTTP routes
# --------------------------------------------------------------------------- #


def _register_routes() -> bool:
    try:
        from aiohttp import web
        from server import PromptServer
    except Exception:
        return False
    routes = getattr(getattr(PromptServer, "instance", None), "routes", None)
    if routes is None or getattr(_register_routes, "_registered", False):
        return bool(getattr(_register_routes, "_registered", False))

    @routes.get(f"{ROUTE_PREFIX}/settings")
    async def _settings_get(request):
        return web.json_response({"ok": True, "settings": _read_config()})

    @routes.post(f"{ROUTE_PREFIX}/settings")
    async def _settings_post(request):
        try:
            payload = await request.json()
            return web.json_response({"ok": True, "settings": _write_config(payload if isinstance(payload, dict) else {})})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    @routes.get(f"{ROUTE_PREFIX}/gguf_models")
    async def _gguf_models_get(request):
        try:
            models, projectors = await asyncio.to_thread(_gguf_catalog)
            return web.json_response({
                "ok": True,
                "models": sorted(models),
                "mmproj": sorted(projectors),
                "roots": _gguf_roots(),
            })
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    @routes.post(f"{ROUTE_PREFIX}/unload")
    async def _unload_route(request):
        """Free the model the configured backend is holding.

        For gguf that is this process's own cache; for an OpenAI-compatible
        server it is a request to LM Studio's REST API. Gemini has nothing to
        free, and the button is not shown for it.
        """
        try:
            settings = _read_config()
            api_format = str(settings.get("api_format") or FORMAT_OPENAI).lower()
            if api_format == FORMAT_GGUF:
                # llm.close() blocks while the weights are freed, so it stays off
                # the event loop like every other model-touching call here.
                state = await asyncio.to_thread(_gguf_unload_now)
                if state == "busy":
                    return web.json_response(
                        {"ok": False, "busy": True, "error": "A generation is still running"}, status=409,
                    )
                if state == "unloaded":
                    _log("model unloaded")
                return web.json_response({"ok": True, "unloaded": state == "unloaded"})
            if api_format != FORMAT_OPENAI:
                return web.json_response(
                    {"ok": False, "error": "This backend has no model to unload"}, status=400,
                )
            if not str(settings.get("api_url") or "").strip():
                return web.json_response({"ok": False, "error": "Set the API URL in the settings first"}, status=400)
            count, detail = await asyncio.to_thread(_remote_unload, settings)
            return web.json_response({"ok": True, "unloaded": count > 0, "detail": detail})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    @routes.post(f"{ROUTE_PREFIX}/generate_cancel")
    async def _generate_cancel(request):
        try:
            payload = await request.json()
            request_id = str((payload or {}).get("request_id") or "")
            if not _cancel(request_id):
                return web.json_response({"ok": False, "error": "A request id is required"}, status=400)
            _log("cancel requested for %s", request_id)
            return web.json_response({"ok": True})
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)

    @routes.post(f"{ROUTE_PREFIX}/generate")
    async def _generate_route(request):
        request_id = ""
        try:
            payload = await request.json()
            request_id = str(payload.get("request_id") or "")
            question = str(payload.get("question") or "")
            system = str(payload.get("system_prompt") or "")
            if not question.strip():
                return web.json_response({"ok": False, "error": "Type a question first"}, status=400)
            settings = _read_config()
            problem = _validate(settings)
            if problem:
                return web.json_response({"ok": False, "error": problem}, status=400)
            api_format = str(settings.get("api_format") or FORMAT_OPENAI).lower()
            # llama-cpp takes the same OpenAI-shaped image parts as the HTTP
            # chat-completions format, so the media builder is shared.
            parts_format = FORMAT_OPENAI if api_format == FORMAT_GGUF else api_format
            resources = payload.get("resources") if isinstance(payload.get("resources"), list) else []
            items = await asyncio.to_thread(_media_items, resources, parts_format) if _as_bool(settings.get("read_media"), True) else []
            # The GGUF loop polls this, so cancelling actually stops the
            # generation instead of only freeing the editor.
            should_stop = (lambda: _is_cancelled(request_id)) if request_id else None
            _raise_if_cancelled(request_id)
            answer = await asyncio.to_thread(_generate, settings, system, question, items, should_stop)
            # An HTTP request cannot be interrupted mid-flight, so a late cancel
            # is honoured by throwing the answer away.
            _raise_if_cancelled(request_id)
            skipped = [item["tag"] for item in items if not item.get("parts")]
            return web.json_response({"ok": True, "answer": answer, "skipped": skipped})
        except _Cancelled as exc:
            # Nothing is running in a worker thread at this point, so freeing a
            # model the cancelled run had loaded is safe here.
            _gguf_release()
            _log("generation cancelled")
            return web.json_response({"ok": False, "cancelled": True, "error": str(exc)}, status=409)
        except asyncio.CancelledError:
            # The editor went away (tab closed, page reloaded). Stop the work
            # the same way an explicit cancel would. The model is *not* freed
            # from here: the worker thread outlives this handler and does it
            # itself once it sees the cancel.
            _cancel(request_id)
            raise
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=500)
        finally:
            _forget_cancel(request_id)

    _register_routes._registered = True
    return True


def _register_routes_when_ready() -> None:
    if _register_routes():
        return

    def wait_for_server() -> None:
        # ComfyUI creates PromptServer shortly after custom-node imports. Retry
        # for a bounded period without delaying node import.
        for _ in range(2400):
            if _register_routes():
                return
            threading.Event().wait(0.05)

    threading.Thread(target=wait_for_server, daemon=True, name="LLMWidgetRoutes").start()


# --------------------------------------------------------------------------- #
# Node
# --------------------------------------------------------------------------- #


def _execution_media_items(image, video, api_format: str) -> list[dict[str, Any]]:
    """Build request parts from the tensors an execution actually receives."""
    items: list[dict[str, Any]] = []
    if image is not None:
        parts = _tensor_still_parts(image, api_format, IMAGE_BATCH_STILLS)
        items.append({"tag": "image 1", "type": "image", "path": "", "parts": parts, "sampled": len(parts) > 1})
    if video is not None:
        frames = None
        try:
            frames = video.get_components().images
        except Exception as exc:
            logging.warning(LOG_PREFIX + "could not read the connected video (%s).", exc)
        parts = _tensor_still_parts(frames, api_format, VIDEO_STILLS) if frames is not None else []
        items.append({"tag": "video 1", "type": "video", "path": "", "parts": parts, "sampled": bool(parts)})
    return items


class LLMWidget:
    CATEGORY = "LLM Widget"
    FUNCTION = "run"
    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("text",)
    # Deliberately not an OUTPUT_NODE: this is a tool, not a step. An output
    # node would be pulled into every queued prompt even when nothing reads its
    # text, which is the opposite of a widget the user keeps muted.
    DESCRIPTION = (
        "Ask a local or remote LLM a question, optionally about a connected image or video. "
        "Runs from the editor without executing the graph; keep it bypassed or muted."
    )

    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {
                "system_prompt": ("STRING", {"multiline": True, "default": DEFAULT_SYSTEM_PROMPT}),
                "question": ("STRING", {"multiline": True, "default": ""}),
                "answer": ("STRING", {"multiline": True, "default": ""}),
                # Off by default: the point of this node is to answer in the
                # editor, so an execution normally just hands the stored answer
                # to whatever is wired to the output.
                "generate_on_execute": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "image": ("IMAGE",),
                "video": ("VIDEO",),
            },
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    @classmethod
    def IS_CHANGED(cls, **kwargs):
        return float("nan")

    def run(self, system_prompt, question, answer, generate_on_execute, image=None, video=None, unique_id=None):
        stored = str(answer or "")
        if not _as_bool(generate_on_execute):
            if not stored.strip():
                logging.warning(
                    LOG_PREFIX + "the answer is empty. Click the run button on the node, "
                    "or enable generate_on_execute."
                )
            return (stored,)
        settings = _read_config()
        problem = _validate(settings)
        if problem:
            raise ValueError(problem)
        if not str(question or "").strip():
            raise ValueError("Type a question first")
        api_format = str(settings.get("api_format") or FORMAT_OPENAI).lower()
        parts_format = FORMAT_OPENAI if api_format == FORMAT_GGUF else api_format
        items = (
            _execution_media_items(image, video, parts_format)
            if _as_bool(settings.get("read_media"), True) else []
        )
        text = _generate(settings, str(system_prompt or ""), str(question), items)
        _notify_answer(unique_id, text)
        return (text,)


_register_routes_when_ready()

NODE_CLASS_MAPPINGS = {"LLMWidget": LLMWidget}
NODE_DISPLAY_NAME_MAPPINGS = {"LLMWidget": "LLM Widget"}
