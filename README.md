# ImageVault – Photo Metadata and Album Organizer

Organizes images by date and type, detects duplicates, and creates album folders.
**Safe by design:** it only prints a preview report until you pass `--apply` and type `YES`.

## Setup
    pip install -r requirements.txt     # Pillow optional (enables EXIF)

## Usage
    python imagevault.py organize PHOTOS ALBUMS                       # preview only
    python imagevault.py organize PHOTOS ALBUMS --layout date-type --report preview.json
    python imagevault.py organize PHOTOS ALBUMS --apply               # copy after confirmation
    python imagevault.py organize PHOTOS ALBUMS --apply --mode move --duplicates skip
    python imagevault.py undo ALBUMS                                  # revert last run
    python -m unittest -v                                             # run tests

Layouts: `date` (2023/2023-07), `type` (JPEG, PNG…), `camera`, `date-type`.

## How it works
| Concept | Where |
|---|---|
| Dataclasses (`ImageRecord`, `PlanItem`) | data model |
| dict/defaultdict/Counter/set | size & hash grouping, album counts, collision tracking |
| Metadata | EXIF → date in filename → file modified time |
| Duplicates | group by size, then SHA-256 (chunked); earliest photo kept as original |
| Validation | paths, source ≠ destination, extensions, confirmation prompt |
| Exceptions | `ImageVaultError`/`ValidationError`, per-file `OSError` handling, exit codes |
| Outputs | console report, `.json`/`.csv`/`.txt` report, undo journal |

## Ideas for extension
Event albums (group photos taken within N hours), GPS-based albums, perceptual-hash near-duplicates, a Tkinter GUI.
