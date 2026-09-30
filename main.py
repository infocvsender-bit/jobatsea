import asyncio
import json
import logging
import os
import re
from pathlib import Path
from urllib.parse import urljoin, urlparse, urlunparse

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    async_playwright,
)
from telethon import TelegramClient
from telethon.sessions import StringSession


# ============================================================
# НАСТРОЙКИ
# ============================================================

BASE_URL = "https://jobatsea.online"

TELEGRAM_TARGET = "@fwd19472"

SECTIONS = [
    "Engine_Officers",
    "Engine_Ratings",
    "Deck_Officers",
    "Deck_Ratings",
    "Catering_Staff",
    "Offshore",
]

MAX_PAGES_PER_SECTION = 50
PAGE_TIMEOUT_MS = 60_000
JOB_DELAY_SECONDS = 2
SECTION_DELAY_SECONDS = 3

SENT_JOBS_FILE = Path("sent_jobs.json")
DEBUG_DIR = Path("debug")


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)


# ============================================================
# ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ
# ============================================================

TELEGRAM_API_ID_RAW = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "").strip()


def validate_configuration() -> int:
    if not TELEGRAM_API_ID_RAW:
        raise ValueError(
            "Не задана переменная TELEGRAM_API_ID "
            "в Railway Variables"
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

    try:
        return int(TELEGRAM_API_ID_RAW)
    except ValueError as error:
        raise ValueError(
            "TELEGRAM_API_ID должен содержать только цифры"
        ) from error


# ============================================================
# БЕСПЛАТНЫЕ EMAIL-СЕРВИСЫ
# ============================================================

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.co.uk",
    "yahoo.de",
    "yahoo.fr",
    "hotmail.com",
    "hotmail.co.uk",
    "live.com",
    "outlook.com",
    "outlook.co.uk",
    "msn.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "protonmail.com",
    "proton.me",
    "tutanota.com",
    "tuta.io",
    "mail.com",
    "gmx.com",
    "gmx.de",
    "zoho.com",
    "yandex.ru",
    "yandex.com",
    "ya.ru",
    "mail.ru",
    "bk.ru",
    "inbox.ru",
    "list.ru",
    "rambler.ru",
    "ukr.net",
    "i.ua",
    "meta.ua",
    "web.de",
    "wp.pl",
    "seznam.cz",
    "qq.com",
    "163.com",
    "126.com",
}


EMAIL_PATTERN = re.compile(
    r"\b[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+\b"
)


def is_free_email(email: str) -> bool:
    email = email.strip().lower()

    if "@" not in email:
        return True

    domain = email.rsplit("@", 1)[1].lower()

    return domain in FREE_EMAIL_DOMAINS


def get_business_emails(text: str) -> list[str]:
    if not text:
        return []

    found_emails = EMAIL_PATTERN.findall(text)

    result = []

    for email in found_emails:
        email = email.strip().lower()

        if is_free_email(email):
            continue

        if email not in result:
            result.append(email)

    return result


# ============================================================
# РАБОТА СО ССЫЛКАМИ
# ============================================================

def normalize_url(url: str) -> str | None:
    if not url:
        return None

    url = str(url).strip()

    if url.startswith(
        (
            "javascript:",
            "mailto:",
            "tel:",
            "#",
        )
    ):
        return None

    absolute_url = urljoin(
        f"{BASE_URL}/",
        url,
    )

    parsed = urlparse(absolute_url)

    if parsed.scheme not in {"http", "https"}:
        return None

    if parsed.netloc.lower() != "jobatsea.online":
        return None

    return urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path.rstrip("/") or "/",
            parsed.params,
            parsed.query,
            "",
        )
    )


def is_job_link(url: str) -> bool:
    """
    Валидная ссылка вакансии имеет вид:

    https://jobatsea.online/job/622499/title/
    """

    if not url:
        return False

    url = str(url).strip()

    # Ищем ссылку, если она находится внутри onclick,
    # data-url или другого HTML-атрибута.
    match = re.search(
        r"(?:https?://jobatsea\.online)?"
        r"/job/[^\s\"'<>\\)]+",
        url,
        flags=re.IGNORECASE,
    )

    if match:
        url = match.group(0)

    normalized = normalize_url(url)

    if not normalized:
        return False

    parsed = urlparse(normalized)
    path = parsed.path.lower()

    parts = [
        part
        for part in path.split("/")
        if part
    ]

    return (
        len(parts) >= 2
        and parts[0] == "job"
        and parts[1].isdigit()
    )


def extract_job_links_from_values(
    values: list[str],
) -> list[str]:
    result = set()

    for value in values:
        if not value:
            continue

        value = str(value).strip()

        matches = re.findall(
            r"(?:https?://jobatsea\.online)?"
            r"/job/[^\s\"'<>\\)]+",
            value,
            flags=re.IGNORECASE,
        )

        for match in matches:
            match = match.rstrip(
                ".,;:)]}"
            )

            normalized = normalize_url(match)

            if normalized and is_job_link(normalized):
                result.add(normalized)

    return sorted(result)


# ============================================================
# СОХРАНЕНИЕ ОТПРАВЛЕННЫХ ВАКАНСИЙ
# ============================================================

def load_sent_jobs() -> set[str]:
    if not SENT_JOBS_FILE.exists():
        return set()

    try:
        data = json.loads(
            SENT_JOBS_FILE.read_text(
                encoding="utf-8"
            )
        )

        if isinstance(data, list):
            return set(str(item) for item in data)

        if isinstance(data, dict):
            return set(str(item) for item in data.keys())

    except Exception:
        logger.exception(
            "Не удалось прочитать %s",
            SENT_JOBS_FILE,
        )

    return set()


def save_sent_jobs(sent_jobs: set[str]) -> None:
    SENT_JOBS_FILE.write_text(
        json.dumps(
            sorted(sent_jobs),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


# ============================================================
# ПОЛУЧЕНИЕ ССЫЛОК НА ВАКАНСИИ
# ============================================================

async def get_job_links(
    page,
    section_url: str,
    page_number: int,
) -> list[str]:
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
            timeout=PAGE_TIMEOUT_MS,
        )

        await page.wait_for_timeout(5_000)

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=15_000,
            )
        except PlaywrightTimeoutError:
            logger.warning(
                "Network idle не достигнут: %s",
                page_url,
            )

        # Прокрутка для запуска lazy loading.
        for _ in range(5):
            await page.mouse.wheel(0, 1_200)
            await page.wait_for_timeout(500)

        # Получаем все значения HTML-атрибутов.
        attribute_values = await page.evaluate(
            """
            () => {
                const values = [];

                for (const element of document.querySelectorAll("*")) {
                    for (const attribute of element.attributes) {
                        values.push(attribute.value);
                    }
                }

                return values;
            }
            """
        )

        html = await page.content()

        all_values = list(attribute_values)
        all_values.append(html)

        job_links = extract_job_links_from_values(
            all_values
        )

        logger.info(
            "URL браузера: %s",
            page.url,
        )

        logger.info(
            "Заголовок страницы: %s",
            await page.title(),
        )

        logger.info(
            "Найдено ссылок на вакансии: %d",
            len(job_links),
        )

        for job_link in job_links:
            logger.info(
                "Найдена вакансия: %s",
                job_link,
            )

        if not job_links:
            logger.warning(
                "Ссылки /job/ не найдены: %s",
                page_url,
            )

            DEBUG_DIR.mkdir(
                parents=True,
                exist_ok=True,
            )

            section_name = (
                section_url.rstrip("/")
                .split("/")[-1]
            )

            safe_name = re.sub(
                r"[^a-zA-Z0-9_-]",
                "_",
                section_name,
            )

            debug_file = DEBUG_DIR / (
                f"{safe_name}_page_{page_number}.html"
            )

            debug_file.write_text(
                html,
                encoding="utf-8",
            )

            logger.warning(
                "HTML сохранён в %s",
                debug_file,
            )

            lower_html = html.lower()

            protection_words = [
                "cloudflare",
                "checking your browser",
                "just a moment",
                "captcha",
                "access denied",
                "verify you are human",
            ]

            detected_protection = [
                word
                for word in protection_words
                if word in lower_html
            ]

            if detected_protection:
                logger.warning(
                    "Возможна защита сайта: %s",
                    ", ".join(detected_protection),
                )

        return job_links

    except PlaywrightTimeoutError:
        logger.exception(
            "Тайм-аут при открытии раздела: %s",
            page_url,
        )
        return []

    except Exception:
        logger.exception(
            "Ошибка получения ссылок: %s",
            page_url,
        )
        return []


# ============================================================
# ПОЛУЧЕНИЕ ДАННЫХ ВАКАНСИИ
# ============================================================

async def get_job_details(
    page,
    job_url: str,
) -> dict | None:
    logger.info(
        "Обработка вакансии: %s",
        job_url,
    )

    try:
        await page.goto(
            job_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )

        await page.wait_for_timeout(2_000)

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=15_000,
            )
        except PlaywrightTimeoutError:
            pass

        title = await page.title()

        body_text = await page.locator(
            "body"
        ).inner_text(
            timeout=15_000
        )

        body_text = re.sub(
            r"\n{3,}",
            "\n\n",
            body_text,
        ).strip()

        business_emails = get_business_emails(
            body_text
        )

        if not business_emails:
            logger.info(
                "Вакансия пропущена: "
                "корпоративный email не найден: %s",
                job_url,
            )
            return None

        # Ставим подпись сверху.
        message = (
            "JobAtSea\n\n"
            f"{body_text}"
        )

        return {
            "url": job_url,
            "title": title.strip(),
            "text": message.strip(),
            "emails": business_emails,
        }

    except PlaywrightTimeoutError:
        logger.exception(
            "Тайм-аут при обработке вакансии: %s",
            job_url,
        )
        return None

    except Exception:
        logger.exception(
            "Ошибка обработки вакансии: %s",
            job_url,
        )
        return None


# ============================================================
# TELEGRAM
# ============================================================

async def send_job(
    client: TelegramClient,
    job: dict,
) -> bool:
    try:
        await client.send_message(
            TELEGRAM_TARGET,
            job["text"],
            link_preview=False,
        )

        logger.info(
            "Вакансия отправлена в Telegram: %s",
            job["url"],
        )

        return True

    except Exception:
        logger.exception(
            "Ошибка отправки вакансии: %s",
            job["url"],
        )
        return False


# ============================================================
# ОСНОВНАЯ ЛОГИКА
# ============================================================

async def run_scraper(
    client: TelegramClient,
) -> None:
    sent_jobs = load_sent_jobs()

    logger.info(
        "Уже отправленных вакансий: %d",
        len(sent_jobs),
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )

        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 "
                "(KHTML, like Gecko) "
                "Chrome/120.0 Safari/537.36"
            ),
            viewport={
                "width": 1_440,
                "height": 900,
            },
            locale="en-US",
        )

        section_page = await context.new_page()
        job_page = await context.new_page()

        try:
            for section_name in SECTIONS:
                section_url = (
                    f"{BASE_URL}/jobs/{section_name}/"
                )

                logger.info(
                    "Начало обработки раздела: %s",
                    section_name,
                )

                empty_pages_in_row = 0

                for page_number in range(
                    1,
                    MAX_PAGES_PER_SECTION + 1,
                ):
                    job_links = await get_job_links(
                        section_page,
                        section_url,
                        page_number,
                    )

                    if not job_links:
                        empty_pages_in_row += 1

                        # После двух пустых страниц считаем,
                        # что раздел закончился.
                        if empty_pages_in_row >= 2:
                            logger.info(
                                "В разделе закончились страницы: %s",
                                section_name,
                            )
                            break

                        continue

                    empty_pages_in_row = 0

                    for job_url in job_links:
                        if job_url in sent_jobs:
                            logger.info(
                                "Вакансия уже отправлялась: %s",
                                job_url,
                            )
                            continue

                        job = await get_job_details(
                            job_page,
                            job_url,
                        )

                        if job is None:
                            # Помечать пропущенную вакансию
                            # отправленной не нужно.
                            continue

                        was_sent = await send_job(
                            client,
                            job,
                        )

                        if was_sent:
                            sent_jobs.add(job_url)
                            save_sent_jobs(sent_jobs)

                        await asyncio.sleep(
                            JOB_DELAY_SECONDS
                        )

                logger.info(
                    "Завершён раздел: %s",
                    section_name,
                )

                await asyncio.sleep(
                    SECTION_DELAY_SECONDS
                )

        finally:
            await context.close()
            await browser.close()


async def main() -> None:
    api_id = validate_configuration()

    logger.info(
        "Запуск Telegram-клиента",
    )

    client = TelegramClient(
        StringSession(TELEGRAM_SESSION),
        api_id,
        TELEGRAM_API_HASH,
    )

    await client.start()

    logger.info(
        "Telegram-клиент успешно запущен",
    )

    try:
        await run_scraper(client)
    finally:
        await client.disconnect()

        logger.info(
            "Telegram-клиент отключён",
        )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info(
            "Работа остановлена пользователем",
        )
    except Exception:
        logger.exception(
            "Критическая ошибка приложения",
        )
        raise
