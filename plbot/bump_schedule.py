"""Очередь поднятия: лоты делят интервал поровну и не выходят одновременно."""

from __future__ import annotations

from datetime import datetime, timedelta


def parse_stamp(value) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def pick_staggered_item(
    item_ids: list[str],
    interval: float,
    now: datetime,
    anchor: datetime,
    last_bumped: dict[str, datetime],
    last_slot_at: datetime | None,
    active_ids: set[str] | None = None,
) -> str | None:
    """Возвращает один лот, которому сейчас пора подняться.

    Слот лота i — это anchor + i * (interval / n) и дальше каждый полный интервал.
    Между любыми двумя поднятиями выдерживается тот же слот, поэтому лоты
    не собираются в одну секунду даже после простоя.

    active_ids — лоты, которые сейчас реально можно поднять (APPROVED).
    Индексы и шаг считаются по полному составу item_ids, чтобы временная
    модерация одного лота не сдвигала очередь остальных.
    """

    ids = [item_id for item_id in item_ids if item_id]
    count = len(ids)
    if count == 0 or interval <= 0:
        return None

    slot = interval / count
    if last_slot_at is not None and (now - last_slot_at).total_seconds() < max(slot - 0.5, 0):
        return None

    elapsed = (now - anchor).total_seconds()
    if elapsed < -0.5:
        return None

    due: list[tuple[float, int, str]] = []
    for index, item_id in enumerate(ids):
        if active_ids is not None and item_id not in active_ids:
            continue
        offset = index * slot
        if elapsed + 0.5 < offset:
            continue
        last = last_bumped.get(item_id)
        if last is not None and (now - last).total_seconds() < interval - 1:
            continue
        cycles = int((elapsed - offset) // interval)
        if cycles < 0:
            continue
        scheduled = anchor + timedelta(seconds=cycles * interval + offset)
        if now + timedelta(seconds=1) < scheduled:
            continue
        if last is None:
            waited = (now - (anchor + timedelta(seconds=offset))).total_seconds() + interval
        else:
            waited = (now - last).total_seconds()
        due.append((waited, index, item_id))

    if not due:
        return None
    due.sort(key=lambda row: (-row[0], row[1]))
    return due[0][2]


def next_slot_time(
    index: int,
    count: int,
    interval: float,
    now: datetime,
    anchor: datetime,
    last: datetime | None,
) -> datetime:
    """Ближайшее время слота этого лота, не раньше чем через полный интервал после прошлого поднятия."""

    if count <= 0 or interval <= 0:
        return now
    offset = index * (interval / count)
    first = anchor + timedelta(seconds=offset)
    earliest = first if last is None else max(first, last + timedelta(seconds=interval))
    if earliest >= now - timedelta(seconds=1):
        return earliest

    passed = (now - first).total_seconds()
    steps = int(passed // interval)
    candidate = first + timedelta(seconds=steps * interval)
    if candidate < now - timedelta(seconds=1):
        candidate += timedelta(seconds=interval)
    if last is not None and candidate < last + timedelta(seconds=interval) - timedelta(seconds=1):
        candidate += timedelta(seconds=interval)
    return candidate
