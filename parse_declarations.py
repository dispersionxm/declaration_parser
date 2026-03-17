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
    return re.sub(r"\s+", " ", text).strip()


def clean_page_text(text: str) -> str:
    text = text.replace("\r", "\n")
    lines = [normalize_space(line) for line in text.split("\n") if line.strip()]
    return "\n".join(lines)


def clean_extracted(text: str | None) -> str | None:
    if not text:
        return None
    value = text.replace("\n", " ")
    value = re.sub(r"^(?:1-|2-|№)\s*", "", value, flags=re.IGNORECASE)
    value = re.sub(r"\s+", " ", value).strip(" -\t\n")
    return value or None


def parse_number(raw: str | None) -> float | None:
    if not raw:
        return None

    value = raw.replace(" ", "")
    value = re.sub(r"[^\d,.-]", "", value)

    if "," in value and "." in value:
        value = value.replace(".", "").replace(",", ".")
    else:
        value = value.replace(",", ".")

    if value.count(".") > 1:
        first_dot = value.find(".")
        value = value[: first_dot + 1] + value[first_dot + 1 :].replace(".", "")

    try:
        return float(value)
    except ValueError:
        return None


def first_match(patterns: Iterable[str], text: str, flags: int = re.IGNORECASE | re.MULTILINE) -> str | None:
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return clean_extracted(match.group(1))
    return None


def extract_with_stops(text: str, start_pattern: str, stop_pattern: str, max_chars: int = 700) -> str | None:
    start = re.search(start_pattern, text, flags=re.IGNORECASE | re.MULTILINE)
    if not start:
        return None

    tail = text[start.end() : start.end() + max_chars]
    stop = re.search(stop_pattern, tail, flags=re.IGNORECASE | re.MULTILINE)
    chunk = tail[: stop.start()] if stop else tail
    return clean_extracted(chunk)


def extract_declaration_date(full_text: str) -> str | None:
    tail = full_text[-2000:]

    signature_match = re.search(
        r"(?:подпись|фамилия|имя|декларант|представитель)[\s\S]{0,250}?(\d{2}\.\d{2}\.\d{2,4})",
        tail,
        flags=re.IGNORECASE,
    )
    if signature_match:
        return signature_match.group(1)

    all_dates = re.findall(r"\b\d{2}\.\d{2}\.\d{2,4}\b", tail)
    return all_dates[-1] if all_dates else None


def extract_top_fields(full_text: str) -> dict[str, str | float | None]:
    declaration_number = first_match(
        [
            r"(?:Графа\s*А|Box\s*A|\bA\b)\D{0,30}(\d{8,}/\d{6}/[A-ZА-Я0-9-]+)",
            r"\b(\d{8,}/\d{6}/[A-ZА-Я0-9-]+)\b",
        ],
        full_text,
    )

    vehicle = first_match(
        [
            r"18\s+Идентификация[^\n]*?трансп[^\n]*?\n([^\n]+)",
            r"Идентификация[^\n]*?трансп[^\n]*?\b([A-Z0-9-]{2,}\/[A-Z0-9-]{2,})\b",
        ],
        full_text,
    )

    sender = extract_with_stops(
        full_text,
        start_pattern=r"(?:^|\n)(?:2\.?\s*Отправитель\/Экспортер|000)\s*[:\-]?\s*",
        stop_pattern=r"(?:\bДЕКЛАРАЦИЯ\b|\bУЗБЕКИСТАН\b|№)",
    )

    receiver = extract_with_stops(
        full_text,
        start_pattern=r"(?:^|\n)8\s+Получатель\s*[:\-]?\s*",
        stop_pattern=r"(?:\b\d{10,12}\b|\bРОССИЯ\b|\b14\s+Декларант\b)",
    )

    invoice = first_match(
        [
            r"04021\/0\s*([\s\S]{0,140}?\d{2}\.\d{2}\.\d{2})",
        ],
        full_text,
        flags=re.IGNORECASE,
    )

    contract = first_match(
        [
            r"03011\/2\s*([\s\S]{0,180}?\d{2}\.\d{2}\.\d{2})",
        ],
        full_text,
        flags=re.IGNORECASE,
    )

    customs_value_raw = first_match(
        [
            r"(?:Общая\s+таможенная\s+стоимость|12\s+Общая\s+таможенная\s+стоимость)\D*([\d\s]+[,\.]\d{2})",
        ],
        full_text,
    )

    return {
        "declaration_number": declaration_number,
        "declaration_date": extract_declaration_date(full_text),
        "vehicle": vehicle,
        "sender": sender,
        "receiver": receiver,
        "invoice_04021": invoice,
        "contract_03011": contract,
        "customs_value": parse_number(customs_value_raw),
    }


def parse_box_31_goods(page_text: str) -> list[str]:
    goods: list[str] = []
    blocks = re.finditer(
        r"31\s+[^\n]*?(?:описание\s+товаров)?\s*([\s\S]*?)(?=\n\s*(?:32|33|34|35|36|37|38)\b|$)",
        page_text,
        flags=re.IGNORECASE,
    )

    for block in blocks:
        chunk = block.group(1)
        for raw_line in chunk.splitlines():
            line = clean_extracted(raw_line)
            if not line:
                continue
            if re.search(r"грузовые\s+места|описание\s+товар|маркиров", line, flags=re.IGNORECASE):
                continue
            goods.append(line)

    return goods


def parse_weights(page_text: str, box_number: int) -> list[float]:
    values: list[float] = []

    if box_number == 35:
        label = r"35\s+Вес\s+брутто\s*\(кг\)"
        stop = r"\n\s*(?:36|37|38|31|32|33|34)\b"
    elif box_number == 38:
        label = r"38\s+Вес\s+нетто\s*\(кг\)"
        stop = r"\n\s*(?:39|40|41|42|31|32|33|34|35)\b"
    else:
        return values

    pattern = rf"{label}([\s\S]*?)(?={stop}|$)"
    for match in re.finditer(pattern, page_text, flags=re.IGNORECASE):
        segment = match.group(1)
        number_match = re.search(r"([\d\s]+(?:[,\.]\d+)?)", segment)
        if not number_match:
            continue
        number = parse_number(number_match.group(1))
        if number is not None:
            values.append(number)

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
