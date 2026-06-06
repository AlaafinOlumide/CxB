import pandas as pd


def add_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df["close"]
    high = df["high"]
    low = df["low"]

    # Bollinger Bands 20, 2
    mid = close.rolling(20).mean()
    std = close.rolling(20).std(ddof=0)
    df["bb_mid"] = mid
    df["bb_upper"] = mid + 2 * std
    df["bb_lower"] = mid - 2 * std

    # RSI 14
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, pd.NA)
    df["rsi"] = 100 - (100 / (1 + rs))

    # Stochastic 5,3,3 to match your mobile chart
    low_min = low.rolling(5).min()
    high_max = high.rolling(5).max()
    k_fast = 100 * (close - low_min) / (high_max - low_min).replace(0, pd.NA)
    df["stoch_k"] = k_fast.rolling(3).mean()
    df["stoch_d"] = df["stoch_k"].rolling(3).mean()

    return df.dropna().reset_index(drop=True)
