from pathlib import Path
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

from custom_components.package_inbox.carrier_rules import (
    detect_carrier,
    extract_tracking_code,
    normalize_carrier,
    valid_tracking_code,
)


def test_normalizes_parcel_app_nl_carrier_ids():
    assert normalize_carrier("dhlnl") == "dhl"
    assert normalize_carrier("dhlnlpcode") == "dhl"
    assert normalize_carrier("tntp") == "postnl"
    assert normalize_carrier("tntpit") == "postnl"
    assert normalize_carrier("chrono") == "chronopost"


def test_detects_carrier_from_tracking_urls():
    assert detect_carrier("https://www.fedex.com/fedextrack/?trknbr=871354982751") == "fedex"
    assert detect_carrier("https://my.dhlecommerce.nl/go-track-trace?tc=JJD000090254000059755497") == "dhl"
    assert detect_carrier("https://www.postnl.nl/tracktrace/?B=3SBVMS6743345") == "postnl"
    assert (
        detect_carrier("https://www.chronopost.fr/tracking-no-cms/suivi-page?listeNumerosLT=XU152297803JF")
        == "chronopost"
    )


def test_extracts_fedex_postnl_and_dhl_codes():
    assert extract_tracking_code("trknbr=871354982751", "fedex") == "871354982751"
    assert extract_tracking_code("Barcode 3SBVMS6743345", "postnl") == "3SBVMS6743345"
    assert extract_tracking_code("Zendingsnummer JJD000090254000059755497", "dhl") == "JJD000090254000059755497"
    assert extract_tracking_code("Your parcel XU152297803JF / 343431803222365", "chrono") == "XU152297803JF"


def test_validates_extended_fedex_shapes():
    assert valid_tracking_code("871354982751", "fedex")
    assert valid_tracking_code("9612345678901234567890", "fedex")
    assert not valid_tracking_code("12345", "fedex")
