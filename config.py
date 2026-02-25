"""
Конфигурация SnapSell Bot
Заполните .env или укажите переменные окружения напрямую
"""

import os
from dataclasses import dataclass


@dataclass
class Config:
    # ── Обязательные ──────────────────────────────────────────
    BOT_TOKEN:        str = os.getenv("BOT_TOKEN", "")
    GEMINI_API_KEY:   str = os.getenv("GEMINI_API_KEY", "")

    # ── Опциональные ─────────────────────────────────────────
    FREE_GENERATIONS: int = int(os.getenv("FREE_GENERATIONS", "3"))

    # ID администратора (для команды /admin)
    ADMIN_ID:         int = int(os.getenv("ADMIN_ID", "0"))

    def validate(self):
        if not self.BOT_TOKEN:
            raise ValueError("BOT_TOKEN не задан!")
        if not self.GEMINI_API_KEY:
            raise ValueError("GEMINI_API_KEY не задан!")


config = Config()
