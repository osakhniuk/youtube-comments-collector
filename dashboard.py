"""Local Streamlit dashboard. Reads SQLite/logs; never invokes the collector."""

from datetime import datetime, timezone

import pandas as pd
import streamlit as st

from src.youtube_collector.dashboard_data import (
    chart_counts, collector_status, filter_comments, parse_date, read_api_statistics,
    read_limit, read_snapshot, safe_url, statistics,
)


def format_date(value):
    return value.strftime("%d.%m.%Y %H:%M") if value else "Дані відсутні"


def render():
    st.set_page_config(page_title="YouTube · Статистика збору", page_icon="💬", layout="wide")
    title, refresh = st.columns([4, 1], vertical_alignment="center")
    with title:
        st.title("YouTube · Коментарі")
        st.caption("Локальна аналітика зібраних даних · лише читання · час UTC")
    with refresh:
        st.button("Оновити статистику", width="stretch")
    # Every widget interaction reruns these reads. No cache or HTTP client exists here.
    snapshot = read_snapshot()
    if snapshot.error:
        st.error(snapshot.error)
        st.info("Перевірте наявність data/comments.db. Дашборд не створює базу та не запускає збір.")
        return
    for warning in snapshot.warnings:
        st.warning(warning)
    limit, warning = read_limit()
    if warning:
        st.warning(warning)
    stats = statistics(snapshot)
    status = collector_status(snapshot, limit)
    for columns in (
        (("Коментарів", stats["total"]), ("Унікальних ID", stats["unique"]), ("Відео", stats["videos"])),
        (("Найстаріший коментар", format_date(stats["oldest"])),
         ("Найновіший коментар", format_date(stats["newest"])), ("Стан колектора", status)),
    ):
        for container, (label, value) in zip(st.columns(3), columns):
            with container.container(border=True):
                st.metric(label, value)
    if limit:
        st.progress(min(stats["total"] / limit, 1.0), text=f"MAX_TOTAL_COMMENTS: {stats['total']} / {limit}")
    else:
        st.info("Заповнення ліміту: Дані відсутні")
    st.caption("Оновлено: " + datetime.now(timezone.utc).strftime("%d.%m.%Y %H:%M:%S UTC")
               + ". Стан показано за збереженим прогресом; це не індикатор активного процесу.")
    if not snapshot.comments:
        st.info("База порожня. Коментарів для графіків і таблиці поки немає.")
    overview, table, state_tab = st.tabs(["Огляд", "Коментарі", "Стан та API"])
    with overview:
        st.subheader("Динаміка публікацій")
        counts = chart_counts(snapshot.comments)
        left, right = st.columns([2, 1])
        with left:
            if counts["dates"]:
                st.bar_chart(pd.DataFrame(counts["dates"], columns=["Дата", "Коментарів"]),
                             x="Дата", y="Коментарів", color="#3b82f6")
            else:
                st.info("Дати коментарів: Дані відсутні")
        with right:
            st.caption("Коментарі за відео")
            if counts["videos"]:
                st.bar_chart(pd.DataFrame(counts["videos"], columns=["Video ID", "Коментарів"]),
                             x="Video ID", y="Коментарів", horizontal=True)
            else:
                st.info("Відео: Дані відсутні")
        st.subheader("Час публікації протягом доби")
        if sum(value for _, value in counts["hours"]) >= 5 and len(counts["hours"]) >= 2:
            hours = dict(counts["hours"])
            st.bar_chart(pd.DataFrame({"Година UTC": [f"{hour:02d}:00" for hour in range(24)],
                                      "Коментарів": [hours.get(hour, 0) for hour in range(24)]}),
                         x="Година UTC", y="Коментарів")
        else:
            st.info("Недостатньо даних для змістовного розподілу за годинами.")
        if stats["missing_dates"]:
            st.caption(f"Без коректної дати: {stats['missing_dates']}. Ці записи не входять до часових графіків.")
    with table:
        st.subheader("Знайти коментар")
        text_col, author_col = st.columns(2)
        query = text_col.text_input("Пошук у тексті", placeholder="Слово або фраза")
        author = author_col.text_input("Пошук за автором", placeholder="Ім’я автора")
        video_values = sorted({row["video_id"] for row in snapshot.comments if row.get("video_id")})
        selected = st.multiselect("Відео", options=video_values, default=video_values)
        date_col, order_col = st.columns(2)
        start = end = None
        if stats["oldest"] and stats["newest"]:
            dates = date_col.date_input("Дати публікації (UTC)",
                                       value=(stats["oldest"].date(), stats["newest"].date()),
                                       min_value=stats["oldest"].date(), max_value=stats["newest"].date(),
                                       format="DD.MM.YYYY")
            if len(dates) == 2:
                start, end = dates
            else:
                st.info("Виберіть початкову та кінцеву дату. Поки що фільтр дат не застосовано.")
        order = order_col.selectbox("Сортування", ["Спочатку нові", "Спочатку старі"])
        include_undated = st.checkbox("Показати також коментарі без дати", value=True) if stats["missing_dates"] else False
        rows = filter_comments(snapshot.comments, text=query, author=author, videos=selected,
                               start=start, end=end, ascending=order == "Спочатку старі",
                               include_undated=include_undated)
        st.caption(f"Знайдено: {len(rows)} із {stats['total']}")
        if rows:
            display = pd.DataFrame(rows)[["author", "date", "text", "url", "video_id"]]
            display["date"] = pd.to_datetime(display["date"].map(parse_date), utc=True, errors="coerce")
            display["url"] = display["url"].map(safe_url)
            st.dataframe(display, hide_index=True, width="stretch", height=500, column_config={
                "author": "Автор", "date": "Дата публікації (UTC)",
                "text": st.column_config.TextColumn("Коментар", width="large"),
                "url": st.column_config.LinkColumn("Посилання", display_text="Відкрити коментар"),
                "video_id": "Video ID",
            })
            st.caption("Таблиця підтримує додаткове сортування натисканням заголовків колонок.")
        else:
            st.info("За вибраними фільтрами коментарів немає.")
    with state_tab:
        st.subheader("Стан колектора")
        if not snapshot.states:
            st.info("Дані відсутні")
        else:
            state_rows = []
            for state in snapshot.states:
                one = type(snapshot)(comments=snapshot.comments, states=[state])
                has_cursor = state.get("has_scan_next_page_token") or state.get("has_backfill_next_page_token")
                partial = state.get("has_scan_pending_page") or state.get("has_backfill_pending_page")
                state_rows.append({"Video ID": state.get("video_id") or "Дані відсутні",
                                   "Watermark (UTC)": format_date(parse_date(state.get("last_seen_at"))),
                                   "Інкрементний стан": collector_status(one, limit),
                                   "Курсор": "Збережено; значення приховано" if has_cursor else
                                              ("Повтор поточної сторінки" if partial else "Відсутній"),
                                   "Ліміт досягнуто": "Дані відсутні" if limit is None else
                                                     ("Так" if stats["total"] >= limit else "Ні")})
            st.dataframe(pd.DataFrame(state_rows), hide_index=True, width="stretch")
            st.caption("Відсутній watermark при початковому зборі, зупиненому лімітом, є нормальним. "
                       "Секретні значення курсорів не завантажуються в інтерфейс.")
        st.subheader("API та обмеження")
        api = read_api_statistics(snapshot)
        labels = (("API-запитів", "requests"), ("HTTP-помилок", "http_errors"),
                  ("Повторних спроб", "retries"), ("Подій rate-limit", "rate_limits"))
        for column, (label, key) in zip(st.columns(4), labels):
            column.metric(label, api[key] if api[key] is not None else "Дані відсутні")
        st.caption(api["note"])
        st.write("Останнє успішне збереження: " + format_date(api["last_success"]))
        st.caption("Це статистика підтверджених подій журналу, а не залишок квоти Google Cloud. "
                   "Mock-тести не враховуються. Оновлення сторінки не робить API-запитів.")


if __name__ == "__main__":
    render()
