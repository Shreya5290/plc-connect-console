from datetime import datetime
import re
import shutil
import stat
from pathlib import Path


_GENERATED_SPEC_PATTERN = re.compile(
    r"^Click2Connect_v\d{4}\.\d{2}\.\d{2}_\d{6}(?:_\d+)?_portable\.spec$"
)


def _is_reparse_point(path):
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(attributes & reparse_flag)


def create_build_version(base_dir, now=None):
    base_dir = Path(base_dir)
    dist_dir = base_dir / "dist_portable"
    timestamp = (now or datetime.now()).strftime("%Y.%m.%d_%H%M%S")
    version_file = base_dir / "version.txt"
    previous_version = (
        version_file.read_text(encoding="utf-8").strip()
        if version_file.exists()
        else ""
    )
    version = timestamp
    suffix = 0

    while (
        (dist_dir / f"Click2Connect_v{version}.exe").exists()
        or (base_dir / f"Click2Connect_v{version}_portable.spec").exists()
        or previous_version == version
        or previous_version.startswith(f"{timestamp}_")
    ):
        suffix += 1
        version = f"{timestamp}_{suffix:02d}"

    version_file.write_text(f"{version}\n", encoding="utf-8")
    return version


def cleanup_build_artifacts(base_dir, current_spec=None, current_work_dir=None):
    base_dir = Path(base_dir)
    current_spec = Path(current_spec).resolve() if current_spec else None
    errors = []

    if current_work_dir:
        current_work_dir = Path(current_work_dir)
        if current_work_dir.exists():
            if _is_reparse_point(current_work_dir):
                errors.append(f"{current_work_dir}: skipped reparse-point build directory")
            else:
                try:
                    shutil.rmtree(current_work_dir)
                except PermissionError as exc:
                    errors.append(f"{current_work_dir}: {exc}")

    for spec_file in base_dir.glob("Click2Connect_*_portable.spec"):
        if (
            _GENERATED_SPEC_PATTERN.fullmatch(spec_file.name)
            and (current_spec is None or spec_file.resolve() != current_spec)
        ):
            try:
                spec_file.unlink()
            except PermissionError as exc:
                errors.append(f"{spec_file}: {exc}")

    return errors
