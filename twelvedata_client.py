import requests
import pandas as pd
from datetime import datetime, timezone
from config import settings

BASE_URL = "https://api.twelvedata.com/time_series"

class TwelveDataError(RuntimeError):
    pass


def _interval_minutes(interval: str) -> int:
    return {"1min": 1, "5min": 5, "15min": 15, "30min": 30, "1h": 60}.get(interval, 5)


def fetch_ohlc(interval: str) -> pd.DataFrame:
    if not settings.TWELVE_DATA_API_KEY:
        raise TwelveDataError("Missing TWELVE_DATA_API_KEY")

    params = {
        "symbol": settings.SYMBOL,
        "interval": interval,
        "outputsize": settings.OUTPUTSIZE,
        "apikey": settings.TWELVE_DATA_API_KEY,
        "format": "JSON",
        "timezone": "UTC",
    }
    r = requests.get(BASE_URL, params=params, timeout=20)
    r.raise_for_status()
    data = r.json()

    if data.get("status") == "error":
        raise TwelveDataError(data.get("message", "Twelve Data error"))

    values = data.get("values")
    if not values:
        raise TwelveDataError(f"No OHLC values returned for {settings.SYMBOL} {interval}: {data}")

    df = pd.DataFrame(values)
    df["datetime"] = pd.to_datetime(df["datetime"], utc=True)
    for col in ["open", "high", "low", "close"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["open", "high", "low", "close"])
    df = df.sort_values("datetime").reset_index(drop=True)

    # Use only fully closed candles. Twelve Data can include the currently-forming candle.
    minutes = _interval_minutes(interval)
    now = datetime.now(timezone.utc)
    cutoff = now - pd.Timedelta(minutes=minutes)
    df = df[df["datetime"] <= cutoff].reset_index(drop=True)
    if len(df) < 50:
        raise TwelveDataError(f"Not enough closed candles for {interval}. Got {len(df)}")
    return df
