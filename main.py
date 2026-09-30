import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

from playwright.async_api import (
    Browser,
    Page,
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from telethon import TelegramClient
from telethon.sessions import StringSession


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# CONFIGURATION
# ============================================================

# Не преобразуем API ID в int при загрузке файла.
# Это позволяет вывести понятную ошибку Railway.
TELEGRAM_API_ID_RAW = os.getenv(
    "TELEGRAM_API_ID",
    "",
).strip()

TELEGRAM_API_HASH = os.getenv(
    "TELEGRAM_API_HASH",
    "",
).strip()

TELEGRAM_SESSION = os.getenv(
    "TELEGRAM_SESSION",
    "",
).strip()

TELEGRAM_TARGET = os.getenv(
    "TELEGRAM_TARGET",
    "@fwd19472",
).strip()

SECTIONS = [
    "https://jobatsea.online/jobs/Engine_Officers/",
    "https://jobatsea.online/jobs/Engine_Ratings/",
    "https://jobatsea.online/jobs/Deck_Officers/",
    "https://jobatsea.online/jobs/Deck_Ratings/",
    "https://jobatsea.online/jobs/Catering_Staff/",
    "https://jobatsea.online/jobs/Offshore/",
]

# Для Railway лучше использовать абсолютный путь.
# Railway не удаляет файл при обычном перезапуске контейнера,
# однако файл не является постоянным хранилищем при полном redeploy.
SENT_FILE = Path(
    os.getenv(
        "SENT_FILE",
        "/app/sent_jobs.json",
    )
)

SCAN_INTERVAL_SECONDS = 3600
REQUEST_DELAY_SECONDS = 2
PAGE_DELAY_SECONDS = 1
PAGE_TIMEOUT_MILLISECONDS = 60_000

# Ограничение длины сообщения Telegram.
MAX_TELEGRAM_MESSAGE_LENGTH = 3900

# Защита от бесконечной пагинации при ошибке сайта.
MAX_PAGES_PER_SECTION = 1000

EMAIL_REGEX = re.compile(
    r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b"
)

# Основной селектор ссылок на вакансии.
JOB_LINK_SELECTOR = "a[href*='/job/']"


# ============================================================
# CONFIG VALIDATION
# ============================================================

def validate_configuration() -> int:
    """
    Проверяет переменные Railway и возвращает API ID как число.
    """

    if not TELEGRAM_API_ID_RAW:
        raise ValueError(
            "Не задана переменная TELEGRAM_API_ID "
            "в Railway Variables"
        )

    try:
        api_id = int(TELEGRAM_API_ID_RAW)
    except ValueError as error:
        raise ValueError(
            "TELEGRAM_API_ID должен быть числом"
        ) from error

    if api_id <= 0:
        raise ValueError(
            "TELEGRAM_API_ID должен быть положительным числом"
        )

    if not TELEGRAM_API_HASH:
        raise ValueError(
            "Не задана переменная TELEGRAM_API_HASH "
            "в Railway Variables"
        )

    if not TELEGRAM_SESSION:
        raise ValueError(
            "Не задана переменная TELEGRAM_SESSION "
            "в Railway Variables"
        )

    if not TELEGRAM_TARGET:
        raise ValueError(
            "Не задана переменная TELEGRAM_TARGET"
        )

    return api_id


# ============================================================
# MEMORY
# ============================================================

def load_memory() -> set[str]:
    """
    Загружает ссылки на вакансии, которые уже были отправлены.
    """

    if not SENT_FILE.exists():
        return set()

    try:
        content = SENT_FILE.read_text(
            encoding="utf-8",
        )

        data = json.loads(content)

        if not isinstance(data, list):
            logger.warning(
                "Файл %s имеет неправильный формат",
                SENT_FILE,
            )
            return set()

        return {
            str(item).strip()
            for item in data
            if str(item).strip()
        }

    except json.JSONDecodeError as error:
        logger.warning(
            "Файл %s содержит некорректный JSON: %s",
            SENT_FILE,
            error,
        )
        return set()

    except OSError as error:
        logger.warning(
            "Не удалось прочитать файл памяти: %s",
            error,
        )
        return set()


def save_memory(sent_jobs: set[str]) -> None:
    """
    Безопасно сохраняет ссылки на отправленные вакансии.
    """

    try:
        SENT_FILE.parent.mkdir(
            parents=True,
            exist_ok=True,
        )

        temporary_file = SENT_FILE.with_suffix(
            SENT_FILE.suffix + ".tmp"
        )

        temporary_file.write_text(
            json.dumps(
                sorted(sent_jobs),
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

        temporary_file.replace(SENT_FILE)

    except OSError as error:
        logger.error(
            "Не удалось сохранить файл памяти: %s",
            error,
        )


# ============================================================
# URL AND TEXT HELPERS
# ============================================================

def normalize_url(url: str) -> str:
    """
    Преобразует относительную ссылку в нормальную абсолютную ссылку.
    """

    url = url.strip()

    if not url:
        return ""

    parts = urlsplit(url)

    # Убираем завершающий slash из пути.
    path = parts.path.rstrip("/")

    return urlunsplit(
        (
            parts.scheme,
            parts.netloc,
            path,
            parts.query,
            parts.fragment,
        )
    )


def extract_emails(text: str) -> list[str]:
    """
    Извлекает уникальные e-mail-адреса из текста.
    """

    found_emails = EMAIL_REGEX.findall(text)

    result: list[str] = []
    seen: set[str] = set()

    for email in found_emails:
        clean_email = email.strip(
            ".,;:()[]{}<>\"'"
        ).lower()

        if clean_email and clean_email not in seen:
            seen.add(clean_email)
            result.append(clean_email)

    return result


def limit_message_length(
    text: str,
    max_length: int,
) -> str:
    """
    Ограничивает размер текста перед отправкой в Telegram.
    """

    text = text.strip()

    if len(text) <= max_length:
        return text

    return text[: max_length - 3].rstrip() + "..."


def create_telegram_message(
    job_text: str,
    emails: list[str],
) -> str:
    """
    Формирует сообщение без ссылки на вакансию.
    """

    # Убираем множественные пробелы и пустые строки.
    clean_text = " ".join(job_text.split())

    message = (
        "JobAtSea\n\n"
        f"{clean_text}\n\n"
        f"E-mail: {', '.join(emails)}"
    )

    return limit_message_length(
        message,
        MAX_TELEGRAM_MESSAGE_LENGTH,
    )


# ============================================================
# PLAYWRIGHT FUNCTIONS
# ============================================================

async def get_job_links(
    page: Page,
    section_url: str,
    page_number: int,
) -> list[str]:
    """
    Получает уникальные ссылки на вакансии со страницы раздела.
    """

    separator = "&" if "?" in section_url else "?"
    page_url = f"{section_url}{separator}p={page_number}"

    logger.info(
        "Сканирование страницы раздела: %s",
        page_url,
    )

    try:
        await page.goto(
            page_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MILLISECONDS,
        )

        await page.wait_for_timeout(1000)

    except PlaywrightTimeoutError:
        logger.warning(
            "Тайм-аут при открытии раздела: %s",
            page_url,
        )
        return []

    except Exception as error:
        logger.warning(
            "Ошибка при открытии раздела %s: %s",
            page_url,
            error,
        )
        return []

    try:
        links = await page.locator(
            JOB_LINK_SELECTOR
        ).evaluate_all(
            """
            elements => elements.map(element => element.href)
            """
        )

    except Exception as error:
        logger.warning(
            "Не удалось получить ссылки со страницы %s: %s",
            page_url,
            error,
        )
        return []

    result: list[str] = []
    seen: set[str] = set()

    for raw_link in links:
        absolute_link = urljoin(
            page_url,
            str(raw_link),
        )

        normalized_link = normalize_url(
            absolute_link,
        )

        if (
            normalized_link
            and normalized_link not in seen
            and "/job/" in normalized_link
        ):
            seen.add(normalized_link)
            result.append(normalized_link)

    return result


async def get_job_details(
    page: Page,
    job_url: str,
) -> Optional[dict]:
    """
    Открывает вакансию.

    Возвращает данные только в том случае,
    если на странице найден хотя бы один e-mail.
    """

    logger.info(
        "Проверка вакансии: %s",
        job_url,
    )

    try:
        await page.goto(
            job_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MILLISECONDS,
        )

        await page.wait_for_timeout(1000)

        body = page.locator("body")

        if await body.count() == 0:
            logger.warning(
                "На странице отсутствует body: %s",
                job_url,
            )
            return None

        text = await body.inner_text()

    except PlaywrightTimeoutError:
        logger.warning(
            "Тайм-аут при открытии вакансии: %s",
            job_url,
        )
        return None

    except Exception as error:
        logger.warning(
            "Ошибка при открытии вакансии %s: %s",
            job_url,
            error,
        )
        return None

    text = text.strip()

    if not text:
        logger.info(
            "Пустой текст вакансии: %s",
            job_url,
        )
        return None

    emails = extract_emails(text)

    if not emails:
        logger.info(
            "E-mail не найден, вакансия пропущена: %s",
            job_url,
        )
        return None

    return {
        "text": text,
        "emails": emails,
    }


# ============================================================
# SCANNING
# ============================================================

async def scan_section(
    page: Page,
    section_url: str,
    sent_jobs: set[str],
    client: TelegramClient,
) -> None:
    """
    Обходит страницы одного раздела.
    """

    page_number = 1

    while page_number <= MAX_PAGES_PER_SECTION:
        job_links = await get_job_links(
            page=page,
            section_url=section_url,
            page_number=page_number,
        )

        if not job_links:
            logger.info(
                "Ссылки не найдены. Раздел завершён: %s",
                section_url,
            )
            break

        logger.info(
            "На странице %s найдено ссылок: %s",
            page_number,
            len(job_links),
        )

        for job_link in job_links:
            if job_link in sent_jobs:
                logger.info(
                    "Вакансия уже отправлялась: %s",
                    job_link,
                )
                continue

            job_data = await get_job_details(
                page=page,
                job_url=job_link,
            )

            if job_data is None:
                # Вакансия без e-mail не добавляется в память.
                # Поэтому при следующем цикле она будет проверена снова.
                continue

            message = create_telegram_message(
                job_text=job_data["text"],
                emails=job_data["emails"],
            )

            try:
                await client.send_message(
                    entity=TELEGRAM_TARGET,
                    message=message,
                    link_preview=False,
                )

                sent_jobs.add(job_link)
                save_memory(sent_jobs)

                logger.info(
                    "Вакансия отправлена в %s: %s",
                    TELEGRAM_TARGET,
                    job_link,
                )

            except Exception as error:
                logger.error(
                    "Ошибка отправки вакансии %s: %s",
                    job_link,
                    error,
                )

            await asyncio.sleep(
                REQUEST_DELAY_SECONDS,
            )

        page_number += 1

        await asyncio.sleep(
            PAGE_DELAY_SECONDS,
        )

    else:
        logger.warning(
            "Достигнут лимит страниц %s для раздела %s",
            MAX_PAGES_PER_SECTION,
            section_url,
        )


# ============================================================
# BROWSER
# ============================================================

async def launch_browser(playwright) -> Browser:
    """
    Запускает Chromium с параметрами для Railway/Docker.
    """

    return await playwright.chromium.launch(
        headless=True,
        args=[
            "--no-sandbox",
            "--disable-setuid-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
        ],
    )


# ============================================================
# MAIN
# ============================================================

async def main() -> None:
    """
    Основная функция приложения.
    """

    api_id = validate_configuration()

    sent_jobs = load_memory()

    logger.info(
        "Загружено отправленных вакансий: %s",
        len(sent_jobs),
    )

    client = TelegramClient(
        StringSession(TELEGRAM_SESSION),
        api_id,
        TELEGRAM_API_HASH,
    )

    try:
        logger.info("Подключение к Telegram...")

        await client.start()

        logger.info(
            "Telegram подключён. Получатель: %s",
            TELEGRAM_TARGET,
        )

        async with async_playwright() as playwright:
            browser = await launch_browser(playwright)

            try:
                page = await browser.new_page(
                    viewport={
                        "width": 1280,
                        "height": 900,
                    },
                    user_agent=(
                        "Mozilla/5.0 (X11; Linux x86_64) "
                        "AppleWebKit/537.36 "
                        "(KHTML, like Gecko) "
                        "Chrome/120.0 Safari/537.36"
                    ),
                )

                page.set_default_timeout(
                    PAGE_TIMEOUT_MILLISECONDS,
                )

                while True:
                    logger.info(
                        "Начинается новый цикл сканирования"
                    )

                    for section_url in SECTIONS:
                        try:
                            await scan_section(
                                page=page,
                                section_url=section_url,
                                sent_jobs=sent_jobs,
                                client=client,
                            )

                        except Exception as error:
                            logger.exception(
                                "Ошибка при обработке раздела %s: %s",
                                section_url,
                                error,
                            )

                    logger.info(
                        "Сканирование завершено. "
                        "Следующий запуск через %s секунд.",
                        SCAN_INTERVAL_SECONDS,
                    )

                    await asyncio.sleep(
                        SCAN_INTERVAL_SECONDS,
                    )

            finally:
                await browser.close()

    finally:
        await client.disconnect()


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == "__main__":
    try:
        asyncio.run(main())

    except KeyboardInterrupt:
        logger.info(
            "Программа остановлена пользователем"
        )

    except Exception as error:
        logger.exception(
            "Критическая ошибка приложения: %s",
            error,
        )
        raise
