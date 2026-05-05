from pathlib import Path

import yaml

REPO_DIR = Path(__file__).resolve().parents[1]
ADDON_DIR = REPO_DIR / "addons" / "parcels_fedex_scraper"


def test_fedex_scraper_addon_metadata_is_present():
    config = yaml.safe_load((ADDON_DIR / "config.yaml").read_text())
    repository = yaml.safe_load((REPO_DIR / "repository.yaml").read_text())

    assert repository["name"] == "Parcels for Home Assistant Add-ons"
    assert config["slug"] == "parcels_fedex_scraper"
    assert config["name"] == "Parcels FedEx Scraper"
    assert config["stage"] == "experimental"
    assert "amd64" in config["arch"]
    assert config["ports"]["8765/tcp"] is None
    assert config["schema"]["scraper_token"] == "password?"
    assert config["schema"]["headless"] == "bool"


def test_fedex_scraper_addon_has_runtime_files():
    dockerfile = (ADDON_DIR / "Dockerfile").read_text()
    run_script = ADDON_DIR / "run.sh"
    app = ADDON_DIR / "app" / "main.py"

    assert "mcr.microsoft.com/playwright/python" in dockerfile
    assert "aiohttp>=3.10,<4" in dockerfile
    assert 'CMD ["/run.sh"]' in dockerfile
    assert run_script.exists()
    assert app.exists()
    app_text = app.read_text()
    assert 'Path("/data/options.json")' in app_text
    assert 'return ["0.0.0.0", "::"]' in app_text
    assert 'addon_options.get("timeout")' in app_text
