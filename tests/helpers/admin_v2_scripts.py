"""Admin V2 browser scripts are split into classic-script parts; read them in template load order."""

from pathlib import Path
import re


ROOT = Path(__file__).parents[2]
TEMPLATE = ROOT / "resolver/templates/admin_v2.html"
STATIC_DIR = ROOT / "resolver/static/admin-v2"
SCRIPT_TAG = re.compile(r'<script defer src="/admin/assets-v2/([^"]+\.js)"></script>')


def admin_v2_parts(prefix: str = "admin-v2-") -> list[Path]:
    """Script paths whose file names start with prefix, in the order admin_v2.html loads them."""
    names = SCRIPT_TAG.findall(TEMPLATE.read_text(encoding="utf-8"))
    return [STATIC_DIR / name for name in names if name.startswith(prefix)]


def admin_v2_source(prefix: str = "admin-v2-") -> str:
    """Concatenated source of the matching Admin V2 parts, in template order."""
    return "\n".join(path.read_text(encoding="utf-8") for path in admin_v2_parts(prefix))
