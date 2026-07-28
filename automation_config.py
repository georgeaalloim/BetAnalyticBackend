from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _env_text(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default

    cleaned = value.strip()
    return cleaned if cleaned else default


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or not value.strip():
        return default

    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False

    raise ValueError(f"Μη έγκυρη boolean τιμή: {value!r}.")


def parse_seasons(value: str | None) -> tuple[int, ...] | None:
    """
    Διαβάζει λίστα σεζόν όπως ``2024,2025``.

    Κενή τιμή ή ``auto`` σημαίνει αυτόματη επιλογή των διαθέσιμων
    σεζόν από την τοπική βάση και τις ενεργές πηγές.
    """

    if value is None:
        return None

    cleaned = value.strip()
    if not cleaned or cleaned.lower() == "auto":
        return None

    seasons: list[int] = []
    for part in cleaned.split(","):
        item = part.strip()
        if not item:
            continue

        season = int(item)
        if season < 2000 or season > 2100:
            raise ValueError(f"Μη έγκυρη σεζόν: {season}.")
        if season not in seasons:
            seasons.append(season)

    if not seasons:
        return None

    return tuple(sorted(seasons))


@dataclass(frozen=True)
class AutomationConfig:
    league_id: int
    league_name: str
    output_dir: Path
    sync_seasons: tuple[int, ...] | None
    include_next_season: bool
    lookahead_days: int
    upcoming_statuses: tuple[str, ...]
    r2_account_id: str
    r2_access_key_id: str
    r2_secret_access_key: str
    r2_bucket_name: str
    r2_public_base_url: str
    r2_prefix: str

    @classmethod
    def from_environment(
        cls,
        output_dir: str | Path = "automation_output",
        sync_seasons_override: str | None = None,
        lookahead_days_override: int | None = None,
    ) -> "AutomationConfig":
        lookahead_days = (
            lookahead_days_override
            if lookahead_days_override is not None
            else int(_env_text("BETANALYTIC_LOOKAHEAD_DAYS", "45"))
        )
        if lookahead_days < 1 or lookahead_days > 365:
            raise ValueError(
                "Το BETANALYTIC_LOOKAHEAD_DAYS πρέπει να είναι από 1 έως 365."
            )

        raw_statuses = _env_text(
            "BETANALYTIC_UPCOMING_STATUSES",
            "NS,TBD,PST",
        )
        upcoming_statuses = tuple(
            status.strip().upper()
            for status in raw_statuses.split(",")
            if status.strip()
        )
        if not upcoming_statuses:
            raise ValueError("Πρέπει να οριστεί τουλάχιστον ένα upcoming status.")

        raw_seasons = (
            sync_seasons_override
            if sync_seasons_override is not None
            else os.getenv("BETANALYTIC_SYNC_SEASONS")
        )

        return cls(
            league_id=int(_env_text("BETANALYTIC_LEAGUE_ID", "197")),
            league_name=_env_text(
                "BETANALYTIC_LEAGUE_NAME",
                "Super League 1",
            ),
            output_dir=Path(output_dir).expanduser().resolve(),
            sync_seasons=parse_seasons(raw_seasons),
            include_next_season=parse_bool(
                os.getenv("BETANALYTIC_INCLUDE_NEXT_SEASON"),
                default=True,
            ),
            lookahead_days=lookahead_days,
            upcoming_statuses=upcoming_statuses,
            r2_account_id=_env_text("R2_ACCOUNT_ID"),
            r2_access_key_id=_env_text("R2_ACCESS_KEY_ID"),
            r2_secret_access_key=_env_text("R2_SECRET_ACCESS_KEY"),
            r2_bucket_name=_env_text("R2_BUCKET_NAME"),
            r2_public_base_url=_env_text("R2_PUBLIC_BASE_URL").rstrip("/"),
            r2_prefix=_env_text("R2_PREFIX", "betanalytic").strip("/"),
        )

    @property
    def r2_is_configured(self) -> bool:
        return all(
            (
                self.r2_account_id,
                self.r2_access_key_id,
                self.r2_secret_access_key,
                self.r2_bucket_name,
                self.r2_public_base_url,
            )
        )

    def public_object_url(self, file_name: str) -> str:
        key = "/".join(
            part
            for part in (self.r2_prefix, file_name.lstrip("/"))
            if part
        )
        if not self.r2_public_base_url:
            return key
        return f"{self.r2_public_base_url}/{key}"
