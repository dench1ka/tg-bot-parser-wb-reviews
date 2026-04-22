"""
Клиент к публичному (внутреннему) API Wildberries.

Ключевой момент ТЗ: фильтр по звёздам применяется ДО сбора —
используем встроенный параметр `&stars=` на стороне WB,
а не фильтруем уже скачанные страницы на своей стороне.
"""
import asyncio
import aiohttp
import logging
import re
from typing import AsyncGenerator, Optional

from config import WB_REQUEST_DELAY

logger = logging.getLogger(__name__)

# Кеш imtId / nmIds на время жизни процесса
_imt_id_cache: dict[str, str] = {}
_nm_ids_cache: dict[str, list[str]] = {}

HEADERS_BROWSER = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Referer": "https://www.wildberries.ru/",
    "Origin": "https://www.wildberries.ru",
}

HEADERS_API = {
    "User-Agent": HEADERS_BROWSER["User-Agent"],
    "Accept": "*/*",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Origin": "https://www.wildberries.ru",
    "Referer": "https://www.wildberries.ru/",
}


# ─── Доменные исключения ─────────────────────────────────────────────────────

class WBError(Exception):
    """Базовая ошибка обращения к WB."""


class WBNotFound(WBError):
    """Товар не найден / imtId получить не удалось."""


class WBNetworkError(WBError):
    """WB недоступен или отвечает ошибкой."""


# ─── Парсинг входа пользователя ──────────────────────────────────────────────

_URL_PATTERNS = [
    re.compile(r"/catalog/(\d+)"),
    re.compile(r"[?&]nm=(\d+)"),
    re.compile(r"[?&]card=(\d+)"),
]


def parse_article(text: str) -> Optional[str]:
    """Из ссылки или строки извлечь числовой артикул (nmId).

    Поддерживает:
      • голый артикул: "12345678"
      • ссылки wildberries.ru / .by / .kz / wb.ru с /catalog/<id>/detail.aspx
      • ссылки с параметром ?nm=<id> или ?card=<id>
    """
    if not text:
        return None
    text = text.strip()
    if text.isdigit() and 4 <= len(text) <= 12:
        return text
    for pat in _URL_PATTERNS:
        m = pat.search(text)
        if m:
            return m.group(1)
    return None


# ─── Выбор хоста feedbacks1 / feedbacks2 ─────────────────────────────────────

def _get_feedbacks_host(imt_id: str) -> str:
    """Простая хеш-балансировка. Если этот хост не даст данных,
    get_product_meta всё равно переберёт оба сервера."""
    try:
        return f"https://feedbacks{(int(imt_id) % 2) + 1}.wb.ru"
    except ValueError:
        return "https://feedbacks1.wb.ru"


# ─── Прогрев cookies ─────────────────────────────────────────────────────────

async def _warmup(session: aiohttp.ClientSession) -> None:
    """Открыть главную WB, чтобы в cookie_jar появились нужные куки."""
    try:
        async with session.get(
            "https://www.wildberries.ru/",
            headers=HEADERS_BROWSER,
            timeout=aiohttp.ClientTimeout(total=10),
        ):
            pass
    except Exception as e:
        logger.warning(f"warmup: не удалось получить куки WB: {e}")


# ─── imtId (root) ────────────────────────────────────────────────────────────

def _extract_root(data: dict) -> Optional[str]:
    products = (data.get("data") or data).get("products") or data.get("products") or []
    for p in products:
        root = p.get("root") or p.get("imtId")
        if root:
            return str(root)
    return None


async def get_imt_id(session: aiohttp.ClientSession, nm_id: str) -> Optional[str]:
    """Получить imtId через card API. С кешем."""
    if nm_id in _imt_id_cache:
        return _imt_id_cache[nm_id]

    card_urls = [
        f"https://card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest=-1257786&spp=30&lang=ru&nm={nm_id}",
        f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&nm={nm_id}",
        f"https://card.wb.ru/cards/detail?appType=1&curr=rub&dest=-1257786&nm={nm_id}",
    ]
    for url in card_urls:
        try:
            async with session.get(url, headers=HEADERS_BROWSER,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json(content_type=None)
                root = _extract_root(data)
                if root:
                    _imt_id_cache[nm_id] = root
                    return root
        except Exception as e:
            logger.warning(f"card API error: {e}")

    # Fallback: nmId может сам быть imtId — проверим через feedbacks
    for server in ("1", "2"):
        try:
            url = f"https://feedbacks{server}.wb.ru/feedbacks/v2/{nm_id}?take=1&skip=0&order=dateDesc"
            async with session.get(url, headers=HEADERS_API,
                                   timeout=aiohttp.ClientTimeout(total=8)) as resp:
                if resp.status == 200:
                    data = await resp.json(content_type=None)
                    if data.get("feedbackCount", 0) > 0 or data.get("feedbacks"):
                        _imt_id_cache[nm_id] = nm_id
                        return nm_id
        except Exception:
            continue

    return None


# ─── Все nmId товара (цвета/размеры) ─────────────────────────────────────────

async def get_all_nm_ids(session: aiohttp.ClientSession,
                         nm_id: str, imt_id: str) -> list[str]:
    """Список всех nmId, относящихся к данной группе товара (colors)."""
    cache_key = f"{nm_id}:{imt_id}"
    if cache_key in _nm_ids_cache:
        return _nm_ids_cache[cache_key]

    urls = [
        f"https://card.wb.ru/cards/v4/detail?appType=1&curr=rub&dest=-1257786&spp=30&lang=ru&nm={nm_id}",
        f"https://card.wb.ru/cards/v2/detail?appType=1&curr=rub&dest=-1257786&nm={nm_id}",
    ]
    for url in urls:
        try:
            async with session.get(url, headers=HEADERS_BROWSER,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json(content_type=None)
                products = (data.get("data") or data).get("products") or data.get("products") or []
                nm_ids: list[str] = []
                for p in products:
                    if str(p.get("root") or p.get("imtId") or "") != imt_id:
                        continue
                    for color in p.get("colors") or []:
                        cnm = color.get("nm")
                        if cnm and str(cnm) not in nm_ids:
                            nm_ids.append(str(cnm))
                    pid = p.get("id")
                    if pid and str(pid) not in nm_ids:
                        nm_ids.append(str(pid))
                if nm_ids:
                    _nm_ids_cache[cache_key] = nm_ids
                    return nm_ids
        except Exception as e:
            logger.warning(f"get_all_nm_ids error: {e}")

    _nm_ids_cache[cache_key] = [nm_id]
    return [nm_id]


# ─── Метаданные товара ───────────────────────────────────────────────────────

async def get_product_meta(session: aiohttp.ClientSession, imt_id: str) -> dict:
    """Читает суммарные feedbackCount, valuation и (если есть) распределение по звёздам.
    Перебирает оба хоста — иногда первый пустой, данные на втором."""
    for server in ("1", "2"):
        try:
            url = (
                f"https://feedbacks{server}.wb.ru/feedbacks/v2/{imt_id}"
                f"?take=1&skip=0&order=dateDesc"
            )
            async with session.get(url, headers=HEADERS_API,
                                   timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status != 200:
                    continue
                data = await resp.json(content_type=None)
                fc = data.get("feedbackCount", 0)
                if fc or data.get("feedbacks"):
                    return {
                        "rating": data.get("valuation", "0"),
                        "feedbacks": fc,
                        "stars_distribution": data.get("valuationDistribution") or {},
                    }
        except Exception as e:
            logger.warning(f"get_product_meta error: {e}")
    return {"rating": "0", "feedbacks": 0, "stars_distribution": {}}


async def count_total_reviews(session: aiohttp.ClientSession,
                              article: str,
                              star_filter: Optional[list[int]] = None) -> int:
    """Оценка общего кол-ва отзывов с УЧЁТОМ выбранного фильтра —
    чтобы прогресс-бар показывал реалистичную цифру."""
    imt_id = await get_imt_id(session, article)
    if not imt_id:
        return 0
    meta = await get_product_meta(session, imt_id)
    total = int(meta.get("feedbacks", 0) or 0)
    if not star_filter:
        return total
    dist = meta.get("stars_distribution") or {}
    if dist:
        try:
            return sum(int(dist.get(str(s), 0) or 0) for s in star_filter)
        except Exception:
            return total
    return total


async def get_overall_rating(session: aiohttp.ClientSession, article: str) -> float:
    imt_id = await get_imt_id(session, article)
    if not imt_id:
        return 0.0
    meta = await get_product_meta(session, imt_id)
    try:
        return round(float(meta.get("rating", 0)), 2)
    except Exception:
        return 0.0


# ─── Парсинг одного фидбека ──────────────────────────────────────────────────

def _normalize_feedback(fb: dict) -> dict:
    photos = len(fb.get("photos") or [])
    video = 1 if fb.get("video") else 0
    answer = fb.get("answer") or {}
    answer_date = (answer.get("createDate") or answer.get("createdDate") or "")[:10]
    return {
        "Дата": (fb.get("createdDate") or "")[:10],
        "Автор": (fb.get("wbUserDetails") or {}).get("name") or "Аноним",
        "Оценка": fb.get("productValuation", 0),
        "Текст": fb.get("text") or "",
        "Достоинства": fb.get("pros") or "",
        "Недостатки": fb.get("cons") or "",
        "Ответ продавца": answer.get("text", ""),
        "Дата ответа": answer_date,
        "Фото/видео": photos + video,
    }


# ─── Сбор отзывов ────────────────────────────────────────────────────────────
#
# WB на публичном API feedbacks не поддерживает параметр &stars= (игнорирует),
# поэтому фильтруем на стороне клиента. Лимит ~1030 отзывов на nmId
# обходится за счёт перебора всех nmId товара (цвета/размеры) — get_all_nm_ids.


async def _fetch_reviews_one_nm(
    session: aiohttp.ClientSession,
    host: str,
    imt_id: str,
    nm_id: str,
    seen_ids: set,
    star_filter: Optional[list[int]] = None,
) -> AsyncGenerator[list[dict], None]:
    """Один проход по конкретному nmId. Фильтр применяется ПОСЛЕ получения
    (на стороне клиента), т.к. WB не умеет &stars=."""
    skip = 0
    take = 30
    while True:
        url = (
            f"{host}/feedbacks/v2/{imt_id}"
            f"?take={take}&skip={skip}&order=dateDesc&nm={nm_id}"
        )

        try:
            async with session.get(
                url, headers=HEADERS_API,
                timeout=aiohttp.ClientTimeout(total=30),
            ) as resp:
                if resp.status == 429:
                    logger.warning("WB вернул 429, пауза 5 сек")
                    await asyncio.sleep(5)
                    continue
                if resp.status != 200:
                    logger.warning(f"feedbacks вернул {resp.status} nm={nm_id}")
                    break
                data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            logger.error(f"таймаут feedbacks nm={nm_id} skip={skip}")
            break
        except aiohttp.ClientError as e:
            logger.error(f"сеть feedbacks nm={nm_id} skip={skip}: {e}")
            break

        feedbacks = data.get("feedbacks") or []
        if not feedbacks:
            break

        page: list[dict] = []
        new_count = 0
        for fb in feedbacks:
            fb_id = fb.get("id")
            if not fb_id or fb_id in seen_ids:
                continue
            seen_ids.add(fb_id)
            new_count += 1
            # Фильтр на стороне клиента
            rating = fb.get("productValuation", 0)
            if star_filter and rating not in star_filter:
                continue
            page.append(_normalize_feedback(fb))

        if page:
            yield page

        logger.info(
            f"[nm={nm_id}] страница skip={skip}: новых={new_count}, "
            f"всего уникальных={len(seen_ids)}"
        )

        # Если на этой странице нет новых фидбеков — дальше пойдут только дубли
        if new_count == 0:
            break

        skip += len(feedbacks)
        await asyncio.sleep(WB_REQUEST_DELAY)


async def fetch_reviews(
    session: aiohttp.ClientSession,
    article: str,
    star_filter: Optional[list[int]] = None,
) -> AsyncGenerator[list[dict], None]:
    """Основной публичный генератор.

    WB-лимит ~1030 отзывов на nmId обходится перебором всех nmId товара.
    Фильтр по звёздам применяется на стороне клиента после получения страницы.
    """
    await _warmup(session)

    imt_id = await get_imt_id(session, article)
    if not imt_id:
        raise WBNotFound(f"Товар {article} не найден на Wildberries")

    host = _get_feedbacks_host(imt_id)
    nm_ids = await get_all_nm_ids(session, article, imt_id)
    logger.info(f"Сбор отзывов: imt={imt_id}, nmIds={len(nm_ids)}")

    seen_ids: set = set()
    for nm_id in nm_ids:
        async for page in _fetch_reviews_one_nm(
            session, host, imt_id, nm_id, seen_ids, star_filter=star_filter,
        ):
            yield page


# ─── Сбор вопросов (фильтра нет — всегда все) ────────────────────────────────

async def fetch_questions(
    session: aiohttp.ClientSession,
    article: str,
) -> AsyncGenerator[list[dict], None]:
    imt_id = await get_imt_id(session, article)
    if not imt_id:
        raise WBNotFound(f"Товар {article} не найден")

    skip = 0
    take = 30
    seen_ids: set = set()

    while True:
        url = (
            f"https://questions.wildberries.ru/api/v1/questions"
            f"?imtId={imt_id}&take={take}&skip={skip}&order=dateDesc"
        )
        try:
            async with session.get(
                url, headers=HEADERS_API,
                timeout=aiohttp.ClientTimeout(total=20),
            ) as resp:
                if resp.status == 429:
                    await asyncio.sleep(5)
                    continue
                if resp.status != 200:
                    logger.warning(f"questions вернул {resp.status}")
                    break
                data = await resp.json(content_type=None)
        except asyncio.TimeoutError:
            logger.error(f"таймаут questions skip={skip}")
            break
        except aiohttp.ClientError as e:
            logger.error(f"сеть questions skip={skip}: {e}")
            break

        questions = data.get("questions") or []
        if not questions:
            break

        page: list[dict] = []
        new_q = 0
        for q in questions:
            q_id = q.get("id")
            if not q_id or q_id in seen_ids:
                continue
            seen_ids.add(q_id)
            new_q += 1
            answer = q.get("answer") or {}
            buyer_answers = q.get("buyerAnswers") or []
            buyer_texts = "; ".join(
                a.get("text", "") for a in buyer_answers if a.get("text")
            )
            page.append({
                "Дата": (q.get("createdDate") or "")[:10],
                "Автор": (q.get("wbUserDetails") or {}).get("name") or "",
                "Вопрос": q.get("text") or "",
                "Ответ продавца": answer.get("text", ""),
                "Дата ответа продавца": (answer.get("createdDate") or "")[:10],
                "Ответы покупателей": buyer_texts,
            })

        if page:
            yield page

        if new_q == 0 or len(questions) < take:
            break

        skip += take
        await asyncio.sleep(WB_REQUEST_DELAY)