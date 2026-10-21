"""Run incremental collection for every configured video."""

import argparse
import logging
import sqlite3

from .collector import collect_video
from .config import load_config
from .logging_setup import configure_logging, log_event, run_context
from .locking import CollectorAlreadyRunning, collector_lock
from .storage import DEFAULT_DB_PATH

LOGGER = logging.getLogger("youtube_collector.main")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="YouTube comments collector")
    parser.add_argument("--mode", choices=("incremental", "backfill"), default="incremental")
    args = parser.parse_args([] if argv is None else argv)
    try:
        config = load_config()
        if not config.video_ids:
            raise ValueError("У .env задайте VIDEO_IDS=MDoDaf629LU.")
        with run_context(), collector_lock(DEFAULT_DB_PATH):
            _run_collection(config, args.mode)
    except CollectorAlreadyRunning as exc:
        raise SystemExit(str(exc)) from None
    except ValueError as exc:
        raise SystemExit(f"Помилка: {exc}") from None
    except (sqlite3.Error, OSError):
        raise SystemExit("Помилка: не вдалося зберегти коментарі у SQLite. Перевірте файл бази та права доступу.") from None


def _run_collection(config, mode):
    try:
        configure_logging(secrets=(config.youtube_api_key,))
    except OSError:
        raise SystemExit("Помилка: не вдалося відкрити logs/collector.log.") from None
    log_event(LOGGER, "run_started", mode=mode, videos=len(config.video_ids),
              max_total_comments=config.max_total_comments,
              max_pages_per_run=config.max_pages_per_run, http_max_retries=config.http_max_retries,
              request_delay_seconds=config.request_delay_seconds,
              connect_timeout_seconds=config.http_connect_timeout_seconds,
              read_timeout_seconds=config.http_read_timeout_seconds)
    failed = False
    for index, video_id in enumerate(config.video_ids):
        try:
            result = (collect_video(config, video_id) if mode == "incremental"
                      else collect_video(config, video_id, mode="backfill"))
        except (sqlite3.Error, OSError, ValueError):
            failed = True
            log_event(LOGGER, "video_failed", level=logging.ERROR, video_id=video_id,
                      reason="storage_or_state_error")
            print(f"Video ID: {video_id}\nПомилка: не вдалося прочитати або зберегти стан SQLite; "
                  "збір цього відео перервано.")
            continue
        status = "завершено" if result.complete else "не завершено; потрібен повторний запуск"
        if result.stop_reason == "total_limit":
            status = "призупинено: досягнуто MAX_TOTAL_COMMENTS"
        if result.error:
            status = "перервано; збережено останню успішну сторінку"
            failed = True
        reasons = {
            "old_page_overlap": "дві старі сторінки за межами часового перекриття",
            "end_of_list": "кінець списку API", "page_limit": "ліміт сторінок за запуск",
            "total_limit": "досягнуто глобальний MAX_TOTAL_COMMENTS",
            "quota_exceeded": "вичерпано спільну квоту API",
            "api_error": "помилка API", "storage_error": "помилка SQLite",
            "invalid_comment": "некоректна сторінка коментарів; курсор збережено",
            "backfill_already_complete": "історичний backfill уже завершено",
        }
        print(f"Режим: {result.mode}\n"
              f"Video ID: {result.video_id}\n"
              f"Виконано API-запитів: {result.api_requests}\n"
              f"Прочитано сторінок: {result.pages}\n"
              f"Причина завершення: {reasons[result.stop_reason]}\n"
              f"Отримано коментарів: {result.received}\n"
              f"Нових збережено: {result.saved}\n"
              f"Дублікатів пропущено: {result.duplicates}\n"
              f"Загалом записів у базі: {result.total}\n"
              f"Стан інкрементного збору: {status}\n"
              f"Чи залишилися сторінки для продовження: {'так' if result.has_more else 'ні'}")
        if result.token_restarted:
            print("Недійсний pageToken: сканування перезапущено з першої сторінки.")
        if result.error:
            print(f"Помилка: {result.error}")
        if result.stop_reason == "total_limit":
            print("Збір призупинено на глобальному ліміті; прогрес збережено для його збільшення.")
            break
        if result.stop_reason == "quota_exceeded":
            skipped = len(config.video_ids) - index - 1
            log_event(LOGGER, "run_stopped", reason="quotaExceeded", skipped_videos=skipped)
            print("Спільну квоту API вичерпано. Подальші запити цього запуску скасовано.")
            break
    log_event(LOGGER, "run_finished", failed=failed)
    if failed:
        raise SystemExit(1)


if __name__ == "__main__":
    import sys
    main(sys.argv[1:])
