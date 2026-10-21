"""Synthetic fixtures exclusively for temporary unit-test databases."""
from datetime import datetime, timedelta, timezone

def sample_records():
    return [dict(author="Тестовий автор", url=f"https://www.youtube.com/watch?v=fixture-video&lc=fixture-{i:03}",
        date=(datetime(2026,1,1,tzinfo=timezone.utc)+timedelta(seconds=i)).isoformat(),
        text='Тест, "лапки"\nДругий рядок', source="youtube", comment_id=f"fixture-{i:03}",
        video_id="fixture-video", collected_at="2026-10-20T10:00:00Z") for i in range(200)]
