"""Dataclasses for the Money OS domain."""
from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from typing import Any


# Transaction kind. Income increases balance; expense decreases it.
TX_KINDS = ("expense", "income", "transfer")


@dataclass
class Account:
    name: str
    kind: str  # "cash", "bank", "card", "wallet", "investment"
    currency: str
    opening_balance: float = 0.0
    created_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Category:
    name: str
    kind: str  # "need", "want", "save", "income"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Transaction:
    ts: float
    amount: float          # always positive; sign comes from `kind`
    currency: str
    category: str
    account: str
    kind: str              # one of TX_KINDS
    merchant: str = ""
    note: str = ""
    id: int | None = None

    def signed_amount(self) -> float:
        if self.kind == "income":
            return abs(self.amount)
        if self.kind == "expense":
            return -abs(self.amount)
        return 0.0  # transfer has no net effect on total wealth

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Budget:
    month: str             # "YYYY-MM"
    category: str
    limit: float
    currency: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)
