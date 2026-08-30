"""手动交易 CSV/JSON 导入工具。"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from quart.execution.fees import Fees
from quart.execution.models import BUY, SELL
from quart.manual_trading.models import FillInput
from quart.manual_trading.repository import TradingRepository


def load_snapshot_json(path: Path | str) -> dict:
    source_path = Path(path)
    with source_path.open(encoding="utf-8-sig") as file:
        payload = json.load(file)
    required = {"as_of", "cash_total", "cash_available_to_trade", "positions"}
    missing = sorted(required - set(payload))
    if missing:
        raise ValueError(f"账户快照缺少字段: {missing}")
    payload.setdefault("cash_withdrawable", payload["cash_available_to_trade"])
    payload.setdefault("cash_frozen", 0.0)
    return payload


def import_fills_csv(
    repository: TradingRepository,
    account_id: int,
    path: Path | str,
    estimate_missing_fees: bool = True,
) -> list[int]:
    """导入通用成交 CSV。

    必填列: trade_date,symbol,side,quantity,price。
    可选列: trade_time,planned_order_id,broker_fill_id,commission,stamp_tax,
    transfer_fee,other_fee,settle_date。
    """
    source_path = Path(path)
    fill_ids: list[int] = []
    fees = Fees.from_config()
    with source_path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        required = {"trade_date", "symbol", "side", "quantity", "price"}
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f"成交 CSV 缺少列: {missing}")
        for line_number, row in enumerate(reader, start=2):
            try:
                side = str(row["side"]).strip().upper()
                symbol = _normalize_symbol(row["symbol"])
                quantity = int(row["quantity"])
                price = float(row["price"])
                commission = _float(row.get("commission"))
                stamp_tax = _float(row.get("stamp_tax"))
                transfer_fee = _float(row.get("transfer_fee"))
                other_fee = _float(row.get("other_fee"))
                source = "CSV"
                if estimate_missing_fees and not any((commission, stamp_tax, transfer_fee, other_fee)):
                    amount = quantity * price
                    other_fee = fees.buy_cost(amount) if side == BUY else fees.sell_cost(amount)
                    source = "CSV_ESTIMATED_FEES"
                planned_order_id = _int_or_none(row.get("planned_order_id"))
                trade_date = str(row["trade_date"]).strip()
                if planned_order_id is None:
                    planned_order_id = repository.match_planned_order(
                        account_id,
                        symbol,
                        side,
                        trade_date,
                        quantity,
                    )
                fill = FillInput(
                    symbol=symbol,
                    side=side,
                    quantity=quantity,
                    price=price,
                    trade_date=trade_date,
                    trade_time=_text_or_none(row.get("trade_time")),
                    planned_order_id=planned_order_id,
                    broker_fill_id=_text_or_none(row.get("broker_fill_id")),
                    commission=commission,
                    stamp_tax=stamp_tax,
                    transfer_fee=transfer_fee,
                    other_fee=other_fee,
                    source=source,
                    settle_date=_text_or_none(row.get("settle_date")),
                )
                if side not in (BUY, SELL):
                    raise ValueError(f"未知方向 {side!r}")
                fill_ids.append(repository.record_fill(account_id, fill))
            except Exception as exc:
                raise ValueError(f"成交 CSV 第 {line_number} 行导入失败: {exc}") from exc
    return fill_ids


def export_plan_csv(repository: TradingRepository, plan_id: str, path: Path | str) -> Path:
    """导出券商客户端可人工参考的委托 CSV，不包含自动报单指令。"""
    detail = repository.plan_detail(plan_id)
    if detail is None:
        raise KeyError(f"交易计划不存在: {plan_id}")
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "plan_id",
        "planned_order_id",
        "trade_date",
        "symbol",
        "side",
        "quantity",
        "reference_price",
        "target_weight",
        "estimated_fee",
        "deferred_quantity",
        "status",
    ]
    plan = detail["plan"]
    with output.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        for order in detail["orders"]:
            writer.writerow(
                {
                    "plan_id": plan_id,
                    "planned_order_id": order["planned_order_id"],
                    "trade_date": plan["intended_trade_date"],
                    "symbol": order["symbol"],
                    "side": order["side"],
                    "quantity": order["approved_quantity"] or order["strategy_quantity"],
                    "reference_price": order["reference_price"],
                    "target_weight": order["target_weight"],
                    "estimated_fee": order["estimated_fee"],
                    "deferred_quantity": order["deferred_quantity"],
                    "status": order["status"],
                }
            )
    return output


def write_fill_template(path: Path | str) -> Path:
    output = Path(path)
    output.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "trade_date",
        "trade_time",
        "symbol",
        "side",
        "quantity",
        "price",
        "planned_order_id",
        "broker_fill_id",
        "commission",
        "stamp_tax",
        "transfer_fee",
        "other_fee",
        "settle_date",
    ]
    with output.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=fields)
        writer.writeheader()
        writer.writerow({
            "trade_date": "2026-08-31",
            "trade_time": "09:35:00",
            "symbol": "600519",
            "side": "BUY",
            "quantity": "100",
            "price": "1500.00",
            "planned_order_id": "",
            "broker_fill_id": "example-001",
            "commission": "",
            "stamp_tax": "",
            "transfer_fee": "",
            "other_fee": "",
            "settle_date": "2026-09-01",
        })
    return output


def _float(value: str | None) -> float:
    return float(value) if value not in (None, "") else 0.0


def _int_or_none(value: str | None) -> int | None:
    return int(value) if value not in (None, "") else None


def _text_or_none(value: str | None) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _normalize_symbol(value: str) -> str:
    text = str(value).strip()
    return text.zfill(6) if text.isdigit() else text


__all__ = ["export_plan_csv", "import_fills_csv", "load_snapshot_json", "write_fill_template"]
