from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .enrichment import LeadEnricher
from .hours import summarize_weekly_hours
from .models import DependencyError, Lead, PROFILE_DIR
from .web import normalize_outbound_url


def detect_browser() -> tuple[str, Optional[Path]]:
    candidates = (
        ("chrome", Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")),
        ("chrome", Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe")),
        ("edge", Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe")),
        ("edge", Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")),
    )
    for browser_name, browser_path in candidates:
        if browser_path.exists():
            return browser_name, browser_path
    return "chrome", None


def load_selenium() -> dict[str, object]:
    try:
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException, WebDriverException
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.webdriver.common.by import By
        from selenium.webdriver.edge.options import Options as EdgeOptions
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait
    except ImportError as exc:
        raise DependencyError(
            "Selenium is not installed yet.\n"
            "Install it with:\n"
            "  .\\lead_finder\\Scripts\\python.exe -m pip install selenium"
        ) from exc

    return {
        "webdriver": webdriver,
        "TimeoutException": TimeoutException,
        "WebDriverException": WebDriverException,
        "ChromeOptions": ChromeOptions,
        "EdgeOptions": EdgeOptions,
        "By": By,
        "EC": EC,
        "WebDriverWait": WebDriverWait,
    }


class GoogleMapsLeadFinder:
    def __init__(self, pause_seconds: float) -> None:
        modules = load_selenium()
        self.webdriver = modules["webdriver"]
        self.TimeoutException = modules["TimeoutException"]
        self.WebDriverException = modules["WebDriverException"]
        self.ChromeOptions = modules["ChromeOptions"]
        self.EdgeOptions = modules["EdgeOptions"]
        self.By = modules["By"]
        self.EC = modules["EC"]
        self.WebDriverWait = modules["WebDriverWait"]
        self.pause_seconds = pause_seconds
        self.timeout_seconds = 20
        self.driver = None
        self.browser_name, self.browser_binary = detect_browser()
        self.enricher = LeadEnricher()

    def start(self) -> None:
        PROFILE_DIR.mkdir(exist_ok=True)
        profile_dir = PROFILE_DIR / self.browser_name
        profile_dir.mkdir(exist_ok=True)

        if self.browser_name == "edge":
            options = self.EdgeOptions()
        else:
            options = self.ChromeOptions()

        options.add_argument("--start-maximized")
        options.add_argument("--disable-notifications")
        options.add_argument("--disable-blink-features=AutomationControlled")
        options.add_argument("--lang=en-US")
        options.add_argument(f"--user-data-dir={profile_dir}")

        if self.browser_binary is not None:
            options.binary_location = str(self.browser_binary)

        if self.browser_name == "edge":
            self.driver = self.webdriver.Edge(options=options)
        else:
            self.driver = self.webdriver.Chrome(options=options)

        self.driver.set_page_load_timeout(60)

    def close(self) -> None:
        if self.driver is not None:
            self.driver.quit()
            self.driver = None

    def wait(self, seconds: Optional[int] = None):
        return self.WebDriverWait(self.driver, seconds or self.timeout_seconds)

    def run_search(self, query: str) -> None:
        search_url = f"https://www.google.com/maps/search/?api=1&query={quote(query)}"
        self.driver.get(search_url)
        time.sleep(self.pause_seconds * 2)
        self.wait_for_results()

    def wait_for_results(self) -> None:
        for attempt in range(2):
            try:
                self.wait(25).until(
                    lambda drv: len(self.extract_result_links()) > 0 or "/maps/place/" in drv.current_url
                )
                return
            except self.TimeoutException:
                if attempt == 0:
                    print()
                    print("Google Maps may be waiting for consent or verification.")
                    input("Finish that in the browser, then press Enter to continue.")
                else:
                    raise

    def wait_for_visible(self, locators: list[tuple[object, str]], label: str):
        for attempt in range(2):
            for by, selector in locators:
                try:
                    return self.wait().until(self.EC.visibility_of_element_located((by, selector)))
                except self.TimeoutException:
                    continue
            if attempt == 0:
                print()
                print(f"I couldn't find the {label} yet.")
                input("If the browser needs manual confirmation, finish it and press Enter.")
        raise self.TimeoutException(f"Unable to find: {label}")

    def collect_result_links(self, limit: int) -> list[str]:
        links: list[str] = []
        seen: set[str] = set()
        stagnant_rounds = 0
        previous_count = 0

        while len(links) < limit and stagnant_rounds < 6:
            for href in self.extract_result_links():
                normalized = self.normalize_maps_url(href)
                if normalized and normalized not in seen:
                    seen.add(normalized)
                    links.append(normalized)

            if len(links) >= limit:
                break

            if len(seen) == previous_count:
                stagnant_rounds += 1
            else:
                stagnant_rounds = 0
            previous_count = len(seen)

            if not self.scroll_results():
                stagnant_rounds += 1

            time.sleep(self.pause_seconds)

        return links[:limit]

    def extract_result_links(self) -> list[str]:
        anchors = self.driver.find_elements(self.By.CSS_SELECTOR, "a[href*='/maps/place/']")
        links: list[str] = []
        for anchor in anchors:
            href = (anchor.get_attribute("href") or "").strip()
            if "/maps/place/" in href:
                links.append(href)
        return links

    def scroll_results(self) -> bool:
        feeds = self.driver.find_elements(self.By.CSS_SELECTOR, "div[role='feed']")
        if feeds:
            self.driver.execute_script("arguments[0].scrollTop = arguments[0].scrollHeight;", feeds[0])
            return True

        links = self.driver.find_elements(self.By.CSS_SELECTOR, "a[href*='/maps/place/']")
        if links:
            self.driver.execute_script("arguments[0].scrollIntoView({block: 'end'});", links[-1])
            return True

        return False

    def scrape_place(self, url: str) -> Lead:
        self.driver.get(url)
        name_element = self.wait_for_visible(
            [
                (self.By.CSS_SELECTOR, "h1.DUwDvf"),
                (self.By.CSS_SELECTOR, "h1.fontHeadlineLarge"),
                (self.By.CSS_SELECTOR, "h1"),
            ],
            "place title",
        )
        time.sleep(self.pause_seconds)

        rating, review_count = self.extract_rating_and_reviews()
        weekly_hours = self.extract_weekly_hours()
        live_timing = self.extract_timing()
        reference_links = self.extract_reference_links()
        website = self.extract_website()

        lead = Lead(
            priority="",
            priority_reason="",
            business_name=self.clean_text(name_element.text) or "Unknown",
            contact=self.extract_phone(),
            address=self.extract_address(),
            timing=summarize_weekly_hours(weekly_hours or live_timing, live_timing),
            website=website,
            website_source="maps" if website else "",
            website_status="",
            website_type="",
            website_http_status="",
            social_profiles="",
            directory_profiles="",
            message_profiles="",
            category=self.extract_category(),
            rating=rating,
            review_count=review_count,
            maps_url=self.driver.current_url,
        )
        return self.enricher.enrich(lead, reference_links)

    def extract_reference_links(self) -> list[str]:
        links: list[str] = []
        for anchor in self.driver.find_elements(self.By.CSS_SELECTOR, "a[href]"):
            href = normalize_outbound_url(anchor.get_attribute("href") or "")
            if href:
                links.append(href)
        return self.dedupe_urls(links)

    def extract_address(self) -> str:
        return self.extract_data_item("address")

    def extract_phone(self) -> str:
        selectors = (
            "button[data-item-id^='phone:tel:']",
            "button[data-item-id*='phone']",
            "a[data-item-id^='phone:tel:']",
        )
        for selector in selectors:
            elements = self.driver.find_elements(self.By.CSS_SELECTOR, selector)
            for element in elements:
                candidate = " ".join(
                    filter(
                        None,
                        [
                            self.clean_text(element.text),
                            self.clean_text(element.get_attribute("aria-label")),
                            self.clean_text(element.get_attribute("data-item-id")),
                        ],
                    )
                )
                match = re.search(r"\+?\d[\d\s().-]{7,}\d", candidate)
                if match:
                    return self.clean_text(match.group(0))
        return ""

    def extract_website(self) -> str:
        selectors = (
            "a[data-item-id='authority']",
            "a[data-item-id*='authority']",
            "a[aria-label^='Website:']",
        )
        for selector in selectors:
            elements = self.driver.find_elements(self.By.CSS_SELECTOR, selector)
            for element in elements:
                href = normalize_outbound_url(element.get_attribute("href") or "")
                if href:
                    return href
        return ""

    def extract_category(self) -> str:
        selectors = (
            "button[jsaction*='pane.rating.category']",
            "button.DkEaL",
            "div[role='main'] button[jsaction*='category']",
        )
        for selector in selectors:
            elements = self.driver.find_elements(self.By.CSS_SELECTOR, selector)
            for element in elements:
                text = self.clean_text(element.text)
                if text:
                    return text
        return ""

    def extract_timing(self) -> str:
        selectors = (
            "div.OqCZI.fontBodyMedium.WVXvdc div.MkV9",
            "div.OqCZI.fontBodyMedium.WVXvdc",
            "span[aria-label='Hours']",
        )
        for selector in selectors:
            elements = self.driver.find_elements(self.By.CSS_SELECTOR, selector)
            for element in elements:
                if selector == "span[aria-label='Hours']":
                    container = self.find_clickable_parent(element)
                    if container is None:
                        continue
                    text = self.clean_text(container.text)
                else:
                    text = self.clean_text(element.text)
                if text and any(word in text.lower() for word in ("open", "closed", "closes", "opens")):
                    return text
        return ""

    def extract_weekly_hours(self) -> str:
        hour_buttons = self.driver.find_elements(
            self.By.CSS_SELECTOR,
            "button.mWUh3d[aria-label*='Copy open hours']",
        )
        hours: list[str] = []
        for button in hour_buttons:
            label = self.clean_text(button.get_attribute("aria-label"))
            if not label:
                continue
            entry = re.sub(r",?\s*Copy open hours$", "", label, flags=re.IGNORECASE).strip()
            if entry and entry not in hours:
                hours.append(entry)
        return "\n".join(hours)

    def extract_rating_and_reviews(self) -> tuple[str, str]:
        rating = ""
        review_count = ""

        rating_selectors = (
            "span.MW4etd",
            "div.F7nice span[aria-hidden='true']",
            "div[role='img'][aria-label*='star']",
        )
        for selector in rating_selectors:
            elements = self.driver.find_elements(self.By.CSS_SELECTOR, selector)
            for element in elements:
                combined = " ".join(
                    filter(
                        None,
                        [
                            self.clean_text(element.text),
                            self.clean_text(element.get_attribute("aria-label")),
                        ],
                    )
                )
                match = re.search(r"(\d(?:\.\d)?)", combined)
                if match:
                    rating = match.group(1)
                    break
            if rating:
                break

        review_selectors = (
            "span.UY7F9",
            "button[jsaction*='pane.reviewChart.moreReviews']",
            "button[aria-label*='review']",
        )
        for selector in review_selectors:
            elements = self.driver.find_elements(self.By.CSS_SELECTOR, selector)
            for element in elements:
                combined = " ".join(
                    filter(
                        None,
                        [
                            self.clean_text(element.text),
                            self.clean_text(element.get_attribute("aria-label")),
                        ],
                    )
                )
                match = re.search(r"([\d,]+)\s*reviews?", combined, re.IGNORECASE)
                if match:
                    review_count = match.group(1)
                    break
                text_only = re.search(r"^\(?([\d,]+)\)?$", combined)
                if text_only:
                    review_count = text_only.group(1)
                    break
            if review_count:
                break

        source = self.driver.page_source
        if not rating:
            match = re.search(r"(\d(?:\.\d)?)\s*stars?", source, re.IGNORECASE)
            if match:
                rating = match.group(1)
        if not review_count:
            match = re.search(r"([\d,]+)\s*reviews?", source, re.IGNORECASE)
            if match:
                review_count = match.group(1)

        return rating, review_count

    def extract_data_item(self, key: str) -> str:
        selectors = (
            f"button[data-item-id='{key}']",
            f"button[data-item-id*='{key}']",
            f"a[data-item-id='{key}']",
            f"a[data-item-id*='{key}']",
        )
        label_prefix = f"{key.title()}:"

        for selector in selectors:
            elements = self.driver.find_elements(self.By.CSS_SELECTOR, selector)
            for element in elements:
                text = self.clean_text(element.text)
                if text:
                    return text
                aria = self.clean_text(element.get_attribute("aria-label"))
                if aria.lower().startswith(label_prefix.lower()):
                    return aria[len(label_prefix) :].strip()
        return ""

    def find_clickable_parent(self, element):
        current = element
        for _ in range(4):
            current = current.find_element(self.By.XPATH, "./..")
            role = self.clean_text(current.get_attribute("role")).lower()
            if role == "button":
                return current
        return None

    @staticmethod
    def clean_text(value: Optional[str]) -> str:
        text = re.sub(r"\s+", " ", value or "").strip()
        text = re.sub(r"[\uE000-\uF8FF]", "", text)
        return text.strip()

    @staticmethod
    def dedupe_urls(urls: list[str]) -> list[str]:
        deduped: list[str] = []
        seen: set[str] = set()
        for url in urls:
            normalized = url.rstrip("/")
            if normalized and normalized not in seen:
                seen.add(normalized)
                deduped.append(url)
        return deduped

    @staticmethod
    def normalize_maps_url(url: str) -> str:
        parsed = urlsplit(url)
        kept_query = [(key, value) for key, value in parse_qsl(parsed.query) if key not in {"authuser", "hl", "entry"}]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(kept_query), ""))
