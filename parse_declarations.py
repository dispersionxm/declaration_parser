#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class DeclarationRow:
    file_name: str
    declaration_number: str | None
    declaration_date: str | None
    vehicle: str | None
    sender: str | None
    receiver: str | None
    invoice_04021: str | None
    contract_03011: str | None
    goods_description: str | None
    total_gross_weight: float
    total_net_weight: float
    customs_value: float | None


def normalize_space(text: str) -> str:
    return re.sub(r"[ \t]+", " ", text).strip()


def clean_page_text(text: str) -> str:
    text = text.replace("\r", "\n")
    lines = [normalize_space(line) for line in text.split("\n") if line.strip()]
    return "\n".join(lines)


def parse_number(raw: str | None) -> float | None:
    if not raw:
        return None

    # Оставляем только числовые символы и разделители.
    cleaned = re.sub(r"[^\d,.-]", "", raw)

    # Если в числе есть и запятая, и точка, предполагаем тысячные + дробные.
    if "," in cleaned and "." in cleaned:
        cleaned = cleaned.replace(".", "").replace(",", ".")
    else:
        cleaned = cleaned.replace(",", ".")

    # Фолбэк на случай кривого OCR с несколькими точками.
    if cleaned.count(".") > 1:
        first_dot = cleaned.find(".")
        cleaned = cleaned[: first_dot + 1] + cleaned[first_dot + 1 :].replace(".", "")

    try:
        return float(cleaned)
    except ValueError:
        return None


def first_match(patterns: Iterable[str], text: str, flags: int = re.IGNORECASE | re.MULTILINE) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return normalize_space(match.group(1))
    return None


def extract_top_fields(full_text: str) -> dict[str, str | float | None]:
    declaration_number = first_match(
        [
            r"(?:Графа\s*А|Box\s*A|\bA\b)\D{0,20}(\d{8,}/\d{6}/[A-ZА-Я0-9-]+)",
            r"\b(\d{8,}/\d{6}/[A-ZА-Я0-9-]+)\b",
        ],
        full_text,
    )

    declaration_date = first_match(
        [
            r"(?:Графа\s*54|Box\s*54|\b54\b)\D{0,30}(\d{2}\.\d{2}\.\d{2,4})",
            r"\b(\d{2}\.\d{2}\.\d{2})\b",
        ],
        full_text,
    )

    vehicle = first_match(
        [
            r"(?:18\s+Идентификация[^\n]*?трансп[^\n]*?)\n([^\n]+)",
            r"Идентификация[^\n]*?трансп[^\n]*?\b([A-Z0-9-]{2,}\/[A-Z0-9-]{2,})\b",
            r"\b([A-Z0-9-]{2,}\/[A-Z0-9-]{2,})\b",
        ],
        full_text,
    )

    sender = first_match(
        [
            r"(?:^|\n)2\s+Отправитель\/Экспортер\s*[:\-]?\s*([^\n]+)",
            r"(?:^|\n)Отправитель\/Экспортер\s*[:\-]?\s*([^\n]+)",
        ],
        full_text,
    )

    receiver = first_match(
        [
            r"(?:^|\n)8\s+Получатель\s*[:\-]?\s*([^\n]+)",
            r"(?:^|\n)Получатель\s*[:\-]?\s*([^\n]+)",
        ],
        full_text,
    )

    invoice = first_match(
        [
            r"04021\/0\s*([^\n;]+)",
            r"04021\D+([^\n;]+)",
        ],
        full_text,
    )

    contract = first_match(
        [
            r"03011\/2\s*([^\n;]+)",
            r"03011\D+([^\n;]+)",
        ],
        full_text,
    )

    customs_value_raw = first_match(
        [
            r"(?:Общая\s+таможенная\s+стоимость|12\s+Общая\s+таможенная\s+стоимость)\D*([\d\s]+[,\.]\d{2})",
        ],
        full_text,
    )

    return {
        "declaration_number": declaration_number,
        "declaration_date": declaration_date,
        "vehicle": vehicle,
        "sender": sender,
        "receiver": receiver,
        "invoice_04021": invoice,
        "contract_03011": contract,
        "customs_value": parse_number(customs_value_raw),
    }


def parse_box_31_goods(page_text: str) -> list[str]:
    goods: list[str] = []
    for line in page_text.splitlines():
        match = re.match(r"^31\b\s*(.*)$", line, re.IGNORECASE)
        if not match:
            continue

        candidate = normalize_space(match.group(1))
        if not candidate:
            continue

        # Отсеиваем служебные подписи графы.
        if re.search(r"грузовые\s+места|описание\s+товар", candidate, re.IGNORECASE):
            continue

        goods.append(candidate)

    return goods


def parse_weights(page_text: str, box_number: int) -> list[float]:
    values: list[float] = []

    for line in page_text.splitlines():
        match = re.match(rf"^{box_number}\b\s*(.*)$", line)
        if not match:
            continue

        tail = match.group(1)
        num_match = re.search(r"([\d\s]+(?:[,\.]\d+)?)", tail)
        if not num_match:
            continue

        num = parse_number(num_match.group(1))
        if num is not None:
            values.append(num)

    return values


def parse_pdf(pdf_path: Path) -> DeclarationRow:
    import pdfplumber

    texts: list[str] = []
    goods: list[str] = []
    gross_values: list[float] = []
    net_values: list[float] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            page_text = clean_page_text(page.extract_text() or "")
            texts.append(page_text)
            goods.extend(parse_box_31_goods(page_text))
            gross_values.extend(parse_weights(page_text, 35))
            net_values.extend(parse_weights(page_text, 38))

    fields = extract_top_fields("\n".join(texts))

    unique_goods: list[str] = []
    seen: set[str] = set()
    for item in goods:
        key = item.lower()
        if key not in seen:
            seen.add(key)
            unique_goods.append(item)

    return DeclarationRow(
        file_name=pdf_path.name,
        declaration_number=fields["declaration_number"],
        declaration_date=fields["declaration_date"],
        vehicle=fields["vehicle"],
        sender=fields["sender"],
        receiver=fields["receiver"],
        invoice_04021=fields["invoice_04021"],
        contract_03011=fields["contract_03011"],
        goods_description=", ".join(unique_goods) if unique_goods else None,
        total_gross_weight=round(sum(gross_values), 3),
        total_net_weight=round(sum(net_values), 3),
        customs_value=fields["customs_value"],
    )


def process_folder(input_dir: Path, output_file: Path) -> None:
    import pandas as pd

    if not input_dir.exists():
        raise FileNotFoundError(f"Input folder does not exist: {input_dir}")

    pdf_files = sorted(input_dir.glob("*.pdf"))
    if not pdf_files:
        raise FileNotFoundError(f"PDF files were not found in {input_dir}")

    rows: list[dict[str, object]] = []
    for idx, pdf_path in enumerate(pdf_files, start=1):
        print(f"Processing [{idx}/{len(pdf_files)}]: {pdf_path.name}...")
        try:
            row = parse_pdf(pdf_path)
            rows.append(asdict(row))
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] Failed to parse {pdf_path.name}: {exc}")

    if not rows:
        raise RuntimeError("No declarations were parsed successfully.")

    df = pd.DataFrame(rows)
    df.to_excel(output_file, index=False, engine="openpyxl")
    print(f"Done. Saved: {output_file}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Parse customs declaration PDFs and export to a single Excel report.",
    )
    parser.add_argument("--input", default="./input", help="Path to directory with PDF files")
    parser.add_argument("--output", default="logistics_report.xlsx", help="Output Excel filename")
    return parser


def check_dependencies() -> None:
    missing: list[str] = []
    for package in ("pdfplumber", "pandas", "openpyxl"):
        try:
            __import__(package)
        except ModuleNotFoundError:
            missing.append(package)

    if missing:
        joined = ", ".join(missing)
        raise ModuleNotFoundError(
            f"Missing dependencies: {joined}. Install with: pip install pdfplumber pandas openpyxl"
        )


def main() -> None:
    args = build_parser().parse_args()
    try:
        check_dependencies()
        process_folder(Path(args.input), Path(args.output))
    except Exception as exc:  # noqa: BLE001
        print(f"Error: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
