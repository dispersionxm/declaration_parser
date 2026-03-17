#!/usr/bin/env python3
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


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


def clean_flow_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{2,}", "\n", text)
    return text.strip()


def clean_extracted(text: str | None) -> str | None:
    if not text:
        return None
    value = text.replace("\n", " ")
    value = re.sub(r"^(?:№|No\.?|1-|2-|1\s+)\s*", "", value, flags=re.IGNORECASE)
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


def extract_contextual(text: str, start_pattern: str, stop_pattern: str, max_chars: int = 1200) -> str | None:
    start = re.search(start_pattern, text, flags=re.IGNORECASE)
    if not start:
        return None

    tail = text[start.end() : start.end() + max_chars]
    stop = re.search(stop_pattern, tail, flags=re.IGNORECASE)
    chunk = tail[: stop.start()] if stop else tail
    return clean_extracted(chunk)


def extract_until_first_date(text: str, code_pattern: str, max_chars: int = 220) -> str | None:
    code_match = re.search(code_pattern, text, flags=re.IGNORECASE)
    if not code_match:
        return None

    tail = text[code_match.end() : code_match.end() + max_chars]
    date_match = re.search(r"\d{2}\.\d{2}\.\d{2,4}", tail)
    if not date_match:
        return clean_extracted(tail)

    return clean_extracted(tail[: date_match.end()])


def extract_declaration_number(text: str) -> str | None:
    match = re.search(r"\b(\d{8,}/\d{6}/[A-ZА-Я0-9-]+)\b", text)
    return clean_extracted(match.group(1)) if match else None


def extract_box18_vehicle(text: str) -> str | None:
    segment_match = re.search(r"18\s+Идентификация([\s\S]{0,400}?)(?:\bUZ\b|\n\d{1,2}\s)", text, re.IGNORECASE)
    segment = segment_match.group(1) if segment_match else text

    strict = re.search(r"\b[0-9]{2}[A-Z][0-9]{3}[A-Z]{2}/[0-9]{2,}\b", segment)
    if strict:
        return strict.group(0)

    generic = re.search(r"\b[\w-]+/[\w-]+\b", segment)
    if generic:
        return generic.group(0)

    return None


def extract_box54_date(full_text: str) -> str | None:
    tail = full_text[-2500:]
    near_name = re.search(r"(?:АЛДАБАЙ|МАНЬ|подпись|фамилия|имя|декларант)[\s\S]{0,250}?(\d{2}\.\d{2}\.\d{2,4})", tail, re.IGNORECASE)
    if near_name:
        return near_name.group(1)

    all_dates = re.findall(r"\b\d{2}\.\d{2}\.\d{2,4}\b", tail)
    return all_dates[-1] if all_dates else None


def extract_customs_value(text: str) -> float | None:
    match = re.search(
        r"(?:Общая\s+таможенная\s+стоимость|12\s+Общая\s+таможенная\s+стоимость)\D*([\d\s]+[,\.]\d{2})",
        text,
        flags=re.IGNORECASE,
    )
    return parse_number(match.group(1)) if match else None


def extract_top_fields(full_text: str) -> dict[str, str | float | None]:
    sender = extract_contextual(
        full_text,
        start_pattern=r"2\.?\s*Отправитель/Экспортер",
        stop_pattern=r"(?:\bДЕКЛАРАЦИЯ\b|№|\bУЗБЕКИСТАН\b|\b1\s)",
    )

    # fallback: иногда данные начинаются сразу после 000
    if not sender:
        sender = extract_contextual(
            full_text,
            start_pattern=r"\b000\b",
            stop_pattern=r"(?:\bДЕКЛАРАЦИЯ\b|№|\bУЗБЕКИСТАН\b|\b1\s)",
        )

    receiver = extract_contextual(
        full_text,
        start_pattern=r"8\s+Получатель",
        stop_pattern=r"(?:\b\d{10}\b|\bРОССИЯ\b|\b14\s+Декларант\b)",
    )

    invoice = extract_until_first_date(full_text, r"04021/0")
    contract = extract_until_first_date(full_text, r"03011/2")

    return {
        "declaration_number": extract_declaration_number(full_text),
        "declaration_date": extract_box54_date(full_text),
        "vehicle": extract_box18_vehicle(full_text),
        "sender": sender,
        "receiver": receiver,
        "invoice_04021": invoice,
        "contract_03011": contract,
        "customs_value": extract_customs_value(full_text),
    }


def parse_box_31_goods(page_text: str) -> list[str]:
    goods: list[str] = []
    matches = re.finditer(
        r"31\s+[^\n]*?(?:описание\s+товаров)?([\s\S]{0,1200}?)(?=\n\s*(?:32|33|34|35|36|37|38)\b|$)",
        page_text,
        flags=re.IGNORECASE,
    )

    for match in matches:
        chunk = match.group(1)
        for raw_line in chunk.splitlines():
            line = clean_extracted(raw_line)
            if not line:
                continue
            if re.search(r"описание\s+товар|грузовые\s+места|маркиров", line, flags=re.IGNORECASE):
                continue
            if re.fullmatch(r"[\d\s.,]+", line):
                continue
            goods.append(line)

    return goods


def parse_weights(text: str, box_number: int) -> list[float]:
    values: list[float] = []

    if box_number == 35:
        label_pattern = r"35\s+Вес\s+брутто\s*\(кг\)"
    elif box_number == 38:
        label_pattern = r"38\s+Вес\s+нетто\s*\(кг\)"
    else:
        return values

    for label in re.finditer(label_pattern, text, flags=re.IGNORECASE):
        segment = text[label.end() : label.end() + 100]
        num_match = re.search(r"\b\d[\d ]*(?:[.,]\d+)?\b", segment)
        if not num_match:
            continue
        num = parse_number(num_match.group(0))
        if num is not None:
            values.append(num)

    return values


def parse_weights_from_tables(pages: list) -> tuple[list[float], list[float]]:
    gross: list[float] = []
    net: list[float] = []

    for page in pages:
        tables = page.extract_tables() or []
        for table in tables:
            for row in table:
                if not row:
                    continue
                row_text = " ".join(cell or "" for cell in row)
                row_text = normalize_space(row_text)

                if re.search(r"35\s*Вес\s*брутто", row_text, flags=re.IGNORECASE):
                    match = re.search(r"\b\d[\d ]*(?:[.,]\d+)?\b", row_text)
                    if match:
                        num = parse_number(match.group(0))
                        if num is not None:
                            gross.append(num)

                if re.search(r"38\s*Вес\s*нетто", row_text, flags=re.IGNORECASE):
                    match = re.search(r"\b\d[\d ]*(?:[.,]\d+)?\b", row_text)
                    if match:
                        num = parse_number(match.group(0))
                        if num is not None:
                            net.append(num)

    return gross, net


def parse_pdf(pdf_path: Path) -> DeclarationRow:
    import pdfplumber

    page_texts: list[str] = []
    goods: list[str] = []

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            page_text = clean_flow_text(page.extract_text(layout=True) or "")
            page_texts.append(page_text)
            goods.extend(parse_box_31_goods(page_text))

        full_text = "\n".join(page_texts)
        fields = extract_top_fields(full_text)

        gross_values = parse_weights(full_text, 35)
        net_values = parse_weights(full_text, 38)

        # Required fallback: if 0 extracted, retry via page tables.
        if sum(gross_values) == 0 or sum(net_values) == 0:
            tbl_gross, tbl_net = parse_weights_from_tables(pdf.pages)
            if sum(gross_values) == 0 and tbl_gross:
                gross_values = tbl_gross
            if sum(net_values) == 0 and tbl_net:
                net_values = tbl_net

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
