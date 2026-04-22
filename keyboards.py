from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


FILTER_OPTIONS = {
    "all": ("Все отзывы", None),
    "1":   ("⭐ Только 1 звезда", [1]),
    "1-2": ("⭐⭐ 1–2 звезды", [1, 2]),
    "1-3": ("⭐⭐⭐ 1–3 звезды", [1, 2, 3]),
    "4-5": ("⭐⭐⭐⭐⭐ 4–5 звёзд", [4, 5]),
    "manual": ("✏️ Выбрать вручную", None),
}

# Метки для имён файлов (без эмодзи, безопасно для ФС)
FILTER_FILE_LABELS = {
    "all": "все",
    "1":   "1звезда",
    "1-2": "1-2звезды",
    "1-3": "1-3звезды",
    "4-5": "4-5звёзд",
}

STAR_NAMES = {1: "⭐1", 2: "⭐⭐2", 3: "⭐⭐⭐3", 4: "⭐⭐⭐⭐4", 5: "⭐⭐⭐⭐⭐5"}


def filter_keyboard(selected_key: str = "all") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for key, (label, _) in FILTER_OPTIONS.items():
        prefix = "✅ " if key == selected_key else ""
        builder.button(text=f"{prefix}{label}", callback_data=f"filter:{key}")
    builder.button(text="❌ Отмена", callback_data="filter:cancel")
    builder.adjust(2)
    return builder.as_markup()


def manual_star_keyboard(selected: set[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for star in [1, 2, 3, 4, 5]:
        mark = "✅ " if star in selected else ""
        builder.button(
            text=f"{mark}{STAR_NAMES[star]}",
            callback_data=f"star:{star}",
        )
    builder.button(text="✔️ Подтвердить", callback_data="star:confirm")
    builder.button(text="◀️ Назад", callback_data="star:cancel")
    builder.adjust(3, 2, 2)
    return builder.as_markup()


def history_keyboard(runs: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for run in runs:
        created = (run.get("created_at") or "")[:16]
        label = f"📦 {run['article']} | {run['filter_label']} | {created}"
        # Telegram ограничивает длину кнопки; обрезаем с запасом
        if len(label) > 60:
            label = label[:57] + "…"
        builder.button(text=label, callback_data=f"history:{run['id']}")
    builder.adjust(1)
    return builder.as_markup()


def collecting_keyboard() -> InlineKeyboardMarkup:
    """Кнопка отмены поверх прогресс-сообщения."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🛑 Отменить сбор", callback_data="cancel_run")
    return builder.as_markup()


def manual_file_label(stars: list[int]) -> str:
    """Читаемое имя файла для произвольного набора звёзд.
    [1, 2] -> '1-2звезды', [1, 3, 5] -> '1-3-5звёзд', [3] -> '3звезды'.
    """
    if not stars:
        return "пусто"
    joined = "-".join(str(s) for s in sorted(stars))
    suffix = "звезда" if len(stars) == 1 and stars[0] == 1 else "звёзд" if len(stars) > 2 else "звезды"
    return f"{joined}{suffix}"
