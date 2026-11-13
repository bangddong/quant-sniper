"""Fail-closed live KRX market status check."""

import datetime
import json
import urllib.request


STATUS_URL = "https://m.stock.naver.com/api/stock/069500/basic"


def get_live_market_status(now=None):
    """Verify today's regular market is trading; any uncertainty returns False."""
    now = now or datetime.datetime.now()
    try:
        request = urllib.request.Request(
            STATUS_URL, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))

        traded_at = datetime.datetime.fromisoformat(data["localTradedAt"])
        return (
            traded_at.date() == now.date()
            and data.get("marketStatus") == "OPEN"
            and data.get("marketStatusDetailType") == "open"
            and data.get("marketSessionType") == "regularMarket"
            and data.get("tradableStatus") == "tradable"
            and data.get("tradeStopType", {}).get("name") == "TRADING"
        )
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return False
