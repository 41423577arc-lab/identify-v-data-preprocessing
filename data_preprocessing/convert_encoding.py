"""Convert one CSV losslessly, or batch clean/deduplicate a folder, to UTF-8 BOM."""
import argparse
import codecs
import csv
import hashlib
import io
import json
from pathlib import Path


BASE = Path(__file__).resolve().parent


def decode_source(data, encoding=None):
    if encoding:
        return data.decode(encoding, errors="strict"), encoding
    # UTF-32 BOMs must be checked before their UTF-16 prefixes.
    for bom, codec in (
        (codecs.BOM_UTF32_LE, "utf-32"),
        (codecs.BOM_UTF32_BE, "utf-32"),
        (codecs.BOM_UTF8, "utf-8-sig"),
        (codecs.BOM_UTF16_LE, "utf-16"),
        (codecs.BOM_UTF16_BE, "utf-16"),
    ):
        if data.startswith(bom):
            return data.decode(codec, errors="strict"), codec
    # Do not silently guess a legacy encoding or replace undecodable characters.
    return data.decode("utf-8", errors="strict"), "utf-8"


def csv_rows(text):
    csv.field_size_limit(100_000_000)
    return list(csv.reader(io.StringIO(text, newline=""), strict=True))


def convert(source, destination, source_encoding=None):
    source, destination = source.resolve(), destination.resolve()
    if source == destination:
        raise ValueError("Source and destination must be different files")
    original = source.read_bytes()
    text, encoding = decode_source(original, source_encoding)
    # An explicitly selected utf-8 codec can leave its BOM in the decoded text.
    if text.startswith("\ufeff"):
        text = text[1:]
    rows = csv_rows(text)
    if not rows or not rows[0]:
        raise ValueError("CSV has no header")
    if any(len(row) != len(rows[0]) for row in rows[1:]):
        raise ValueError("CSV records have inconsistent column counts")
    converted = text.encode("utf-8-sig", errors="strict")
    if csv_rows(converted.decode("utf-8-sig")) != rows:
        raise ValueError("CSV contents changed during encoding conversion")
    if source.read_bytes() != original:
        raise ValueError("Source changed during conversion; retry after export finishes")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Existing outputs, including the original file, are never overwritten.
    with destination.open("xb") as stream:
        stream.write(converted)
    written = destination.read_bytes()
    if written != converted or not written.startswith(codecs.BOM_UTF8):
        raise ValueError("Output verification failed")
    if source.read_bytes() != original:
        raise ValueError("Source changed concurrently; output is from the earlier snapshot")
    return {
        "source": str(source),
        "output": str(destination),
        "source_encoding": encoding,
        "output_encoding": "utf-8-sig",
        "data_records": len(rows) - 1,
        "column_count": len(rows[0]),
        "columns": rows[0],
        "source_sha256": hashlib.sha256(original).hexdigest(),
        "output_sha256": hashlib.sha256(written).hexdigest(),
        "source_unchanged": True,
        "csv_values_and_order_unchanged": True,
        "csv_layout_and_line_endings_unchanged": True,
        "ad_filter_applied": False,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", nargs="?", type=Path,
                        default=BASE.parent / "raw_data" / "无标题.csv")
    parser.add_argument("--output", type=Path, help="Output file, or output directory for folder input")
    parser.add_argument("--source-encoding", help="Explicit codec, e.g. gb18030")
    args = parser.parse_args()
    if args.source.is_dir():
        from batch_preprocess import batch_process
        try:
            result = batch_process(args.source, args.output, source_encoding=args.source_encoding,
                                   convert_only=True)
        except (OSError, UnicodeError, ValueError, csv.Error, LookupError) as error:
            parser.exit(1, f"Batch conversion stopped: {error}\n")
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return
    destination = args.output or BASE / "outputs" / f"{args.source.stem}_utf8_bom.csv"
    try:
        result = convert(args.source, destination, args.source_encoding)
    except (OSError, UnicodeError, ValueError, csv.Error, LookupError) as error:
        # Do not print a decoder exception: it can contain fragments of input data.
        parser.exit(1, f"Conversion stopped ({type(error).__name__}). Check paths, "
                    "source encoding, CSV structure, and whether output already exists.\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
