"""Toplu video üretimi: uv run python batch.py <girdi.csv|.txt> --preset presets/uzay.toml"""

from app.services import batch_production

if __name__ == "__main__":
    from app.utils.logging_utils import force_utf8_console

    force_utf8_console()
    raise SystemExit(batch_production.main())
