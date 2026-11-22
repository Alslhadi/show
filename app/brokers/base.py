"""Broker interface. Every adapter (paper, MT5, later Exness API) implements this."""
from __future__ import annotations

from abc import ABC, abstractmethod

import pandas as pd

from ..risk import AccountState, SymbolSpec

TIMEFRAMES = {"M1": 1, "M5": 5, "M15": 15, "M30": 30, "H1": 60, "H4": 240, "D1": 1440}


class Broker(ABC):
    name = "base"
    is_simulation = False

    @abstractmethod
    def connect(self) -> bool: ...

    @abstractmethod
    def connected(self) -> bool: ...

    @abstractmethod
    def account(self) -> AccountState: ...

    @abstractmethod
    def spec(self, symbol: str) -> SymbolSpec: ...

    @abstractmethod
    def tick(self, symbol: str) -> dict: ...  # {"bid","ask","time"}

    @abstractmethod
    def candles(self, symbol: str, tf: str, n: int = 300) -> pd.DataFrame: ...  # closed candles only

    @abstractmethod
    def positions(self) -> list[dict]: ...

    @abstractmethod
    def history(self, n: int = 50) -> list[dict]: ...

    @abstractmethod
    def market_order(self, symbol: str, direction: str, lots: float, sl: float, tp: float,
                     comment: str, client_id: str) -> dict: ...
    # returns {"ok": bool, "ticket": int|None, "price": float|None, "retcode": int|str, "message": str}

    @abstractmethod
    def modify(self, ticket: int, sl: float | None, tp: float | None) -> dict: ...

    @abstractmethod
    def close(self, ticket: int, lots: float | None = None) -> dict: ...

    def step(self) -> None:
        """Advance internal state (simulation only)."""
