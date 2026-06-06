import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    TWELVE_DATA_API_KEY: str = os.getenv("TWELVE_DATA_API_KEY", "")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    SYMBOL: str = os.getenv("SYMBOL", "XAU/USD")
    POLL_SECONDS: int = int(os.getenv("POLL_SECONDS", "300"))
    OUTPUTSIZE: int = int(os.getenv("OUTPUTSIZE", "120"))

    ACCOUNT_SIZE: float = float(os.getenv("ACCOUNT_SIZE", "50000"))
    DAILY_TARGET_MIN: float = float(os.getenv("DAILY_TARGET_MIN", "250"))
    DAILY_TARGET_MAX: float = float(os.getenv("DAILY_TARGET_MAX", "350"))
    MAX_RISK_PER_TRADE: float = float(os.getenv("MAX_RISK_PER_TRADE", "120"))
    NORMAL_LOT: float = float(os.getenv("NORMAL_LOT", "0.15"))
    AGGRESSIVE_LOT: float = float(os.getenv("AGGRESSIVE_LOT", "0.25"))
    MAX_LOT: float = float(os.getenv("MAX_LOT", "0.30"))
    DOLLARS_PER_1_00_LOT_PER_POINT: float = float(os.getenv("DOLLARS_PER_1_00_LOT_PER_POINT", "100"))

    SEND_WAIT_SIGNALS: bool = os.getenv("SEND_WAIT_SIGNALS", "false").lower() == "true"
    SIGNAL_COOLDOWN_MINUTES: int = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "30"))
    SEND_STARTUP_MESSAGE: bool = os.getenv("SEND_STARTUP_MESSAGE", "true").lower() == "true"
    SEND_ERROR_MESSAGES: bool = os.getenv("SEND_ERROR_MESSAGES", "false").lower() == "true"

    PORT: int = int(os.getenv("PORT", "10000"))

settings = Settings()
