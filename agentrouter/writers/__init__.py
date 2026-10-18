"""Config writers: read the model a tool uses and point it at another one, preserving file formatting."""
import os
import shutil
import tempfile
import time
from pathlib import Path

from ..config import DATA_DIR


def backup(path: Path) -> Path:
    dest_dir = DATA_DIR / "backups"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{path.name}.{time.strftime('%Y%m%d-%H%M%S')}"
    shutil.copy2(path, dest)
    # keep the 20 newest backups per file
    olds = sorted(dest_dir.glob(f"{path.name}.*"))[:-20]
    for old in olds:
        old.unlink(missing_ok=True)
    return dest


def atomic_write(path: Path, text: str):
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
        shutil.copymode(path, tmp)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
