import os
import re
import json
import logging
import asyncio
from pathlib import Path
from telethon import TelegramClient
from telethon.sessions import StringSession
from playwright.async_api import async_playwright

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# --- Конфигурация ---
API_ID = int(os.environ.get("TELEGRAM_API_ID"))
API_HASH = os.environ.get("TELEGRAM_API_HASH")
SESSION_STRING = os.environ.get("TELEGRAM_SESSION")
TARGET_CHAT = "@fwd19472"
DEBUG_DIR = Path("debug")

# Список бесплатных почтовых доменов
FREE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "mail.ru", 
    "yandex.ru", "rambler.ru", "list.ru", "bk.ru", "inbox.ru"
}

# Целевой раздел
TARGET_URL = "https://jobatsea.online/jobs/Engine_Officers/"

def is_free_email(email: str) -> bool:
    domain = email.split('@')[-1].lower()
    return domain in FREE_EMAIL_DOMAINS

def get_business_emails(text: str) -> list:
    emails = re.findall(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', text)
    return [e for e in emails if not is_free_email(e)]

async def process_page(page, url):
    """Парсинг конкретной страницы раздела."""
    logger.info(f"Открываем страницу: {url}")
    await page.goto(url, wait_until="networkidle", timeout=60000)
    
    # Небольшая пауза для прогрузки JS
    await asyncio.sleep(3)
    
    html = await page.content()
    
    # --- Диагностика Cloudflare ---
    lower_html = html.lower()
    protection_words = ["cloudflare", "checking your browser", "just a moment", "captcha", "access denied"]
    detected_protection = [word for word in protection_words if word in lower_html]
    
    if detected_protection:
        logger.error(f"Сайт заблокировал запрос (Cloudflare). Обнаружено: {', '.join(detected_protection)}")
        
        # Сохраним HTML для анализа
        DEBUG_DIR.mkdir(parents=True, exist_ok=True)
        debug_file = DEBUG_DIR / "blocked_page.html"
        debug_file.write_text(html, encoding="utf-8")
        logger.warning(f"HTML заблокированной страницы сохранен в {debug_file}")
        return []

    # --- Поиск ссылок на вакансии ---
    links = await page.eval_on_selector_all("a", "elements => elements.map(e => e.href)")
    job_links = [link for link in links if "/job/" in link]
    job_links = list(set(job_links)) # Уникальные
    
    logger.info(f"Найдено ссылок на вакансии: {len(job_links)}")
    return job_links

async def main():
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH)
    await client.start()
    logger.info("Telegram-клиент успешно запущен")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        
        # Установка реального User-Agent
        await page.set_extra_http_headers({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36"
        })

        # Основной цикл для раздела
        job_links = await process_page(page, TARGET_URL)
        
        if job_links:
            logger.info(f"Начинаем обработку {len(job_links)} ссылок...")
            for link in job_links:
                # Здесь должна быть логика получения контента вакансии и отправки в Telegram
                # ... (код парсинга деталей вакансии и отправки через client.send_message)
                logger.info(f"Обработка вакансии: {link}")
        else:
            logger.warning("Вакансии не найдены или доступ заблокирован.")

        await browser.close()
    
    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
