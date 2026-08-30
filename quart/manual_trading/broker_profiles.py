"""券商成交文件列名映射与取值归一化。

各券商导出的当日成交 CSV 列名和取值各不相同（日期格式、买卖方向、
代码带后缀等）。本模块把它们归一化为通用成交模板的列
（trade_date,symbol,side,quantity,price,...），再交给
`quart.manual_trading.io.import_fills_csv` 复用既有的匹配/费用/去重逻辑。

内置 profile 覆盖常见命名；未识别的列名会显式报错而不是静默丢弃，
避免成交被漏导入。新券商只需在 BROKER_PROFILES 里补一份别名映射。
"""
from __future__ import annotations

import csv
from pathlib import Path

CANONICAL_FIELDS = [
    "trade_date",
    "trade_time",
    "symbol",
    "side",
    "quantity",
    "price",
    "broker_fill_id",
    "commission",
    "stamp_tax",
    "transfer_fee",
    "other_fee",
    "settle_date",
]

# 各券商常见的列名别名；键为通用列，值为按优先级排列的候选列名。
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "trade_date": ("成交日期", "交易日期", "日期", "date", "trade_date", "发生日期"),
    "trade_time": ("成交时间", "交易时间", "时间", "time", "trade_time"),
    "symbol": ("证券代码", "股票代码", "代码", "symbol", "code", "证券代码/证券简称"),
    "side": ("操作", "买卖方向", "方向", "业务名称", "交易类别", "side", "操作方向"),
    "quantity": ("成交数量", "成交量", "数量", "quantity", "qty", "成交数量(股)"),
    "price": ("成交价格", "成交价", "价格", "price", "均价", "成交均价"),
    "broker_fill_id": ("成交编号", "合同编号", "委托编号", "成交序号", "broker_fill_id", "record_id"),
    "commission": ("佣金", "手续费", "佣金及印花税", "commission"),
    "stamp_tax": ("印花税", "印花税金", "stamp_tax", "印花税费"),
    "transfer_fee": ("过户费", "过户费用", "transfer_fee"),
    "other_fee": ("其他费用", "附加费", "规费", "other_fee"),
    "settle_date": ("交收日期", "结算日期", "settle_date"),
}

# 买卖方向取值归一化（含券商常见的中文与缩写）。
SIDE_ALIASES: dict[str, str] = {
    "BUY": "BUY",
    "B": "BUY",
    "买入": "BUY",
    "证券买入": "BUY",
    "证券买人": "BUY",  # 部分券商导出用"买人"
    "担保品买入": "BUY",
    "买进": "BUY",
    "SELL": "SELL",
    "S": "SELL",
    "卖出": "SELL",
    "证券卖出": "SELL",
    "证券卖人": "SELL",
    "担保品卖出": "SELL",
    "卖出还款": "SELL",
}

#: 预置券商方案：不同券商的导出列名差异主要靠 COLUMN_ALIASES 兜底，
#: 这里记录需要特殊处理的券商名和说明。
BROKER_PROFILES: dict[str, dict[str, str]] = {
    "generic": {"description": "通用模板/本平台导出格式"},
    "ths": {"description": "同花顺/部分券商客户端导出：日期 20260831、方向为 买入/卖出"},
    "eastmoney": {"description": "东方财富证券：日期 2026-08-31、方向为 证券买入/证券卖出"},
    "htsec": {"description": "华泰证券：成交日期+成交时间、数量列带(股)后缀"},
}


def normalize_side(value: str) -> str:
    text = str(value).strip()
    return SIDE_ALIASES.get(text, SIDE_ALIASES.get(text.upper(), ""))


def normalize_date(value: str) -> str:
    """支持 2026-08-31 / 2026/8/31 / 20260831 / 20260831093000。"""
    text = str(value).strip()
    if not text:
        raise ValueError("日期为空")
    for separator in ("/", "-"):
        if separator in text:
            parts = text.split(" ")[0].split(separator)
            if len(parts) == 3 and all(part.isdigit() for part in parts):
                year, month, day = (int(part) for part in parts)
                return f"{year:04d}-{month:02d}-{day:02d}"
            break
    digits = text.replace("/", "").replace("-", "").replace(":", "").replace(" ", "")
    if len(digits) >= 8 and digits[:8].isdigit():
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:8]}"
    raise ValueError(f"无法解析日期: {value!r}")


def normalize_symbol(value: str) -> str:
    text = str(value).strip()
    # 部分券商导出 "600000.SH" / "000001SZ"
    for suffix in (".SH", ".SZ", ".BJ"):
        if text.upper().endswith(suffix):
            text = text[: -len(suffix)]
            break
    return text.zfill(6) if text.isdigit() else text


def convert_broker_csv(
    source: Path | str,
    target: Path | str,
    profile: str | None = None,
) -> Path:
    """把券商成交 CSV 转换为通用模板 CSV。

    未识别的列会聚合到错误信息中；方向取值无法归一化时该行报错。
    """
    source_path = Path(source)
    target_path = Path(target)
    del profile  # 预留：当前别名表已覆盖常见命名，profile 仅用于文档语义
    with source_path.open(encoding="utf-8-sig", newline="") as file:
        reader = csv.DictReader(file)
        columns = list(reader.fieldnames or [])
        mapping = _resolve_columns(columns)
        missing_required = [
            name for name in ("trade_date", "symbol", "side", "quantity", "price")
            if name not in mapping
        ]
        if missing_required:
            raise ValueError(
                f"成交 CSV 缺少必需列 {missing_required}; 实际列: {columns}"
            )
        rows: list[dict[str, str]] = []
        errors: list[str] = []
        for line_number, raw in enumerate(reader, start=2):
            try:
                rows.append(_convert_row(raw, mapping))
            except Exception as exc:
                errors.append(f"第 {line_number} 行: {exc}")
        if errors:
            raise ValueError("券商成交 CSV 转换失败:\n" + "\n".join(errors))

    return _write_normalized(target_path, rows)


def convert_broker_xlsx(
    source: Path | str,
    target: Path | str,
    sheet: str | int = 0,
    profile: str | None = None,
) -> Path:
    """把券商导出的 XLSX 成交表转换为通用模板 CSV。

    列名归一化与 CSV 版本共用 `COLUMN_ALIASES`；首行为表头。
    依赖 openpyxl（项目依赖已含）。
    """
    source_path = Path(source)
    target_path = Path(target)
    del profile
    try:
        import openpyxl
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("读取 XLSX 需要 openpyxl: pip install openpyxl") from exc

    workbook = openpyxl.load_workbook(source_path, read_only=True, data_only=True)
    ws = workbook[sheet] if isinstance(sheet, str) else workbook.worksheets[sheet]
    iterator = ws.iter_rows(values_only=True)
    try:
        header = next(iterator)
    except StopIteration as exc:
        raise ValueError("XLSX 工作表为空") from exc

    columns = [str(value).strip() if value is not None else "" for value in header]
    mapping = _resolve_columns(columns)
    missing_required = [
        name for name in ("trade_date", "symbol", "side", "quantity", "price")
        if name not in mapping
    ]
    if missing_required:
        raise ValueError(
            f"成交 XLSX 缺少必需列 {missing_required}; 实际列: {columns}"
        )

    rows: list[dict[str, str]] = []
    errors: list[str] = []
    for line_number, values in enumerate(iterator, start=2):
        if all(value is None or str(value).strip() == "" for value in values):
            continue  # 跳过尾部空行
        raw = {columns[i]: values[i] for i in range(len(columns)) if i < len(values)}
        try:
            rows.append(_convert_row(raw, mapping))
        except Exception as exc:
            errors.append(f"第 {line_number} 行: {exc}")
    workbook.close()
    if errors:
        raise ValueError("券商成交 XLSX 转换失败:\n" + "\n".join(errors))
    if not rows:
        raise ValueError("XLSX 中没有有效成交行")

    return _write_normalized(target_path, rows)


def _write_normalized(target_path: Path, rows: list[dict[str, str]]) -> Path:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with target_path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CANONICAL_FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({name: row.get(name, "") for name in CANONICAL_FIELDS})
    return target_path


def _resolve_columns(columns: list[str]) -> dict[str, str]:
    stripped = {str(name).strip(): name for name in columns if name is not None}
    mapping: dict[str, str] = {}
    for canonical, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            exact = stripped.get(alias) or stripped.get(alias.lower())
            if exact is not None:
                mapping[canonical] = exact
                break
    return mapping


def _convert_row(raw: dict[str, str], mapping: dict[str, str]) -> dict[str, str]:
    row: dict[str, str] = {}
    for canonical, source_column in mapping.items():
        row[canonical] = str(raw.get(source_column) or "").strip()
    side = normalize_side(row.get("side", ""))
    if not side:
        raise ValueError(f"无法识别买卖方向: {row.get('side')!r}")
    row["side"] = side
    row["trade_date"] = normalize_date(row.get("trade_date", ""))
    row["symbol"] = normalize_symbol(row.get("symbol", ""))
    if not row["symbol"]:
        raise ValueError("证券代码为空")
    quantity = row.get("quantity", "")
    try:
        # XLSX 数字单元格可能是 100 / 100.0；统一按整数处理
        row["quantity"] = str(int(float(quantity)))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"成交数量无效: {quantity!r}") from exc
    try:
        float(row["price"])
    except (KeyError, ValueError) as exc:
        raise ValueError(f"成交价格无效: {row.get('price')!r}") from exc
    return row


__all__ = [
    "BROKER_PROFILES",
    "CANONICAL_FIELDS",
    "COLUMN_ALIASES",
    "convert_broker_csv",
    "convert_broker_xlsx",
    "normalize_date",
    "normalize_side",
    "normalize_symbol",
]
