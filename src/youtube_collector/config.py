"""Читання конфігурації з .env та змінних середовища."""

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values


PROJECT_ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Config:
    """Налаштування; API-ключ прихований у repr."""

    youtube_api_key: str = field(default="", repr=False)
    video_ids: tuple[str, ...] = ()
    request_delay_seconds: float = 1.0
    max_pages_per_run: int = 5
    max_total_comments: int = 200
    http_max_retries: int = 3
    http_connect_timeout_seconds: float = 5.0
    http_read_timeout_seconds: float = 30.0
    http_max_retry_wait_seconds: float = 120.0

    def __post_init__(self) -> None:
        if not math.isfinite(self.request_delay_seconds) or self.request_delay_seconds < 0:
            raise ValueError("REQUEST_DELAY_SECONDS має бути скінченним числом >= 0.")
        if not isinstance(self.max_pages_per_run, int) or self.max_pages_per_run <= 0:
            raise ValueError("MAX_PAGES_PER_RUN має бути цілим числом > 0.")
        if not isinstance(self.max_total_comments, int) or self.max_total_comments <= 0:
            raise ValueError("MAX_TOTAL_COMMENTS має бути цілим числом > 0.")
        if not isinstance(self.http_max_retries, int) or not 0 <= self.http_max_retries <= 10:
            raise ValueError("HTTP_MAX_RETRIES має бути цілим числом від 0 до 10.")
        for name, value in (
            ("HTTP_CONNECT_TIMEOUT_SECONDS", self.http_connect_timeout_seconds),
            ("HTTP_READ_TIMEOUT_SECONDS", self.http_read_timeout_seconds),
            ("HTTP_MAX_RETRY_WAIT_SECONDS", self.http_max_retry_wait_seconds),
        ):
            if not math.isfinite(value) or not 0 < value <= 3600:
                raise ValueError(f"{name} має бути скінченним числом > 0 та <= 3600.")


def load_config(env_path: str | Path | None = None) -> Config:
    """Прочитати .env; ключ і VIDEO_IDS читаються лише з файлу.

    Для решти налаштувань змінні середовища мають пріоритет.
    Значення конфігурації не виводяться і не логуються.
    """
    path = Path(env_path) if env_path is not None else PROJECT_ROOT / ".env"
    file_values = dotenv_values(path, interpolate=False)
    values = {**file_values, **os.environ}
    # Credentials and selectors must come only from the chosen .env file.
    for name in ("YOUTUBE_API_KEY", "VIDEO_IDS"):
        values[name] = file_values.get(name)

    try:
        delay = float(values.get("REQUEST_DELAY_SECONDS", "1.0"))
    except (TypeError, ValueError):
        raise ValueError("REQUEST_DELAY_SECONDS має бути числом >= 0.") from None
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("REQUEST_DELAY_SECONDS має бути скінченним числом >= 0.")

    try:
        pages = int(values.get("MAX_PAGES_PER_RUN", "5"))
    except (TypeError, ValueError):
        raise ValueError("MAX_PAGES_PER_RUN має бути цілим числом > 0.") from None
    if pages <= 0:
        raise ValueError("MAX_PAGES_PER_RUN має бути цілим числом > 0.")

    try:
        total_limit = int(values.get("MAX_TOTAL_COMMENTS", "200"))
    except (TypeError, ValueError):
        raise ValueError("MAX_TOTAL_COMMENTS має бути цілим числом > 0.") from None

    try:
        retries = int(values.get("HTTP_MAX_RETRIES", "3"))
    except (TypeError, ValueError):
        raise ValueError("HTTP_MAX_RETRIES має бути цілим числом від 0 до 10.") from None
    timeouts = {}
    for name, default in (("HTTP_CONNECT_TIMEOUT_SECONDS", "5"),
                          ("HTTP_READ_TIMEOUT_SECONDS", "30"),
                          ("HTTP_MAX_RETRY_WAIT_SECONDS", "120")):
        try:
            timeouts[name] = float(values.get(name, default))
        except (TypeError, ValueError):
            raise ValueError(f"{name} має бути числом > 0 та <= 3600.") from None

    video_ids = tuple(dict.fromkeys(
        item.strip()
        for item in (values.get("VIDEO_IDS") or "").split(",")
        if item.strip()
    ))
    return Config(
        youtube_api_key=(values.get("YOUTUBE_API_KEY") or "").strip(),
        video_ids=video_ids,
        request_delay_seconds=delay,
        max_pages_per_run=pages,
        max_total_comments=total_limit,
        http_max_retries=retries,
        http_connect_timeout_seconds=timeouts["HTTP_CONNECT_TIMEOUT_SECONDS"],
        http_read_timeout_seconds=timeouts["HTTP_READ_TIMEOUT_SECONDS"],
        http_max_retry_wait_seconds=timeouts["HTTP_MAX_RETRY_WAIT_SECONDS"],
    )
