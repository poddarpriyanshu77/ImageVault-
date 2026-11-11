#!/usr/bin/env python3
"""
ImageVault - Photo Metadata and Album Organizer
------------------------------------------------
Scans a folder of images, reads metadata (EXIF when Pillow is installed,
otherwise filename / file-modified date), detects duplicate files by
SHA-256 content hash, and organizes photos into album folders.

NOTHING is touched on disk until you pass --apply and confirm.
By default the program only prints a preview report.

Usage examples:
    python imagevault.py organize ~/Photos ~/Albums                    # preview only
    python imagevault.py organize ~/Photos ~/Albums --layout date-type --report preview.json
    python imagevault.py organize ~/Photos ~/Albums --apply            # copy after confirmation
    python imagevault.py organize ~/Photos ~/Albums --apply --mode move
    python imagevault.py undo ~/Albums                                 # revert last run
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

try:  # Pillow is optional: without it we fall back to filename/mtime dates.
    from PIL import Image, UnidentifiedImageError
    PIL_OK = True
except ImportError:  # pragma: no cover
    PIL_OK = False
    UnidentifiedImageError = OSError  # type: ignore

# ----------------------------------------------------------------- constants
IMAGE_EXTENSIONS = {
    ".jpg": "JPEG", ".jpeg": "JPEG", ".png": "PNG", ".gif": "GIF",
    ".bmp": "BMP", ".tif": "TIFF", ".tiff": "TIFF", ".webp": "WEBP",
    ".heic": "HEIC",
}
LAYOUTS = ("date", "type", "camera", "date-type")
JOURNAL_NAME = ".imagevault_journal.json"
DUP_FOLDER = "_Duplicates"
FILENAME_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})[-_]?(0[1-9]|1[0-2])[-_]?(0[1-9]|[12]\d|3[01])(?!\d)")


# ---------------------------------------------------------------- exceptions
class ImageVaultError(Exception):
    """Base class for all application-level errors."""


class ValidationError(ImageVaultError):
    """Raised when user input (paths, options) is invalid."""


# ---------------------------------------------------------------- data model
@dataclass
class ImageRecord:
    path: Path
    size: int
    file_type: str
    taken: datetime
    date_source: str                    # exif | filename | file-modified
    camera: str = "Unknown Camera"
    width: Optional[int] = None
    height: Optional[int] = None
    sha256: Optional[str] = None
    warnings: list = field(default_factory=list)


@dataclass
class PlanItem:
    src: Path
    dst: Optional[Path]
    action: str                         # copy | move | skip-duplicate
    reason: str


# ------------------------------------------------------------------- helpers
def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return f"{n:.1f} GB"


def safe_name(text: str) -> str:
    """Make a string safe to use as a folder name."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")
    return cleaned or "Unknown"


def validate_paths(source: Path, dest: Path) -> tuple[Path, Path]:
    source, dest = source.expanduser().resolve(), dest.expanduser().resolve()
    if not source.exists():
        raise ValidationError(f"Source folder does not exist: {source}")
    if not source.is_dir():
        raise ValidationError(f"Source is not a folder: {source}")
    if source == dest:
        raise ValidationError("Source and destination must be different folders.")
    if dest.exists() and not dest.is_dir():
        raise ValidationError(f"Destination exists and is not a folder: {dest}")
    return source, dest


# ----------------------------------------------------------------- metadata
def _parse_exif_date(value) -> Optional[datetime]:
    try:
        return datetime.strptime(str(value).strip(), "%Y:%m:%d %H:%M:%S")
    except (ValueError, TypeError):
        return None


def _date_from_filename(name: str) -> Optional[datetime]:
    m = FILENAME_DATE.search(name)
    if not m:
        return None
    try:
        return datetime(int(m[1]), int(m[2]), int(m[3]))
    except ValueError:
        return None


def extract_metadata(path: Path) -> ImageRecord:
    """Build an ImageRecord. Date priority: EXIF > filename > file modified time."""
    st = path.stat()
    rec = ImageRecord(
        path=path, size=st.st_size, file_type=IMAGE_EXTENSIONS[path.suffix.lower()],
        taken=datetime.fromtimestamp(st.st_mtime), date_source="file-modified",
    )
    exif_date = None
    if PIL_OK:
        try:
            with Image.open(path) as img:
                rec.width, rec.height = img.size
                exif = img.getexif()
                if exif:
                    exif_date = (_parse_exif_date(exif.get_ifd(0x8769).get(36867))   # DateTimeOriginal
                                 or _parse_exif_date(exif.get(306)))                 # DateTime
                    make, model = str(exif.get(271, "")).strip(), str(exif.get(272, "")).strip()
                    if model:
                        rec.camera = model if make and make in model else f"{make} {model}".strip()
        except (UnidentifiedImageError, OSError, ValueError, SyntaxError) as exc:
            rec.warnings.append(f"Unreadable metadata ({exc.__class__.__name__})")
    else:
        rec.warnings.append("Pillow not installed - EXIF skipped")

    if exif_date:
        rec.taken, rec.date_source = exif_date, "exif"
    else:
        fn_date = _date_from_filename(path.name)
        if fn_date:
            rec.taken, rec.date_source = fn_date, "filename"
    return rec


# ----------------------------------------------------------------- scanning
def scan_directory(source: Path, exclude: Optional[Path] = None):
    """Return (records, skipped_non_images, errors). Skips hidden files and `exclude`."""
    records, skipped, errors = [], 0, []
    for p in sorted(source.rglob("*")):
        try:
            if exclude and (p == exclude or exclude in p.parents):
                continue
            if not p.is_file() or p.is_symlink() or any(part.startswith(".") for part in p.relative_to(source).parts):
                continue
            if p.suffix.lower() not in IMAGE_EXTENSIONS:
                skipped += 1
                continue
            records.append(extract_metadata(p))
        except (PermissionError, OSError) as exc:
            errors.append(f"{p}: {exc}")
    return records, skipped, errors


def compute_hash(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def find_duplicates(records: list[ImageRecord]) -> tuple[dict, list]:
    """Group identical files. Hash only files that share a size (fast pre-filter)."""
    by_size = defaultdict(list)
    for r in records:
        by_size[r.size].append(r)
    groups, errors = defaultdict(list), []
    for same_size in by_size.values():
        if len(same_size) < 2:
            continue
        for r in same_size:
            try:
                r.sha256 = compute_hash(r.path)
                groups[r.sha256].append(r)
            except OSError as exc:
                errors.append(f"{r.path}: cannot hash ({exc})")
    return {h: g for h, g in groups.items() if len(g) > 1}, errors


# ----------------------------------------------------------------- planning
def album_folder(rec: ImageRecord, layout: str) -> Path:
    y, m = f"{rec.taken.year}", f"{rec.taken.year}-{rec.taken.month:02d}"
    if layout == "date":
        return Path(y) / m
    if layout == "type":
        return Path(rec.file_type)
    if layout == "camera":
        return Path(safe_name(rec.camera))
    return Path(y) / m / rec.file_type          # date-type


def _unique(dst: Path, reserved: set) -> Path:
    cand, i = dst, 1
    while cand in reserved or cand.exists():
        cand = dst.with_name(f"{dst.stem}_{i}{dst.suffix}")
        i += 1
    reserved.add(cand)
    return cand


def build_plan(records, dup_groups, dest: Path, layout: str, mode: str, dup_action: str) -> list[PlanItem]:
    duplicates = set()
    originals = {}
    for group in dup_groups.values():
        keep = min(group, key=lambda r: (r.taken, str(r.path)))   # earliest photo is the original
        for r in group:
            if r is not keep:
                duplicates.add(r.path)
                originals[r.path] = keep.path

    plan, reserved = [], set()
    for rec in sorted(records, key=lambda r: (r.taken, str(r.path))):
        if rec.path in duplicates:
            if dup_action == "skip":
                plan.append(PlanItem(rec.path, None, "skip-duplicate", f"duplicate of {originals[rec.path].name}"))
            else:
                dst = _unique(dest / DUP_FOLDER / rec.path.name, reserved)
                plan.append(PlanItem(rec.path, dst, mode, f"duplicate of {originals[rec.path].name}"))
        else:
            dst = _unique(dest / album_folder(rec, layout) / rec.path.name, reserved)
            plan.append(PlanItem(rec.path, dst, mode, f"{layout} album ({rec.date_source} date)"))
    return plan


# ------------------------------------------------------------------ reports
def build_report(source, dest, records, skipped, dup_groups, plan, errors, layout, mode) -> dict:
    wasted = sum(r.size for g in dup_groups.values() for r in sorted(g, key=lambda r: (r.taken, str(r.path)))[1:])
    albums = Counter(str(p.dst.parent.relative_to(dest)) for p in plan if p.dst)
    return {
        "generated": datetime.now().isoformat(timespec="seconds"),
        "source": str(source), "destination": str(dest), "layout": layout, "mode": mode,
        "images_found": len(records), "total_size": sum(r.size for r in records),
        "non_image_files_skipped": skipped,
        "by_type": dict(Counter(r.file_type for r in records)),
        "date_sources": dict(Counter(r.date_source for r in records)),
        "duplicate_groups": len(dup_groups),
        "duplicate_files": sum(len(g) - 1 for g in dup_groups.values()),
        "wasted_space": wasted,
        "albums": dict(sorted(albums.items())),
        "operations": [{"action": p.action, "src": str(p.src), "dst": str(p.dst) if p.dst else "",
                        "reason": p.reason} for p in plan],
        "warnings": [f"{r.path.name}: {w}" for r in records for w in r.warnings],
        "errors": errors,
    }


def print_report(rep: dict, verbose: bool = False) -> None:
    line = "=" * 64
    print(f"\n{line}\n  IMAGEVAULT PREVIEW REPORT  (no files have been changed)\n{line}")
    print(f"Source       : {rep['source']}\nDestination  : {rep['destination']}")
    print(f"Layout/Mode  : {rep['layout']} / {rep['mode']}")
    print(f"\nImages found : {rep['images_found']} ({human_size(rep['total_size'])})")
    print(f"Other files skipped: {rep['non_image_files_skipped']}")
    print("By type      : " + ", ".join(f"{k}={v}" for k, v in sorted(rep["by_type"].items())))
    print("Date source  : " + ", ".join(f"{k}={v}" for k, v in sorted(rep["date_sources"].items())))
    print(f"\nDuplicates   : {rep['duplicate_files']} redundant file(s) in {rep['duplicate_groups']} group(s), "
          f"{human_size(rep['wasted_space'])} wasted")
    print(f"\nAlbum folders to be created ({len(rep['albums'])}):")
    for name, count in rep["albums"].items():
        print(f"  {name:<40} {count:>4} file(s)")
    ops = rep["operations"]
    shown = ops if verbose else ops[:15]
    print(f"\nPlanned operations ({len(ops)}){'' if verbose else ', first 15 shown (use --verbose for all)'}:")
    for op in shown:
        target = op["dst"] or "-"
        print(f"  [{op['action']:<14}] {Path(op['src']).name}  ->  {target}\n{'':19}({op['reason']})")
    for title, items in (("Warnings", rep["warnings"]), ("Errors", rep["errors"])):
        if items:
            print(f"\n{title} ({len(items)}):")
            for i in items[: (None if verbose else 10)]:
                print(f"  ! {i}")
    print(line)


def save_report(rep: dict, path: Path) -> None:
    """Write the report as .json, .csv (operations table) or plain text."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        ext = path.suffix.lower()
        if ext == ".json":
            path.write_text(json.dumps(rep, indent=2), encoding="utf-8")
        elif ext == ".csv":
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.DictWriter(fh, fieldnames=["action", "src", "dst", "reason"])
                w.writeheader()
                w.writerows(rep["operations"])
        else:
            import contextlib, io
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                print_report(rep, verbose=True)
            path.write_text(buf.getvalue(), encoding="utf-8")
        print(f"Report saved to {path}")
    except OSError as exc:
        raise ImageVaultError(f"Could not save report: {exc}") from exc


# ---------------------------------------------------------------- execution
def execute_plan(plan: list[PlanItem], dest: Path) -> tuple[list[dict], list[str]]:
    done, failed = [], []
    for item in plan:
        if item.action == "skip-duplicate" or item.dst is None:
            continue
        try:
            item.dst.parent.mkdir(parents=True, exist_ok=True)
            if item.action == "copy":
                shutil.copy2(item.src, item.dst)
            else:
                shutil.move(str(item.src), str(item.dst))
            done.append({"action": item.action, "src": str(item.src), "dst": str(item.dst)})
        except (OSError, shutil.Error) as exc:
            failed.append(f"{item.src.name}: {exc}")
    if done:
        (dest / JOURNAL_NAME).write_text(json.dumps(done, indent=2), encoding="utf-8")
    return done, failed


def undo_last_run(dest: Path) -> int:
    dest = dest.expanduser().resolve()
    journal = dest / JOURNAL_NAME
    if not journal.is_file():
        raise ValidationError(f"No journal found in {dest}; nothing to undo.")
    try:
        entries = json.loads(journal.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ImageVaultError(f"Journal is unreadable: {exc}") from exc
    reverted = 0
    for e in reversed(entries):
        src, dst = Path(e["src"]), Path(e["dst"])
        try:
            if not dst.exists():
                continue
            if e["action"] == "copy":
                dst.unlink()
            elif not src.exists():
                src.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(dst), str(src))
            reverted += 1
        except OSError as exc:
            print(f"  ! could not revert {dst.name}: {exc}")
    for d in sorted((p for p in dest.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True):
        try:
            d.rmdir()                       # only succeeds when empty
        except OSError:
            pass
    journal.unlink()
    return reverted


# --------------------------------------------------------------------- main
def cmd_organize(args) -> int:
    source, dest = validate_paths(Path(args.source), Path(args.destination))
    print(f"Scanning {source} ...")
    records, skipped, errors = scan_directory(source, exclude=dest)
    if not records:
        print("No images found. Nothing to do.")
        return 0
    dup_groups, hash_errors = find_duplicates(records)
    plan = build_plan(records, dup_groups, dest, args.layout, args.mode, args.duplicates)
    report = build_report(source, dest, records, skipped, dup_groups, plan,
                          errors + hash_errors, args.layout, args.mode)
    print_report(report, args.verbose)
    if args.report:
        save_report(report, Path(args.report))

    if not args.apply:
        print("\nPreview only. Re-run with --apply to perform these operations.")
        return 0
    n_ops = sum(1 for p in plan if p.dst)
    if not args.yes:
        answer = input(f"\nAbout to {args.mode} {n_ops} file(s). Type YES to continue: ").strip()
        if answer != "YES":
            print("Cancelled. No files were changed.")
            return 0
    done, failed = execute_plan(plan, dest)
    print(f"\nDone: {len(done)} file(s) {args.mode}d, {len(failed)} failed.")
    for f in failed:
        print(f"  ! {f}")
    print(f"Undo with: python imagevault.py undo \"{dest}\"")
    return 1 if failed else 0


def cmd_undo(args) -> int:
    n = undo_last_run(Path(args.destination))
    print(f"Reverted {n} operation(s).")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="imagevault", description="Photo metadata and album organizer")
    sub = p.add_subparsers(dest="command", required=True)
    o = sub.add_parser("organize", help="preview (default) or perform organization")
    o.add_argument("source"); o.add_argument("destination")
    o.add_argument("--layout", choices=LAYOUTS, default="date", help="album structure (default: date)")
    o.add_argument("--mode", choices=("copy", "move"), default="copy", help="default: copy (safe)")
    o.add_argument("--duplicates", choices=("skip", "separate"), default="separate",
                   help="skip duplicates or put them in a _Duplicates folder (default)")
    o.add_argument("--apply", action="store_true", help="actually perform operations")
    o.add_argument("--yes", action="store_true", help="skip the confirmation prompt")
    o.add_argument("--report", help="save report to .json / .csv / .txt")
    o.add_argument("--verbose", action="store_true")
    o.set_defaults(func=cmd_organize)
    u = sub.add_parser("undo", help="revert the last --apply run")
    u.add_argument("destination")
    u.set_defaults(func=cmd_undo)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except ImageVaultError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Files already processed remain; use 'undo' if needed.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
