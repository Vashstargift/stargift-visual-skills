"""higgsfield_client — вызовы Higgsfield MCP из кода бота (генерация фото).

Единая точка для «Gemini-работы, переехавшей на Хиггсфилд»: text→image и
image+prompt→image через модель nano_banana_2 (тот же Google-движок, что и
Gemini flash-image, но кредиты тратятся из подписки Higgsfield Ultimate).

Как ходим в MCP:
  * Внутри гейтвея — через УЖЕ живое соединение (`tools.mcp_tool._servers`)
    и его фоновый цикл. Свой цикл в гейтвее создавать нельзя — а глобальный
    останавливать тем более (убьёт все MCP-коннекты гейтвея).
  * В standalone-скрипте соединения нет — подключаемся ад-хок и гасим только
    СВОЙ server (loop не трогаем: процесс всё равно завершится).

Формы ответов сняты с живого сервера 06.07.2026:
  generate_image → structuredContent {"results": [{"id": "<job-uuid>", ...}]}
  job_status(sync) → {"generation": {"status": "completed",
                                     "results": {"rawUrl": ..., "minUrl": ...}}}
  media_upload → {"uploads": [{"upload_url", "media_id", ...}]}  (PUT + confirm)

Ошибки → HiggsfieldError; вызывающие ловят и падают на Gemini-фолбэк.
"""
from __future__ import annotations

import json
import logging
import urllib.request

logger = logging.getLogger(__name__)

SERVER = "higgsfield"
DEFAULT_MODEL = "nano_banana_2"
_POLL_TRIES = 14          # job_status(sync=True) держит ~25s → запас ~5 мин
_CALL_TIMEOUT = 60        # на один MCP-вызов (sync-поллинг внутри до ~25s)


class HiggsfieldError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Чистые парсеры (покрыты тестами)
# ---------------------------------------------------------------------------

def extract_job_id(struct) -> str | None:
    """generate_image → id первого результата."""
    try:
        results = struct.get("results") or []
        return results[0]["id"]
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


def extract_generation(struct) -> tuple[str, str | None]:
    """job_status → (status, url|None). url только при status=completed."""
    try:
        gen = struct.get("generation") or {}
        status = gen.get("status") or "unknown"
        res = gen.get("results") or {}
        url = res.get("rawUrl") or res.get("minUrl")
        return status, (url if status == "completed" else None)
    except AttributeError:
        return "unknown", None


def extract_upload(struct) -> tuple[str | None, str | None]:
    """media_upload → (upload_url, media_id) первого слота."""
    try:
        up = (struct.get("uploads") or [{}])[0]
        return up.get("upload_url"), up.get("media_id")
    except (AttributeError, IndexError, TypeError):
        return None, None


# ---------------------------------------------------------------------------
# Транспорт
# ---------------------------------------------------------------------------

def _struct_of(result):
    sc = getattr(result, "structuredContent", None)
    if sc:
        return sc
    for block in (getattr(result, "content", None) or []):
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except (json.JSONDecodeError, ValueError):
                return {"text": text}
    return {}


def _call_tool(tool: str, args: dict, timeout: float = _CALL_TIMEOUT):
    """Вызвать тул Higgsfield MCP; вернуть structuredContent (dict)."""
    from tools import mcp_tool as MT

    with MT._lock:
        server = MT._servers.get(SERVER)

    if server is not None and server.session is not None:
        async def _via_gateway():
            async with server._rpc_lock:
                return await server.session.call_tool(tool, arguments=args)
        result = MT._run_on_mcp_loop(_via_gateway(), timeout=timeout)
    else:
        # standalone: ад-хок соединение; глобальный loop НЕ останавливаем.
        import asyncio
        from hermes_cli.mcp_config import _get_mcp_servers, _resolve_mcp_server_config
        cfg = _resolve_mcp_server_config(_get_mcp_servers()[SERVER])
        MT._ensure_mcp_loop()

        async def _adhoc():
            srv = await asyncio.wait_for(MT._connect_server(SERVER, cfg), timeout=60)
            try:
                async with srv._rpc_lock:
                    return await srv.session.call_tool(tool, arguments=args)
            finally:
                await srv.shutdown()
        result = MT._run_on_mcp_loop(_adhoc(), timeout=timeout + 70)

    if getattr(result, "isError", False):
        text = "".join(getattr(b, "text", "") for b in (result.content or []))
        raise HiggsfieldError(f"{tool}: {text[:300] or 'MCP error'}")
    return _struct_of(result)


# ---------------------------------------------------------------------------
# Публичный API
# ---------------------------------------------------------------------------

def upload_image(png_bytes: bytes, filename: str = "input.png") -> str:
    """Залить картинку в хранилище Higgsfield → media_id."""
    struct = _call_tool("media_upload", {
        "filename": filename, "content_type": "image/png",
    })
    upload_url, media_id = extract_upload(struct)
    if not upload_url or not media_id:
        raise HiggsfieldError(f"media_upload: неожиданный ответ {str(struct)[:200]}")
    req = urllib.request.Request(
        upload_url, data=png_bytes, method="PUT",
        headers={"Content-Type": "image/png"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        if resp.status not in (200, 201, 204):
            raise HiggsfieldError(f"upload PUT: HTTP {resp.status}")
    _call_tool("media_confirm", {"type": "image", "media_ids": [media_id]})
    return media_id


def generate_image_bytes(
    prompt: str,
    aspect_ratio: str = "4:3",
    model: str = DEFAULT_MODEL,
    reference_png: bytes | None = None,
) -> bytes:
    """Сгенерировать картинку (опц. с фото-референсом) → PNG/WEBP байты."""
    if not (prompt or "").strip():
        raise HiggsfieldError("пустой prompt")

    params: dict = {"model": model, "prompt": prompt, "aspect_ratio": aspect_ratio}
    if reference_png:
        media_id = upload_image(reference_png)
        params["medias"] = [{"value": media_id, "role": "image"}]

    struct = _call_tool("generate_image", {"params": params})
    job_id = extract_job_id(struct)
    if not job_id:
        raise HiggsfieldError(f"generate_image: нет job id в {str(struct)[:200]}")

    for _ in range(_POLL_TRIES):
        struct = _call_tool("job_status", {"jobId": job_id, "sync": True})
        status, url = extract_generation(struct)
        if status == "completed" and url:
            req = urllib.request.Request(url, headers={"User-Agent": "stargift-bot"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        if status in ("failed", "canceled", "nsfw", "ip_detected"):
            raise HiggsfieldError(f"генерация {job_id}: status={status}")
    raise HiggsfieldError(f"генерация {job_id}: не завершилась за отведённое время")
