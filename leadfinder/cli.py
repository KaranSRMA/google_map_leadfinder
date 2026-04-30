from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional

from .exporters import export_json, export_xlsx
from .models import DEFAULT_LIMIT, DEFAULT_PAUSE, DependencyError, EXPORT_DIR, Lead, MAX_LIMIT, PRIORITY_ORDER
from .scraper import GoogleMapsLeadFinder
from .web import evaluate_website


def print_banner() -> None:
    print("=" * 72)
    print("Google Maps Lead Finder")
    print("=" * 72)
    print("This tool uses a real browser to collect public business details.")
    print("If Google shows a consent or verification screen, solve it in the")
    print("browser window and then continue in the terminal.")
    print()


def read_input(prompt: str, eof_message: str) -> str:
    try:
        return input(prompt)
    except EOFError as exc:
        raise RuntimeError(eof_message) from exc


def prompt_non_empty(prompt: str, min_length: int = 2, default: Optional[str] = None) -> str:
    while True:
        suffix = f" [{default}]" if default else ""
        value = read_input(f"{prompt}{suffix}: ", "Interactive input ended before setup was finished.").strip()
        if not value and default is not None:
            return default
        if len(value) >= min_length:
            return value
        print(f"Please enter at least {min_length} characters.")


def prompt_int(
    prompt: str,
    default: int,
    min_value: int = 1,
    max_value: int = MAX_LIMIT,
) -> int:
    while True:
        value = read_input(f"{prompt} [{default}]: ", "Interactive input ended before setup was finished.").strip()
        if not value:
            return default
        if not value.isdigit():
            print("Please enter a whole number.")
            continue
        number = int(value)
        if min_value <= number <= max_value:
            return number
        print(f"Please choose a value between {min_value} and {max_value}.")


def prompt_float(
    prompt: str,
    default: float,
    min_value: float = 0.5,
    max_value: float = 10.0,
) -> float:
    while True:
        value = read_input(f"{prompt} [{default}]: ", "Interactive input ended before setup was finished.").strip()
        if not value:
            return default
        try:
            number = float(value)
        except ValueError:
            print("Please enter a valid number.")
            continue
        if min_value <= number <= max_value:
            return number
        print(f"Please choose a value between {min_value} and {max_value}.")


def prompt_choice(prompt: str, choices: tuple[str, ...], default: str) -> str:
    choices_text = "/".join(choices)
    while True:
        value = read_input(
            f"{prompt} [{choices_text}, default {default}]: ",
            "Interactive input ended before setup was finished.",
        ).strip().lower()
        if not value:
            return default
        if value in choices:
            return value
        print(f"Please choose one of: {choices_text}.")


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip().lower()).strip("_")
    return cleaned or "leads"


def save_leads(leads: list[Lead], keyword: str, location: str, output_format: str) -> list[Path]:
    EXPORT_DIR.mkdir(exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    base_name = f"{timestamp}_{slugify(keyword)}_{slugify(location)}"
    written_files: list[Path] = []

    if output_format in {"xlsx", "both"}:
        xlsx_path = EXPORT_DIR / f"{base_name}.xlsx"
        export_xlsx(xlsx_path, leads)
        written_files.append(xlsx_path)

    if output_format in {"json", "both"}:
        json_path = EXPORT_DIR / f"{base_name}.json"
        export_json(json_path, leads)
        written_files.append(json_path)

    return written_files


def collect_settings() -> tuple[str, str, int, float, str]:
    keyword = prompt_non_empty("Business type or keyword", min_length=3)
    location = prompt_non_empty("Location(city,state,country,zip code)", min_length=2)
    limit = prompt_int("Maximum leads to collect", default=DEFAULT_LIMIT)
    pause_seconds = prompt_float("Delay between requests in seconds", default=DEFAULT_PAUSE)
    output_format = prompt_choice("Output format", ("xlsx", "json", "both"), "xlsx")
    return keyword, location, limit, pause_seconds, output_format


def main() -> None:
    print_banner()
    keyword, location, limit, pause_seconds, output_format = collect_settings()
    query = f"{keyword} in {location}"

    finder = None
    try:
        finder = GoogleMapsLeadFinder(pause_seconds=pause_seconds)
        print()
        print(f"Using {finder.browser_name.title()} browser.")
        print(f"Searching for: {query}")
        finder.start()
        finder.run_search(query)

        result_links = finder.collect_result_links(limit)
        if not result_links:
            raise RuntimeError("No result links were found. Try a broader keyword or location.")

        print(f"Found {len(result_links)} place links. Starting detail collection...")
        leads: list[Lead] = []
        seen_urls: set[str] = set()
        skipped_working_sites = 0

        for index, url in enumerate(result_links, start=1):
            print(f"[{index}/{len(result_links)}] Scraping place details...")
            try:
                lead = finder.scrape_place(url)
            except finder.TimeoutException:
                print("  Skipped because the place page did not load in time.")
                continue
            except finder.WebDriverException as exc:
                print(f"  Skipped because the browser reported an error: {exc.__class__.__name__}")
                continue

            if lead.maps_url in seen_urls:
                continue
            seen_urls.add(lead.maps_url)

            decision = evaluate_website(lead.website)
            if not decision.keep:
                skipped_working_sites += 1
                print("  Skipped because the business has a working website.")
                time.sleep(pause_seconds)
                continue

            lead.priority = decision.priority
            lead.priority_reason = decision.priority_reason
            lead.website_status = decision.website_status
            lead.website_type = decision.website_type
            lead.website_http_status = decision.website_http_status
            lead.website = decision.website or lead.website

            leads.append(lead)
            print(f"  Kept as {lead.priority} priority - {lead.priority_reason}")
            time.sleep(pause_seconds)

        if not leads:
            raise RuntimeError("No qualifying leads were kept. The scraped businesses mostly had working websites.")

        leads.sort(key=lambda lead: (PRIORITY_ORDER.get(lead.priority, 99), lead.business_name.lower()))

        written_files = save_leads(leads, keyword, location, output_format)
        print()
        print(
            f"Saved {len(leads)} qualified leads. "
            f"Skipped {skipped_working_sites} businesses with working websites."
        )
        for file_path in written_files:
            print(f"  -> {file_path}")

    except DependencyError as exc:
        print()
        print(str(exc))
        print()
        print("Selenium is required because Google Maps is a dynamic website.")
    except Exception as exc:
        print()
        print(f"Error: {exc}")
        print("Tip: try a broader search, a slower delay, or solve any Google verification screen first.")
    finally:
        if finder is not None:
            finder.close()
