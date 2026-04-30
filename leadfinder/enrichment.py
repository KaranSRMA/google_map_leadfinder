from __future__ import annotations

import re

from .models import ContactCandidate, Lead, WebsiteCandidate
from .web import (
    dedupe_contacts,
    dedupe_urls,
    extract_contacts,
    extract_external_websites,
    fetch_page_snapshot,
    is_directory_website,
    is_messageable_social,
    is_social_website,
    normalize_hostname,
    search_web,
    score_business_domain,
)


class LeadEnricher:
    def __init__(self, search_limit: int = 6, profile_limit: int = 4) -> None:
        self.search_limit = search_limit
        self.profile_limit = profile_limit

    def enrich(self, lead: Lead, reference_links: list[str]) -> Lead:
        social_profiles, directory_profiles, search_websites = self.collect_initial_links(lead, reference_links)
        search_links = self.search_candidate_links(lead, social_profiles, directory_profiles)
        more_social, more_directories, more_websites = self.partition_search_links(lead, search_links)

        social_profiles = dedupe_urls(social_profiles + more_social)
        directory_profiles = dedupe_urls(directory_profiles + more_directories)
        website_candidates = self.build_website_candidates(lead, search_websites + more_websites)

        snapshots = self.fetch_profile_snapshots(social_profiles, directory_profiles)
        website_candidates.extend(self.extract_website_candidates_from_snapshots(lead, snapshots))

        message_profiles = self.collect_message_profiles(social_profiles)
        contact_candidates = self.collect_contact_candidates(lead, snapshots)

        selected_website, website_source = self.choose_website(lead, social_profiles, website_candidates)
        if selected_website and not is_social_website(selected_website):
            official_snapshot = fetch_page_snapshot(selected_website)
            if official_snapshot is not None:
                contact_candidates.extend(extract_contacts(official_snapshot, "website", 90))
                social_profiles = dedupe_urls(social_profiles + self.extract_social_links([official_snapshot]))
                message_profiles = self.collect_message_profiles(social_profiles)

        contacts = dedupe_contacts(contact_candidates)
        primary_contact, contact_source = self.choose_primary_contact(contacts, message_profiles)

        lead.website = selected_website
        lead.website_source = website_source
        lead.social_profiles = "\n".join(social_profiles)
        lead.directory_profiles = "\n".join(directory_profiles)
        lead.message_profiles = "\n".join(message_profiles)
        lead.contact = primary_contact
        lead.contact_source = contact_source
        lead.all_contacts = self.format_all_contacts(contacts, message_profiles)
        return lead

    def collect_initial_links(self, lead: Lead, reference_links: list[str]) -> tuple[list[str], list[str], list[str]]:
        social_profiles: list[str] = []
        directory_profiles: list[str] = []
        search_websites: list[str] = []

        if lead.website:
            if is_social_website(lead.website):
                social_profiles.append(lead.website)
            else:
                search_websites.append(lead.website)

        for link in reference_links:
            if not link.startswith(("http://", "https://")):
                continue
            if is_social_website(link):
                social_profiles.append(link)
            elif is_directory_website(link):
                directory_profiles.append(link)

        return dedupe_urls(social_profiles), dedupe_urls(directory_profiles), dedupe_urls(search_websites)

    def should_search(self, lead: Lead, social_profiles: list[str], directory_profiles: list[str]) -> bool:
        if not lead.website or is_social_website(lead.website):
            return True
        if not lead.contact:
            return True
        return False

    def build_search_queries(self, lead: Lead) -> list[str]:
        location_bits = [part.strip() for part in lead.address.split(",") if part.strip()]
        location_hint = " ".join(location_bits[:2])
        business = f"\"{lead.business_name}\""
        if location_hint:
            return [
                f"{business} \"{location_hint}\" website contact",
                f"{business} \"{location_hint}\" facebook instagram justdial",
            ]
        return [
            f"{business} website contact",
            f"{business} facebook instagram justdial",
        ]

    def search_candidate_links(self, lead: Lead, social_profiles: list[str], directory_profiles: list[str]) -> list[str]:
        if not self.should_search(lead, social_profiles, directory_profiles):
            return []

        links: list[str] = []
        for query in self.build_search_queries(lead):
            links.extend(search_web(query, limit=self.search_limit))
            if len(dedupe_urls(links)) >= self.search_limit:
                break
        return dedupe_urls(links)

    def partition_search_links(self, lead: Lead, search_links: list[str]) -> tuple[list[str], list[str], list[str]]:
        social_profiles: list[str] = []
        directory_profiles: list[str] = []
        websites: list[str] = []

        for link in search_links:
            if is_social_website(link):
                social_profiles.append(link)
            elif is_directory_website(link):
                directory_profiles.append(link)
            elif score_business_domain(link, lead.business_name) > 0:
                websites.append(link)

        return dedupe_urls(social_profiles), dedupe_urls(directory_profiles), dedupe_urls(websites)

    def build_website_candidates(self, lead: Lead, urls: list[str]) -> list[WebsiteCandidate]:
        candidates: list[WebsiteCandidate] = []

        for url in dedupe_urls(urls):
            source = "maps" if url == lead.website else "search"
            base_rank = 100 if source == "maps" else 72
            candidates.append(
                WebsiteCandidate(
                    url=url,
                    source=source,
                    rank=base_rank + score_business_domain(url, lead.business_name),
                )
            )
        return candidates

    def fetch_profile_snapshots(self, social_profiles: list[str], directory_profiles: list[str]):
        snapshots = []
        for url in dedupe_urls(directory_profiles + social_profiles)[: self.profile_limit]:
            snapshot = fetch_page_snapshot(url)
            if snapshot is not None:
                snapshots.append(snapshot)
        return snapshots

    def extract_website_candidates_from_snapshots(self, lead: Lead, snapshots) -> list[WebsiteCandidate]:
        candidates: list[WebsiteCandidate] = []
        for snapshot in snapshots:
            source_label = self.describe_source_url(snapshot.final_url)
            rank_base = 88 if is_directory_website(snapshot.final_url) else 84
            candidates.extend(extract_external_websites(snapshot, lead.business_name, source_label, rank_base))
        return candidates

    def collect_contact_candidates(self, lead: Lead, snapshots) -> list[ContactCandidate]:
        candidates: list[ContactCandidate] = []
        if lead.contact:
            candidates.append(
                ContactCandidate(
                    value=lead.contact,
                    kind=self.detect_contact_kind(lead.contact),
                    source="maps",
                    rank=100,
                )
            )

        for snapshot in snapshots:
            source_label = self.describe_source_url(snapshot.final_url)
            rank_base = 78 if is_directory_website(snapshot.final_url) else 66
            candidates.extend(extract_contacts(snapshot, source_label, rank_base))

        return candidates

    def choose_website(
        self,
        lead: Lead,
        social_profiles: list[str],
        website_candidates: list[WebsiteCandidate],
    ) -> tuple[str, str]:
        deduped: dict[str, WebsiteCandidate] = {}
        for candidate in website_candidates:
            key = candidate.url.rstrip("/")
            current = deduped.get(key)
            if current is None or candidate.rank > current.rank:
                deduped[key] = candidate

        ranked = sorted(deduped.values(), key=lambda item: (-item.rank, item.url))
        if ranked:
            return ranked[0].url, ranked[0].source

        if lead.website and is_social_website(lead.website):
            return lead.website, "maps social"

        if social_profiles:
            first = social_profiles[0]
            return first, self.describe_dm_source(first) if is_messageable_social(first) else "social"

        return "", ""

    def choose_primary_contact(
        self,
        contacts: list[ContactCandidate],
        message_profiles: list[str],
    ) -> tuple[str, str]:
        if contacts:
            primary = contacts[0]
            return primary.value, f"{primary.source} {primary.kind}".strip()
        if message_profiles:
            first = message_profiles[0]
            return first, self.describe_dm_source(first)
        return "", ""

    def collect_message_profiles(self, social_profiles: list[str]) -> list[str]:
        return [url for url in dedupe_urls(social_profiles) if is_messageable_social(url)]

    def extract_social_links(self, snapshots) -> list[str]:
        social_profiles: list[str] = []
        for snapshot in snapshots:
            for link in snapshot.links:
                if link.startswith(("http://", "https://")) and is_social_website(link):
                    social_profiles.append(link)
        return dedupe_urls(social_profiles)

    def format_all_contacts(self, contacts: list[ContactCandidate], message_profiles: list[str]) -> str:
        lines = [f"{contact.kind}: {contact.value} ({contact.source})" for contact in contacts]
        if not lines and message_profiles:
            for profile in message_profiles:
                lines.append(f"message: {profile} ({self.describe_dm_source(profile)})")
        return "\n".join(lines)

    @staticmethod
    def detect_contact_kind(value: str) -> str:
        lowered = value.lower()
        if lowered.startswith("http") and ("whatsapp" in lowered or "wa.me/" in lowered or "wa.link/" in lowered):
            return "whatsapp"
        if "@" in lowered:
            return "email"
        return "phone"

    @staticmethod
    def describe_source_url(url: str) -> str:
        hostname = normalize_hostname(url)
        if "facebook" in hostname or hostname == "fb.com":
            return "facebook"
        if "instagram" in hostname:
            return "instagram"
        if "youtube" in hostname or hostname == "youtu.be":
            return "youtube"
        if "justdial" in hostname:
            return "justdial"
        if "sulekha" in hostname:
            return "sulekha"
        if "indiamart" in hostname:
            return "indiamart"
        return hostname

    @staticmethod
    def describe_dm_source(url: str) -> str:
        hostname = normalize_hostname(url)
        if "instagram" in hostname:
            return "instagram dm"
        if "facebook" in hostname or hostname == "fb.com":
            return "facebook dm"
        return "message profile"
