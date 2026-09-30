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

TELEGRAM_TARGET = "@fwd19472"

SECTIONS = [
    "https://jobatsea.online/jobs/Engine_Officers/",
    "https://jobatsea.online/jobs/Engine_Ratings/",
    "https://jobatsea.online/jobs/Deck_Officers/",
    "https://jobatsea.online/jobs/Deck_Ratings/",
    "https://jobatsea.online/jobs/Catering_Staff/",
    "https://jobatsea.online/jobs/Offshore/",
]

SENT_FILE = Path("sent_jobs.json")
DEBUG_DIR = Path("debug")

SCAN_INTERVAL_SECONDS = 3600
PAGE_TIMEOUT_MS = 60_000
MESSAGE_LIMIT = 3500

TELEGRAM_API_ID_RAW = os.getenv("TELEGRAM_API_ID", "").strip()
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "").strip()
TELEGRAM_SESSION = os.getenv("TELEGRAM_SESSION", "").strip()


# ============================================================
# ЛОГИРОВАНИЕ
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger("jobatsea")


# ============================================================
# E-MAIL
# ============================================================

EMAIL_REGEX = re.compile(
    r"(?<![\w.+-])"
    r"[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+"
    r"@"
    r"[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+"
    r"(?![\w.-])"
)

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "yahoo.com",
    "yahoo.co.uk",
    "yahoo.ca",
    "outlook.com",
    "hotmail.com",
    "hotmail.co.uk",
    "live.com",
    "msn.com",
    "icloud.com",
    "me.com",
    "mac.com",
    "aol.com",
    "proton.me",
    "protonmail.com",
    "pm.me",
    "gmx.com",
    "gmx.de",
    "mail.com",
    "mail.ru",
    "bk.ru",
    "list.ru",
    "inbox.ru",
    "rambler.ru",
    "yandex.ru",
    "yandex.com",
    "ya.ru",
    "ukr.net",
    "web.de",
    "tutanota.com",
    "tuta.io",
}


def extract_emails(text: str) -> list[str]:
    emails = EMAIL_REGEX.findall(text)

    result = []
    seen = set()

    for email in emails:
        email = email.lower().strip(".,;:()[]<>\"'")

        if email not in seen:
            seen.add(email)
            result.append(email)

    return result


def is_free_email(email: str) -> bool:
    if "@" not in email:
        return True

    domain = email.rsplit("@", 1)[1].lower().strip().rstrip(".")
    return domain in FREE_EMAIL_DOMAINS


def get_business_emails(emails: list[str]) -> list[str]:
    return [
        email
        for email in emails
        if not is_free_email(email)
    ]


# ============================================================
# КОНФИГУРАЦИЯ И ПАМЯТЬ
# ============================================================

def validate_configuration():
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

    return api_id, TELEGRAM_API_HASH, TELEGRAM_SESSION


def load_memory() -> set[str]:
    if not SENT_FILE.exists():
        logger.info(
            "Файл sent_jobs.json отсутствует. "
            "Начинаем с пустой памяти."
        )
        return set()

    try:
        data = json.loads(
            SENT_FILE.read_text(encoding="utf-8")
        )

        if not isinstance(data, list):
            logger.warning(
                "sent_jobs.json имеет неправильный формат."
            )
            return set()

        return set(str(item) for item in data)

    except Exception:
        logger.exception("Ошибка чтения sent_jobs.json")
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
# ССЫЛКИ
# ============================================================

def normalize_url(url: str) -> str | None:
    if not url:
        return None

    url = url.strip()

    if url.startswith(
        ("javascript:", "mailto:", "tel:", "#")
    ):
        return None

    absolute_url = urljoin(
        "https://jobatsea.online/",
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
    Ссылка на ваканцию имеет формат:

    https://jobatsea.online/job/622499/название-вакансии/
    """

    normalized = normalize_url(url)

    if not normalized:
        return False

    parsed = urlparse(normalized)
    path = parsed.path.lower()

    # Главное условие для вакансии.
    return (
        path.startswith("/job/")
        and len(path.split("/")) >= 3
    )


# ============================================================
# ПОЛУЧЕНИЕ ССЫЛОК РАЗДЕЛА
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

        await page.wait_for_timeout(1000)

        # Собираем все ссылки с href.
        all_links = await page.eval_on_selector_all(
            "a[href]",
            """
            elements => elements.map(element => element.href)
            """,
        )

        unique_job_links = sorted(
            {
                normalize_url(link)
                for link in all_links
                if is_job_link(link)
            }
        )

        unique_job_links = [
            link
            for link in unique_job_links
            if link
        ]

        logger.info(
            "Всего ссылок на странице: %d",
            len(all_links),
        )

        logger.info(
            "Найдено ссылок на вакансии: %d",
            len(unique_job_links),
        )

        if unique_job_links:
            for link in unique_job_links:
                logger.info("Ваканция: %s", link)

        if not unique_job_links:
            logger.warning(
                "Ссылки /job/ не найдены на странице: %s",
                page_url,
            )

            DEBUG_DIR.mkdir(exist_ok=True)

            safe_name = re.sub(
                r"[^a-zA-Z0-9_-]",
                "_",
                section_url.rstrip("/").split("/")[-1],
            )

            debug_file = DEBUG_DIR / (
                f"{safe_name}_page_{page_number}.html"
            )

            try:
                html = await page.content()
                debug_file.write_text(
                    html,
                    encoding="utf-8",
                )

                logger.warning(
                    "HTML страницы сохранён в %s",
                    debug_file,
                )
            except Exception:
                logger.exception(
                    "Не удалось сохранить HTML страницы"
                )

        return unique_job_links

    except PlaywrightTimeoutError:
        logger.exception(
            "Тайм-аут при открытии раздела: %s",
            page_url,
        )
        return []

    except Exception:
        logger.exception(
            "Ошибка при получении ссылок: %s",
            page_url,
        )
        return []


# ============================================================
# ДАННЫЕ ВАКАНСИИ
# ============================================================

def clean_text(text: str) -> str:
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text.strip()


async def get_job_details(
    page,
    job_url: str,
) -> dict | None:
    logger.info(
        "Открытие вакансии: %s",
        job_url,
    )

    try:
        await page.goto(
            job_url,
            wait_until="domcontentloaded",
            timeout=PAGE_TIMEOUT_MS,
        )

        try:
            await page.wait_for_load_state(
                "networkidle",
                timeout=15_000,
            )
        except PlaywrightTimeoutError:
            pass

        await page.wait_for_timeout(700)

        text = await page.locator("body").inner_text()
        text = clean_text(text)

        if not text:
            logger.warning(
                "Текст вакансии пустой: %s",
                job_url,
            )
            return None

        all_emails = extract_emails(text)

        if not all_emails:
            logger.info(
                "Ваканция пропущена: e-mail не найден: %s",
                job_url,
            )
            return None

        business_emails = get_business_emails(all_emails)

        if not business_emails:
            logger.info(
                "Ваканция пропущена: только бесплатный e-mail: %s",
                job_url,
            )
            return None

        logger.info(
            "Найдены корпоративные e-mail: %s",
            ", ".join(business_emails),
        )

        return {
            "text": text[:MESSAGE_LIMIT],
            "emails": business_emails,
        }

    except PlaywrightTimeoutError:
        logger.exception(
            "Тайм-аут при открытии вакансии: %s",
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
# СКАНИРОВАНИЕ РАЗДЕЛА
# ============================================================

async def scan_section(
    context,
    section_url: str,
    sent_jobs: set[str],
    client: TelegramClient,
) -> None:
    page_number = 1

    page = await context.new_page()

    try:
        while True:
            job_links = await get_job_links(
                page,
                section_url,
                page_number,
            )

            # Пустая страница означает конец пагинации.
            if not job_links:
                logger.info(
                    "Ссылки на вакансии не найдены. "
                    "Раздел завершён: %s",
                    section_url,
                )
                break

            new_sent_count = 0

            for job_url in job_links:
                if job_url in sent_jobs:
                    logger.info(
                        "Ваканция уже была отправлена: %s",
                        job_url,
                    )
                    continue

                job_data = await get_job_details(
                    page,
                    job_url,
                )

                if not job_data:
                    continue

                message = (
                    "JobAtSea\n\n"
                    f"{job_data['text']}\n\n"
                    f"E-mail: "
                    f"{', '.join(job_data['emails'])}"
                )

                try:
                    await client.send_message(
                        TELEGRAM_TARGET,
                        message,
                    )

                    sent_jobs.add(job_url)
                    save_memory(sent_jobs)
                    new_sent_count += 1

                    logger.info(
                        "Ваканция отправлена в Telegram: %s",
                        job_url,
                    )

                    await asyncio.sleep(2)

                except Exception:
                    logger.exception(
                        "Ошибка отправки вакансии: %s",
                        job_url,
                    )

            logger.info(
                "Страница %d обработана. "
                "Новых вакансий отправлено: %d",
                page_number,
                new_sent_count,
            )

            page_number += 1
            await asyncio.sleep(1)

            # Защита от бесконечной пагинации.
            if page_number > 100:
                logger.warning(
                    "Достигнут лимит 100 страниц: %s",
                    section_url,
                )
                break

    finally:
        await page.close()


# ============================================================
# ОСНОВНОЙ ЦИКЛ
# ============================================================

async def scan_all_sections(
    context,
    client: TelegramClient,
    sent_jobs: set[str],
) -> None:
    logger.info("Начинается новый цикл сканирования")

    for section_url in SECTIONS:
        try:
            await scan_section(
                context,
                section_url,
                sent_jobs,
                client,
            )
        except Exception:
            logger.exception(
                "Ошибка обработки раздела: %s",
                section_url,
            )

    logger.info(
        "Сканирование завершено. "
        "Следующий запуск через %d секунд",
        SCAN_INTERVAL_SECONDS,
    )


async def main() -> None:
    api_id, api_hash, session = validate_configuration()
    sent_jobs = load_memory()

    logger.info("Подключение к Telegram...")

    client = TelegramClient(
        StringSession(session),
        api_id,
        api_hash,
    )

    await client.start()

    logger.info(
        "Telegram подключён. Получатель: %s",
        TELEGRAM_TARGET,
    )

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
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

        try:
            while True:
                await scan_all_sections(
                    context,
                    client,
                    sent_jobs,
                )

                await asyncio.sleep(
                    SCAN_INTERVAL_SECONDS
                )

        finally:
            await context.close()
            await browser.close()

    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Скрипт остановлен пользователем.")
    except Exception:
        logger.exception(
            "Критическая ошибка приложения"
        )
        raise
