import csv
import io
import os
from typing import Dict, List, Tuple


def read_csv_file(file_path: str) -> Tuple[List[Dict[str, str]], str]:
    abs_path = os.path.abspath(file_path)
    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"文件不存在 {abs_path}")
    with open(abs_path, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    return rows, abs_path


def parse_csv_content(content: bytes) -> List[Dict[str, str]]:
    text = content.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))
    return [dict(row) for row in reader]


def apply_field_mapping(rows: List[dict], mapping: Dict[str, str]) -> List[dict]:
    if not mapping:
        return rows
    mapped = []
    for row in rows:
        new_row = {}
        for field_name, csv_col in mapping.items():
            new_row[field_name] = (row.get(csv_col, "") or "").strip()
        mapped.append(new_row)
    return mapped
