import asyncio
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse

from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from telethon import TelegramClient
from telethon.sessions import StringSession


# ============================================================
# НАСТРОЙКИ
# ============================================================

TELEGRAM_API_ID_RAW = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "").strip()
TELEGRAM_TARGET = os.getenv("TELEGRAM_TARGET", "@fwd19472").strip()

SCAN_INTERVAL_SECONDS = 3600
REQUEST_DELAY_SECONDS = 2
PAGE_DELAY_SECONDS = 1

SENT_FILE = Path("sent_jobs.json")

SECTIONS = [
    "https://jobatsea.online/jobs/Engine_Officers/",
    "https://jobatsea.online/jobs/Engine_Ratings/",
    "https://jobatsea.online/jobs/Deck_Officers/",
    "https://jobatsea.online/jobs/Deck_Ratings/",
    "https://jobatsea.online/jobs/Catering_Staff/",
    "https://jobatsea.online/jobs/Offshore/",
]

EMAIL_REGEX = re.compile(
    r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+"
)

# Ссылки на вакансии имеют вид /job/622499/...
JOB_LINK_SELECTOR = 'a[href*="/job/"]'


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# КОНФИГУРАЦИЯ
# ============================================================

def validate_configuration() -> int:
    if not TELEGRAM_API_ID_RAW:
        raise ValueError(
            "Не задана переменная TELEGRAM_API_ID в Railway Variables"
        )

    if not TELEGRAM_API_HASH:
        raise ValueError(
            "Не задана переменная TELEGRAM_API_HASH в Railway Variables"
        )

    if not TELEGRAM_SESSION:
        raise ValueError(
            "Не задана переменная TELEGRAM_SESSION в Railway Variables"
        )

    try:
        api_id = int(TELEGRAM_API_ID_RAW)
    except ValueError as exc:
        raise ValueError(
            "TELEGRAM_API_ID должен содержать только цифры"
        ) from exc

    if api_id <= 0:
        raise ValueError("TELEGRAM_API_ID должен быть положительным числом")

    if not TELEGRAM_TARGET:
        raise ValueError("Не задан получатель Telegram")

    return api_id


# ============================================================
# ПАМЯТЬ ОБ ОТПРАВЛЕННЫХ ВАКАНСИЯХ
# ============================================================

def load_memory() -> set[str]:
    if not SENT_FILE.exists():
        logger.info("Файл sent_jobs.json отсутствует. Начинаем с пустой памяти.")
        return set()

    try:
        data = json.loads(SENT_FILE.read_text(encoding="utf-8"))

        if not isinstance(data, list):
            logger.warning(
                "Файл sent_jobs.json имеет неправильный формат. "
                "Начинаем с пустой памяти."
            )
            return set()

        sent_jobs = {str(item) for item in data}
        logger.info(
            "Загружено отправленных вакансий: %d",
            len(sent_jobs),
        )
        return sent_jobs

    except Exception as exc:
        logger.warning(
            "Не удалось загрузить sent_jobs.json: %s. "
            "Начинаем с пустой памяти.",
            exc,
        )
        return set()


def save_memory(sent_jobs: set[str]) -> None:
    temporary_file = SENT_FILE.with_suffix(".tmp")

    temporary_file.write_text(
        json.dumps(
            sorted(sent_jobs),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    temporary_file.replace(SENT_FILE)


# ============================================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ============================================================

def normalize_url(url: str) -> str:
    """
    Приводит ссылку к абсолютному виду и удаляет fragment (#...).
    """
    absolute_url = urljoin("https://jobatsea.online/", url)
    parsed = urlparse(absolute_url)

    return parsed._replace(fragment="").geturl()


def is_job_url(url: str) -> bool:
    """
    Проверяет, что ссылка относится именно к странице вакансии:
    /job/622499/...
    """
    parsed = urlparse(url)

    if parsed.netloc and parsed.netloc != "jobatsea.online":
        return False

    return bool(re.search(r"/job/\d+(?:/|$)", parsed.path))


def extract_emails(text: str) -> list[str]:
    found = EMAIL_REGEX.findall(text)

    unique_emails = []
    seen = set()

    for email in found:
        normalized_email = email.strip(".,;:()[]<>").lower()

        if normalized_email not in seen:
            seen.add(normalized_email)
            unique_emails.append(normalized_email)

    return unique_emails


def clean_job_text(text: str) -> str:
    """
    Убирает лишние пустые строки и ограничивает размер сообщения.
    """
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    result = "\n".join(lines)

    # Ограничение с запасом под подпись и служебный текст Telegram.
    return result[:8000]


# ============================================================
# ПОЛУЧЕНИЕ ДАННЫХ ВАКАНСИИ
# ============================================================

async def get_job_details(page, job_url: str) -> dict | None:
    logger.info("Проверка вакансии: %s", job_url)

    try:
        await page.goto(
            job_url,
            wait_until="domcontentloaded",
            timeout=60_000,
        )

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=10_000,
            )
        except PlaywrightTimeoutError:
            # Некоторые сайты не переходят в networkidle из-за фоновых запросов.
            pass

        await page.wait_for_timeout(500)

        page_text = await page.locator("body").inner_text()
        page_text = clean_job_text(page_text)

        emails = extract_emails(page_text)

        # Вакансии без e-mail пропускаем.
        if not emails:
            logger.info(
                "Вакансия пропущена: e-mail не найден: %s",
                job_url,
            )
            return None

        return {
            "text": page_text,
            "emails": emails,
        }

    except PlaywrightTimeoutError:
        logger.exception(
            "Тайм-аут при открытии вакансии: %s",
            job_url,
        )
        return None

    except Exception:
        logger.exception(
            "Ошибка при обработке вакансии: %s",
            job_url,
        )
        return None


# ============================================================
# СКАНИРОВАНИЕ РАЗДЕЛА
# ============================================================

async def scan_section(
    page,
    section_url: str,
    sent_jobs: set[str],
    client: TelegramClient,
) -> None:
    page_num = 1

    while True:
        list_url = f"{section_url}?p={page_num}"

        logger.info(
            "Сканирование страницы раздела: %s",
            list_url,
        )

        try:
            await page.goto(
                list_url,
                wait_until="domcontentloaded",
                timeout=60_000,
            )

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=10_000,
                )
            except PlaywrightTimeoutError:
                pass

            await page.wait_for_timeout(500)

            raw_links = await page.eval_on_selector_all(
                JOB_LINK_SELECTOR,
                """
                elements => elements.map(element => element.href)
                """,
            )

        except PlaywrightTimeoutError:
            logger.exception(
                "Тайм-аут при открытии раздела: %s",
                list_url,
            )
            break

        except Exception:
            logger.exception(
                "Ошибка при открытии раздела: %s",
                list_url,
            )
            break

        job_links = []
        seen_on_page = set()

        for raw_link in raw_links:
            normalized_link = normalize_url(raw_link)

            if not is_job_url(normalized_link):
                continue

            if normalized_link in seen_on_page:
                continue

            seen_on_page.add(normalized_link)
            job_links.append(normalized_link)

        if not job_links:
            logger.info(
                "Ссылки на вакансии не найдены. Раздел завершён: %s",
                section_url,
            )
            break

        logger.info(
            "Найдено уникальных вакансий на странице %d: %d",
            page_num,
            len(job_links),
        )

        new_jobs_on_page = 0

        for job_url in job_links:
            if job_url in sent_jobs:
                logger.info(
                    "Вакансия уже отправлялась, пропуск: %s",
                    job_url,
                )
                continue

            job_data = await get_job_details(page, job_url)

            if job_data is None:
                continue

            message = (
                "JobAtSea\n\n"
                f"{job_data['text']}\n\n"
                f"E-mail: {', '.join(job_data['emails'])}"
            )

            try:
                await client.send_message(
                    TELEGRAM_TARGET,
                    message,
                    link_preview=False,
                )

                sent_jobs.add(job_url)
                save_memory(sent_jobs)
                new_jobs_on_page += 1

                logger.info(
                    "Вакансия отправлена в %s: %s",
                    TELEGRAM_TARGET,
                    job_url,
                )

                await asyncio.sleep(REQUEST_DELAY_SECONDS)

            except Exception:
                logger.exception(
                    "Не удалось отправить вакансию в Telegram: %s",
                    job_url,
                )

        # Если страница содержит только уже обработанные вакансии,
        # следующая страница всё равно проверяется.
        logger.info(
            "Страница %d обработана. Новых отправок: %d",
            page_num,
            new_jobs_on_page,
        )

        page_num += 1
        await asyncio.sleep(PAGE_DELAY_SECONDS)


# ============================================================
# ОСНОВНОЙ ЦИКЛ
# ============================================================

async def main() -> None:
    api_id = validate_configuration()
    sent_jobs = load_memory()

    client = TelegramClient(
        StringSession(TELEGRAM_SESSION),
        api_id,
        TELEGRAM_API_HASH,
    )

    logger.info("Подключение к Telegram...")

    await client.start()

    logger.info(
        "Telegram подключён. Получатель: %s",
        TELEGRAM_TARGET,
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="en-US",
        )

        page = await context.new_page()

        try:
            while True:
                logger.info("Начинается новый цикл сканирования")

                for section_url in SECTIONS:
                    await scan_section(
                        page=page,
                        section_url=section_url,
                        sent_jobs=sent_jobs,
                        client=client,
                    )

                logger.info(
                    "Сканирование завершено. "
                    "Следующий запуск через %d секунд.",
                    SCAN_INTERVAL_SECONDS,
                )

                await asyncio.sleep(SCAN_INTERVAL_SECONDS)

        finally:
            await context.close()
            await browser.close()

    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Программа остановлена")
    except Exception:
        logger.exception("Критическая ошибка приложения")
        raise
