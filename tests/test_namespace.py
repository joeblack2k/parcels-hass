from pathlib import Path
import json
import sys
import types

REPO_DIR = Path(__file__).resolve().parents[1]
PACKAGE_DIR = REPO_DIR / "custom_components" / "package_inbox"

custom_components = types.ModuleType("custom_components")
custom_components.__path__ = [str(REPO_DIR / "custom_components")]
package_inbox = types.ModuleType("custom_components.package_inbox")
package_inbox.__path__ = [str(PACKAGE_DIR)]
sys.modules.setdefault("custom_components", custom_components)
sys.modules.setdefault("custom_components.package_inbox", package_inbox)

from custom_components.package_inbox.const import DOMAIN


def test_legacy_home_assistant_namespace_stays_package_inbox():
    manifest = json.loads((PACKAGE_DIR / "manifest.json").read_text())

    assert DOMAIN == "package_inbox"
    assert manifest["domain"] == "package_inbox"
    assert manifest["name"] == "Parcels for Home Assistant"
