import requests
import pandas as pd
from config import settings

BASE_URL = "https://api.twelvedata.com/time_series"

class TwelveDataError(RuntimeError):
    pass


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
    return df
