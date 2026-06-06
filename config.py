import os
from dataclasses import dataclass

@dataclass(frozen=True)
class Settings:
    # Required secrets
    TWELVE_DATA_API_KEY: str = os.getenv("TWELVE_DATA_API_KEY", "")
    TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    TELEGRAM_CHAT_ID: str = os.getenv("TELEGRAM_CHAT_ID", "")

    # Twelve Data symbol. Try XAU/USD first. Some accounts may need XAU/USD:FOREX or another provider-supported symbol.
    SYMBOL: str = os.getenv("SYMBOL", "XAU/USD")

    # Free Twelve Data plan is commonly 8 calls/min and 800/day. 5 mins = ~576 calls/day for M5+M15.
    POLL_SECONDS: int = int(os.getenv("POLL_SECONDS", "300"))
    OUTPUTSIZE: int = int(os.getenv("OUTPUTSIZE", "120"))

    # Strategy / prop settings
    ACCOUNT_SIZE: float = float(os.getenv("ACCOUNT_SIZE", "50000"))
    DAILY_TARGET_MIN: float = float(os.getenv("DAILY_TARGET_MIN", "250"))
    DAILY_TARGET_MAX: float = float(os.getenv("DAILY_TARGET_MAX", "350"))
    MAX_LOT: float = float(os.getenv("MAX_LOT", "0.30"))
    NORMAL_LOT: float = float(os.getenv("NORMAL_LOT", "0.20"))
    AGGRESSIVE_LOT: float = float(os.getenv("AGGRESSIVE_LOT", "0.25"))

    # Telegram alert control
    SEND_WAIT_SIGNALS: bool = os.getenv("SEND_WAIT_SIGNALS", "false").lower() == "true"
    SIGNAL_COOLDOWN_MINUTES: int = int(os.getenv("SIGNAL_COOLDOWN_MINUTES", "20"))
    SEND_STARTUP_MESSAGE: bool = os.getenv("SEND_STARTUP_MESSAGE", "true").lower() == "true"

    # Web service
    PORT: int = int(os.getenv("PORT", "10000"))

settings = Settings()
