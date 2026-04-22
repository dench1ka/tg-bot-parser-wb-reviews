"""
Хендлеры Telegram-бота.

Ключевые моменты:
  • Фильтр по звёздам передаётся в wb_api — он применяется на стороне WB.
  • Прогресс-бар считает ожидаемое кол-во С УЧЁТОМ фильтра.
  • Для сводки показываем и общий рейтинг товара, и средний по выборке.
  • Поддержана отмена (/cancel и кнопка «Отменить сбор»).
  • Throttling редактирования прогресс-сообщения — не упираемся в FloodWait.
  • Ошибки WB (not found / network) различаются и показываются человеку.
"""
import asyncio
import logging
import os
import time
from typing import Optional

import aiohttp
from aiogram import Router, F
from aiogram.types import Message, CallbackQuery, FSInputFile
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

from config import ALLOWED_USER_IDS, COLLECTION_TIMEOUT, PROGRESS_EDIT_INTERVAL
from wb_api import (
    parse_article, fetch_reviews, fetch_questions,
    count_total_reviews, get_overall_rating,
    WBNotFound, WBError,
)
from excel_export import save_reviews_xlsx, save_questions_xlsx, create_archive
from keyboards import (
    filter_keyboard, manual_star_keyboard, history_keyboard, collecting_keyboard,
    FILTER_OPTIONS, FILTER_FILE_LABELS, manual_file_label,
)
from database import save_run, get_history, get_run_by_id

logger = logging.getLogger(__name__)
router = Router()


class CollectState(StatesGroup):
    waiting_filter = State()
    manual_stars = State()
    collecting = State()


# Глобальный реестр активных задач сбора: user_id -> asyncio.Task
_active_tasks: dict[int, asyncio.Task] = {}


# ─── Доступ ──────────────────────────────────────────────────────────────────

def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USER_IDS:
        return True  # пустой список = отладочный режим
    return user_id in ALLOWED_USER_IDS


def _access_denied() -> str:
    return "🚫 У вас нет доступа к этому боту."


# ─── Старт / справка ─────────────────────────────────────────────────────────

@router.message(Command("start"))
async def cmd_start(message: Message, state: FSMContext):
    if not is_allowed(message.from_user.id):
        await message.answer(_access_denied())
        return
    await state.clear()
    await message.answer(
        "👋 Привет! Я собираю отзывы и вопросы с Wildberries.\n\n"
        "Отправь мне <b>ссылку на товар</b> или <b>артикул</b> — и я всё соберу.\n\n"
        "📋 <b>Команды:</b>\n"
        "/history — последние 10 сборов\n"
        "/cancel — отменить текущий сбор\n"
        "/help — справка",
        parse_mode="HTML",
    )


@router.message(Command("help"))
async def cmd_help(message: Message):
    if not is_allowed(message.from_user.id):
        await message.answer(_access_denied())
        return
    await message.answer(
        "📖 <b>Как пользоваться:</b>\n\n"
        "1. Отправь ссылку на товар WB или его артикул (число)\n"
        "2. Выбери фильтр по звёздам\n"
        "3. Дождись сбора — бот показывает прогресс\n"
        "4. Получи Excel-файлы и ZIP-архив\n\n"
        "<b>Фильтры отзывов:</b>\n"
        "• Все — все отзывы\n"
        "• 1 / 1–2 / 1–3 / 4–5 звёзд\n"
        "• Вручную — отметь галочками любые звёзды\n\n"
        "📌 Вопросы собираются всегда все, независимо от фильтра.\n\n"
        "/history — скачать файлы прошлых сборов без пересбора\n"
        "/cancel — прервать текущий сбор",
        parse_mode="HTML",
    )


# ─── Отмена ──────────────────────────────────────────────────────────────────

@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    if not is_allowed(message.from_user.id):
        await message.answer(_access_denied())
        return
    await _cancel_user_task(message.from_user.id, state, message)


@router.callback_query(F.data == "cancel_run")
async def cb_cancel_run(callback: CallbackQuery, state: FSMContext):
    if not is_allowed(callback.from_user.id):
        await callback.answer(_access_denied(), show_alert=True)
        return
    await callback.answer("Отменяю…")
    await _cancel_user_task(callback.from_user.id, state, callback.message)


@router.callback_query(CollectState.waiting_filter, F.data == "filter:cancel")
async def cb_cancel_filter(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("❌ Отменено. Отправь новый артикул или ссылку.")
    except TelegramBadRequest:
        pass
    await callback.answer()


async def _cancel_user_task(user_id: int, state: FSMContext, message: Message) -> None:
    task = _active_tasks.get(user_id)
    if task and not task.done():
        task.cancel()
        await message.answer("🛑 Сбор отменяется…")
    else:
        await state.clear()
        await message.answer("ℹ️ Сейчас нет активного сбора.")


# ─── История ─────────────────────────────────────────────────────────────────

@router.message(Command("history"))
async def cmd_history(message: Message):
    if not is_allowed(message.from_user.id):
        await message.answer(_access_denied())
        return

    runs = await get_history(message.from_user.id, limit=10)
    if not runs:
        await message.answer("📭 История пуста. Отправь ссылку или артикул, чтобы начать сбор.")
        return

    await message.answer(
        "🗂 <b>Последние 10 сборов</b>\nВыбери запись, чтобы скачать файлы:",
        reply_markup=history_keyboard(runs),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("history:"))
async def cb_history_item(callback: CallbackQuery):
    if not is_allowed(callback.from_user.id):
        await callback.answer(_access_denied(), show_alert=True)
        return

    try:
        run_id = int(callback.data.split(":", 1)[1])
    except (ValueError, IndexError):
        await callback.answer("❌ Некорректная запись.", show_alert=True)
        return

    run = await get_run_by_id(run_id, callback.from_user.id)
    if not run:
        await callback.answer("❌ Запись не найдена.", show_alert=True)
        return

    try:
        await callback.message.edit_text(
            f"📦 Артикул: <b>{run['article']}</b>\n"
            f"🔍 Фильтр: {run['filter_label']}\n"
            f"📊 Отзывов: {run['reviews_count']}, Вопросов: {run['questions_count']}\n"
            f"⭐ Средний рейтинг товара: {run['avg_rating']}\n"
            f"📅 Дата сбора: {run['created_at']}\n\n"
            "Отправляю файлы…",
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass

    sent_any = False
    for fpath_key, label in (
        ("reviews_file", "📊 Отзывы"),
        ("questions_file", "❓ Вопросы"),
        ("archive_file", "📦 Архив"),
    ):
        fpath = run.get(fpath_key)
        if fpath and os.path.exists(fpath):
            await callback.message.answer_document(FSInputFile(fpath), caption=label)
            sent_any = True

    if not sent_any:
        await callback.message.answer(
            "⚠️ Файлы этого сбора не найдены на диске (возможно, были удалены).\n"
            "Запусти новый сбор, отправив артикул."
        )
    await callback.answer()


# ─── Приём ссылки / артикула ─────────────────────────────────────────────────

@router.message(F.text & ~F.text.startswith("/"))
async def receive_article(message: Message, state: FSMContext):
    if not is_allowed(message.from_user.id):
        await message.answer(_access_denied())
        return

    current_state = await state.get_state()
    if current_state == CollectState.collecting.state:
        await message.answer(
            "⏳ Сбор уже идёт. Дождись окончания или отмени командой /cancel."
        )
        return

    article = parse_article(message.text)
    if not article:
        await message.answer(
            "❌ Не удалось распознать артикул или ссылку.\n\n"
            "Отправь <b>число</b> (артикул) или <b>ссылку</b> вида:\n"
            "<code>https://www.wildberries.ru/catalog/12345678/detail.aspx</code>",
            parse_mode="HTML",
        )
        return

    await state.update_data(article=article, manual_stars=set(), selected_filter_key="all")
    await state.set_state(CollectState.waiting_filter)

    await message.answer(
        f"✅ Артикул: <b>{article}</b>\n\n"
        "Выбери фильтр отзывов (по умолчанию — все):",
        reply_markup=filter_keyboard(selected_key="all"),
        parse_mode="HTML",
    )


# ─── Выбор фильтра ───────────────────────────────────────────────────────────

@router.callback_query(CollectState.waiting_filter, F.data.startswith("filter:"))
async def cb_filter(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    if key == "cancel":
        # обрабатывается в cb_cancel_filter
        return

    data = await state.get_data()
    article = data.get("article")
    if not article:
        await callback.answer("Сессия устарела, отправь артикул ещё раз.", show_alert=True)
        await state.clear()
        return

    if key == "manual":
        await state.set_state(CollectState.manual_stars)
        try:
            await callback.message.edit_text(
                f"✏️ Выбери звёзды (можно несколько):\nАртикул: <b>{article}</b>",
                reply_markup=manual_star_keyboard(set()),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
        await callback.answer()
        return

    if key not in FILTER_OPTIONS:
        await callback.answer("Неизвестный фильтр.", show_alert=True)
        return

    label, stars = FILTER_OPTIONS[key]
    file_label = FILTER_FILE_LABELS.get(key, "все")

    await state.update_data(
        star_filter=stars, filter_label=label, file_label=file_label,
        selected_filter_key=key,
    )
    try:
        await callback.message.edit_text(
            f"📦 Артикул: <b>{article}</b>\n🔍 Фильтр: {label}\n\n⏳ Начинаю сбор…",
            parse_mode="HTML",
        )
    except TelegramBadRequest:
        pass
    await callback.answer()

    await state.set_state(CollectState.collecting)
    await _start_collection_task(
        callback.from_user.id, callback.message, state,
        article, stars, label, file_label,
    )


# ─── Ручной выбор звёзд ──────────────────────────────────────────────────────

@router.callback_query(CollectState.manual_stars, F.data.startswith("star:"))
async def cb_star_toggle(callback: CallbackQuery, state: FSMContext):
    action = callback.data.split(":", 1)[1]
    data = await state.get_data()
    article = data.get("article")
    selected: set = data.get("manual_stars") or set()
    if not isinstance(selected, set):
        selected = set(selected)

    if action == "cancel":
        await state.set_state(CollectState.waiting_filter)
        try:
            await callback.message.edit_text(
                f"✅ Артикул: <b>{article}</b>\n\nВыбери фильтр отзывов:",
                reply_markup=filter_keyboard(),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
        await callback.answer()
        return

    if action == "confirm":
        if not selected:
            await callback.answer("⚠️ Отметь хотя бы одну звезду.", show_alert=True)
            return
        stars = sorted(selected)
        label = "Звёзды: " + ", ".join(str(s) for s in stars)
        file_label = manual_file_label(stars)
        await state.update_data(star_filter=stars, filter_label=label, file_label=file_label)
        try:
            await callback.message.edit_text(
                f"📦 Артикул: <b>{article}</b>\n🔍 Фильтр: {label}\n\n⏳ Начинаю сбор…",
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass
        await callback.answer()
        await state.set_state(CollectState.collecting)
        await _start_collection_task(
            callback.from_user.id, callback.message, state,
            article, stars, label, file_label,
        )
        return

    # Переключение звезды
    try:
        star_num = int(action)
    except ValueError:
        await callback.answer()
        return
    if star_num in selected:
        selected.discard(star_num)
    else:
        selected.add(star_num)

    await state.update_data(manual_stars=selected)
    try:
        await callback.message.edit_reply_markup(
            reply_markup=manual_star_keyboard(selected)
        )
    except TelegramBadRequest:
        pass
    await callback.answer()


# ─── Запуск сбора как отдельной задачи (чтобы его можно было отменить) ───────

async def _start_collection_task(
    user_id: int, message: Message, state: FSMContext,
    article: str, stars: Optional[list[int]],
    filter_label: str, file_label: str,
) -> None:
    # Если у пользователя уже есть активная задача — отменяем её
    prev = _active_tasks.get(user_id)
    if prev and not prev.done():
        prev.cancel()

    task = asyncio.create_task(
        _run_collection_safe(user_id, message, state,
                             article, stars, filter_label, file_label)
    )
    _active_tasks[user_id] = task


async def _run_collection_safe(
    user_id: int, message: Message, state: FSMContext,
    article: str, stars: Optional[list[int]],
    filter_label: str, file_label: str,
) -> None:
    try:
        await asyncio.wait_for(
            _run_collection(user_id, message, state,
                            article, stars, filter_label, file_label),
            timeout=COLLECTION_TIMEOUT,
        )
    except asyncio.CancelledError:
        logger.info(f"Сбор отменён пользователем user_id={user_id}")
        await message.answer("🛑 Сбор отменён.")
    except asyncio.TimeoutError:
        logger.warning(f"Сбор превысил {COLLECTION_TIMEOUT} сек, user_id={user_id}")
        await message.answer(
            f"⏱ Сбор длился слишком долго (>{COLLECTION_TIMEOUT // 60} мин) и был остановлен."
        )
    except Exception as e:
        logger.exception(f"Непойманная ошибка в _run_collection: {e}")
        await message.answer("❌ Внутренняя ошибка. Попробуй ещё раз.")
    finally:
        await state.clear()
        _active_tasks.pop(user_id, None)


# ─── Основная логика сбора ───────────────────────────────────────────────────

async def _safe_edit(msg: Message, text: str, **kwargs) -> None:
    """Редактирование сообщения с защитой от FloodWait и «message is not modified»."""
    try:
        await msg.edit_text(text, **kwargs)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await msg.edit_text(text, **kwargs)
        except Exception:
            pass
    except TelegramBadRequest:
        # Например, текст не изменился — игнор
        pass
    except Exception as e:
        logger.warning(f"edit_text failed: {e}")


async def _run_collection(
    user_id: int, message: Message, state: FSMContext,
    article: str, stars: Optional[list[int]],
    filter_label: str, file_label: str,
) -> None:
    connector = aiohttp.TCPConnector(limit=10, ttl_dns_cache=300)
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=15, sock_read=30)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # --- Подготовка: считаем ожидаемое кол-во С УЧЁТОМ фильтра ---
        try:
            total_approx = await count_total_reviews(session, article, star_filter=stars)
            overall_rating = await get_overall_rating(session, article)
        except WBNotFound:
            await message.answer(
                "❌ Товар не найден на Wildberries.\n"
                "Проверь артикул или ссылку и попробуй ещё раз."
            )
            return
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"Сетевая ошибка на этапе подготовки: {e}")
            await message.answer(
                "❌ Не удалось связаться с Wildberries. Попробуй через минуту."
            )
            return

        # --- Прогресс-сообщение с кнопкой отмены ---
        progress_msg = await message.answer(
            f"🔄 Собираю отзывы…\nСобрано: <b>0</b> из ~<b>{total_approx or '?'}</b>",
            parse_mode="HTML",
            reply_markup=collecting_keyboard(),
        )

        # --- Сбор отзывов ---
        reviews: list[dict] = []
        last_progress_edit = 0.0
        try:
            async for page in fetch_reviews(session, article, stars):
                reviews.extend(page)
                now = time.monotonic()
                if now - last_progress_edit >= PROGRESS_EDIT_INTERVAL:
                    last_progress_edit = now
                    await _safe_edit(
                        progress_msg,
                        f"🔄 Собираю отзывы…\n"
                        f"Собрано: <b>{len(reviews)}</b> из ~<b>{total_approx or '?'}</b>",
                        parse_mode="HTML",
                        reply_markup=collecting_keyboard(),
                    )
        except WBNotFound:
            await message.answer("❌ Товар не найден.")
            return
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"Сетевая ошибка при сборе отзывов: {e}")
            await message.answer(
                "⚠️ WB ответил ошибкой на середине сбора. "
                f"Собрано частично: {len(reviews)} отзывов."
            )
        except WBError as e:
            logger.warning(f"WB error: {e}")

        # --- Сбор вопросов ---
        await _safe_edit(
            progress_msg,
            f"✅ Отзывов собрано: <b>{len(reviews)}</b>\n🔄 Собираю вопросы…",
            parse_mode="HTML",
            reply_markup=collecting_keyboard(),
        )

        questions: list[dict] = []
        try:
            async for page in fetch_questions(session, article):
                questions.extend(page)
        except WBNotFound:
            pass  # вопросов нет — не беда
        except (aiohttp.ClientError, asyncio.TimeoutError) as e:
            logger.warning(f"Сетевая ошибка при сборе вопросов: {e}")

    # --- Средние рейтинги ---
    overall_rating_val = overall_rating or 0.0
    if reviews:
        grades = [r.get("Оценка", 0) for r in reviews if r.get("Оценка")]
        selection_rating = round(sum(grades) / len(grades), 2) if grades else 0.0
    else:
        selection_rating = 0.0

    # --- Сохранение файлов ---
    await _safe_edit(progress_msg, "💾 Сохраняю файлы…", parse_mode="HTML")

    try:
        reviews_path = save_reviews_xlsx(reviews, article, file_label)
        questions_path = save_questions_xlsx(questions, article)
        archive_path = create_archive(reviews_path, questions_path, article)
    except Exception as e:
        logger.exception(f"Ошибка при создании файлов: {e}")
        await message.answer("❌ Ошибка при создании Excel-файлов. Попробуй ещё раз.")
        return

    # --- История ---
    try:
        await save_run(
            user_id=user_id,
            article=article,
            filter_label=filter_label,
            reviews_count=len(reviews),
            questions_count=len(questions),
            avg_rating=overall_rating_val,
            reviews_file=reviews_path,
            questions_file=questions_path,
            archive_file=archive_path,
        )
    except Exception as e:
        logger.exception(f"Не удалось сохранить историю: {e}")

    # --- Сводка ---
    summary_parts = [
        "✅ <b>Сбор завершён!</b>\n",
        f"📦 Артикул: <b>{article}</b>",
        f"🔍 Фильтр: {filter_label}",
        "",
        f"📊 Отзывов собрано: <b>{len(reviews)}</b>",
        f"❓ Вопросов собрано: <b>{len(questions)}</b>",
        f"⭐ Средний рейтинг товара: <b>{overall_rating_val or '—'}</b>",
    ]
    if stars and reviews:
        summary_parts.append(f"📈 Средний рейтинг выборки: <b>{selection_rating}</b>")
    summary_parts.append("\n📎 Отправляю файлы…")

    summary = "\n".join(summary_parts)
    await _safe_edit(progress_msg, summary, parse_mode="HTML")

    # --- Отправка файлов ---
    if reviews and os.path.exists(reviews_path):
        await message.answer_document(
            FSInputFile(reviews_path),
            caption=f"📊 Отзывы — {article} ({filter_label})",
        )

    if questions and os.path.exists(questions_path):
        await message.answer_document(
            FSInputFile(questions_path),
            caption=f"❓ Вопросы — {article}",
        )

    if os.path.exists(archive_path) and (reviews or questions):
        await message.answer_document(
            FSInputFile(archive_path),
            caption=f"📦 Архив — {article}",
        )

    if not reviews and not questions:
        await message.answer(
            "⚠️ По этому артикулу и фильтру не найдено ни отзывов, ни вопросов.\n"
            "Проверь артикул или попробуй другой фильтр."
        )
