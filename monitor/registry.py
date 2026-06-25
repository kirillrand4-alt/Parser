"""Registry of all site scrapers."""
from __future__ import annotations

from .scrapers.compressortyt import CompressortytScraper
from .scrapers.pnevmo_sklad import PnevmoSkladScraper
from .scrapers.pnevmoteh import PnevmotehScraper
from .scrapers.rutector import RutectorScraper
from .scrapers.aerocompressors import AerocompressorsScraper
from .scrapers.vpk import VpkScraper
from .scrapers.air_energy import AirEnergyScraper
from .scrapers.parangonv import ParangonvScraper
from .scrapers.ekk_kompressor import EkkKompressorScraper
from .base_scraper import BaseScraper

ALL_SCRAPERS: dict[str, type[BaseScraper]] = {
    "compressortyt.ru": CompressortytScraper,
    "pnevmo-sklad.ru": PnevmoSkladScraper,
    "pnevmoteh.ru": PnevmotehScraper,
    "rutector.ru": RutectorScraper,
    "aerocompressors.ru": AerocompressorsScraper,
    "v-p-k.ru": VpkScraper,
    "air-energy.ru": AirEnergyScraper,
    "parangonv.ru": ParangonvScraper,
    "ekk-kompressor.ru": EkkKompressorScraper,
}


def get_scraper(site: str) -> BaseScraper:
    cls = ALL_SCRAPERS.get(site)
    if cls is None:
        raise ValueError(f"Unknown site: {site!r}. Available: {list(ALL_SCRAPERS)}")
    return cls()
