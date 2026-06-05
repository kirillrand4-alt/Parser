"""Entry point: python -m monitor [OPTIONS]"""
from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

from .registry import ALL_SCRAPERS, get_scraper
from .storage import Storage, diff_runs

console = Console()

DATA_DIR = Path("data")
DB_PATH = DATA_DIR / "prices.db"
CSV_BASE = DATA_DIR / "prices"


def setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    # force=True replaces any handler a host environment (e.g. Colab/Jupyter)
    # already installed on the root logger; without it basicConfig is a no-op
    # there and our INFO lines stay hidden behind the host's WARNING-level setup.
    logging.basicConfig(
        level=level,
        format="%(message)s",
        handlers=[RichHandler(console=console, rich_tracebacks=True)],
        force=True,
    )
    # Silence noisy libraries
    for lib in ("requests", "urllib3", "charset_normalizer",
                "requests_cache", "requests_cache.backends", "requests_cache.policy"):
        logging.getLogger(lib).setLevel(logging.WARNING)


@click.group()
def cli() -> None:
    """Competitor price & specs monitor for pneumatic/compressor equipment."""


@cli.command("scrape")
@click.option("--site", default="all", show_default=True,
              help="Site to scrape: 'all' or domain name.")
@click.option("--out", default=None, help="CSV output path (default: data/prices_<run_id>.csv)")
@click.option("--db", default=str(DB_PATH), show_default=True, help="SQLite database path.")
@click.option("--parallel/--sequential", default=True, show_default=True,
              help="Scrape sites concurrently (one thread per site, each keeps its own delay).")
@click.option("--verbose", "-v", is_flag=True)
def scrape_cmd(site: str, out: str | None, db: str, parallel: bool, verbose: bool) -> None:
    """Scrape one or all competitor sites."""
    setup_logging(verbose)
    run_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    csv_path = Path(out) if out else Path(f"{CSV_BASE}_{run_id}.csv")

    if site == "all":
        scrapers_to_run = list(ALL_SCRAPERS.keys())
    else:
        if site not in ALL_SCRAPERS:
            console.print(f"[red]Unknown site: {site}. Available: {list(ALL_SCRAPERS)}")
            sys.exit(1)
        scrapers_to_run = [site]

    use_parallel = parallel and len(scrapers_to_run) > 1

    console.rule(f"[bold green]Run {run_id}")
    console.print(f"Sites: {scrapers_to_run}")
    console.print(f"Mode: {'parallel' if use_parallel else 'sequential'}")
    console.print(f"Output CSV: {csv_path}")
    console.print(f"Database:   {db}\n")

    def run_one(site_name: str, storage: Storage, position: int = 0) -> int:
        """Scrape a single site into shared storage. Returns product count."""
        try:
            scraper = get_scraper(site_name)
        except Exception as exc:
            console.print(f"[red]Failed to init scraper for {site_name}: {exc}")
            return 0
        count = 0
        try:
            for product in scraper.scrape(position=position):
                storage.write(product)
                count += 1
        except Exception as exc:
            console.print(f"[red]Scraper error for {site_name}: {exc}")
            logging.exception("Scraper error")
        finally:
            scraper.close()
        console.print(f"  [green]✓[/] {site_name}: {count} products")
        return count

    total_written = 0
    with Storage(Path(db), csv_path, run_id) as storage:
        if use_parallel:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            with ThreadPoolExecutor(max_workers=len(scrapers_to_run)) as pool:
                futures = {
                    pool.submit(run_one, s, storage, pos): s
                    for pos, s in enumerate(scrapers_to_run)
                }
                for fut in as_completed(futures):
                    total_written += fut.result()
        else:
            for site_name in scrapers_to_run:
                console.rule(f"[cyan]{site_name}")
                total_written += run_one(site_name, storage)

    console.rule("[bold green]Done")
    console.print(f"Total products written: [bold]{total_written}[/]")
    console.print(f"CSV:      {csv_path}")
    console.print(f"Database: {db}  (run_id={run_id})")

    # Summary table
    table = Table(title="Products by site")
    table.add_column("Site")
    table.add_column("Count", justify="right")
    for s, c in storage.counts().items():
        table.add_row(s, str(c))
    console.print(table)


@cli.command("diff")
@click.argument("run_a")
@click.argument("run_b")
@click.option("--db", default=str(DB_PATH), show_default=True)
@click.option("--out", default=None, help="Save diff JSON to file.")
def diff_cmd(run_a: str, run_b: str, db: str, out: str | None) -> None:
    """Compare two scrape runs. Show price changes and newly discontinued models.

    Example: python -m monitor diff 20240601_120000 20240602_120000
    """
    result = diff_runs(Path(db), run_a, run_b)

    console.rule(f"Diff: [cyan]{run_a}[/] → [cyan]{run_b}[/]")

    if result["price_changes"]:
        t = Table(title=f"Price changes ({len(result['price_changes'])})")
        t.add_column("Site")
        t.add_column("Name")
        t.add_column("Before", justify="right")
        t.add_column("After", justify="right")
        t.add_column("Δ%", justify="right")
        for ch in result["price_changes"][:50]:
            color = "red" if (ch["change_pct"] or 0) > 0 else "green"
            t.add_row(
                ch["site"], ch["name"][:50],
                f"{ch['price_before']:,.0f}",
                f"{ch['price_after']:,.0f}",
                f"[{color}]{ch['change_pct']:+.1f}%[/{color}]",
            )
        console.print(t)
    else:
        console.print("[green]No price changes detected.")

    if result["new_discontinued"]:
        t2 = Table(title=f"Newly discontinued ({len(result['new_discontinued'])})")
        t2.add_column("Site")
        t2.add_column("Name")
        t2.add_column("Was status")
        t2.add_column("Replacement")
        for d in result["new_discontinued"]:
            t2.add_row(d["site"], d["name"][:50], d["was"], d["replacement"] or "-")
        console.print(t2)

    if result["new_products"]:
        console.print(f"\n[blue]New products in {run_b}:[/] {len(result['new_products'])}")

    if out:
        Path(out).write_text(json.dumps(result, ensure_ascii=False, indent=2))
        console.print(f"Diff saved to {out}")


@cli.command("recon")
@click.option("--verbose", "-v", is_flag=True)
def recon_cmd(verbose: bool) -> None:
    """Probe all sites for YML feeds and server info."""
    setup_logging(verbose)
    from .recon import run_recon
    run_recon()


@cli.command("urls")
@click.option("--site", required=True, help="Site name, e.g. pnevmoteh.ru")
@click.option("--n", default=5, show_default=True, help="How many sample URLs.")
def urls_cmd(site: str, n: int) -> None:
    """Print sample product URLs from a site's sitemap (recon helper)."""
    setup_logging(False)
    from .inspect import sample_urls
    for u in sample_urls(site, n):
        print(u)


@cli.command("inspect")
@click.argument("url")
def inspect_cmd(url: str) -> None:
    """Dump price/specs/availability candidates for a product URL (recon helper)."""
    from .inspect import inspect_url
    inspect_url(url)


@cli.command("inspect-site")
@click.option("--site", required=True, help="Site name, e.g. rutector.ru")
@click.option("--n", default=8, show_default=True)
def inspect_site_cmd(site: str, n: int) -> None:
    """Sample product URLs from a site's sitemap and inspect the first one."""
    setup_logging(False)
    from .inspect import inspect_site
    inspect_site(site, n)


@cli.command("list-runs")
@click.option("--db", default=str(DB_PATH), show_default=True)
def list_runs_cmd(db: str) -> None:
    """List all scrape run IDs in the database."""
    import sqlite3
    conn = sqlite3.connect(db)
    rows = conn.execute(
        "SELECT run_id, COUNT(*) as cnt, MIN(scraped_at), MAX(scraped_at) "
        "FROM products GROUP BY run_id ORDER BY run_id DESC"
    ).fetchall()
    conn.close()
    t = Table(title="Scrape runs")
    t.add_column("Run ID")
    t.add_column("Products", justify="right")
    t.add_column("Started")
    t.add_column("Ended")
    for row in rows:
        t.add_row(*[str(v) for v in row])
    console.print(t)


if __name__ == "__main__":
    cli()
