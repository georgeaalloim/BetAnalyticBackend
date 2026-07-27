from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import boto3
from botocore.client import BaseClient

from automation_config import AutomationConfig


@dataclass(frozen=True)
class UploadItem:
    local_path: Path
    object_key: str
    content_type: str
    cache_control: str


class R2Publisher:
    """Ανεβάζει τα στατικά αρχεία του BetAnalytic σε Cloudflare R2."""

    def __init__(self, config: AutomationConfig) -> None:
        if not config.r2_is_configured:
            raise ValueError(
                "Λείπουν ρυθμίσεις R2. Έλεγξε τα R2_ACCOUNT_ID, "
                "R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY, "
                "R2_BUCKET_NAME και R2_PUBLIC_BASE_URL."
            )

        self.config = config
        self.client: BaseClient = boto3.client(
            service_name="s3",
            endpoint_url=(
                f"https://{config.r2_account_id}.r2.cloudflarestorage.com"
            ),
            aws_access_key_id=config.r2_access_key_id,
            aws_secret_access_key=config.r2_secret_access_key,
            region_name="auto",
        )

    def _full_key(self, object_key: str) -> str:
        return "/".join(
            part
            for part in (
                self.config.r2_prefix,
                object_key.strip("/"),
            )
            if part
        )

    def upload(self, item: UploadItem) -> str:
        if not item.local_path.is_file():
            raise FileNotFoundError(item.local_path)

        key = self._full_key(item.object_key)
        body = item.local_path.read_bytes()

        self.client.put_object(
            Bucket=self.config.r2_bucket_name,
            Key=key,
            Body=body,
            ContentType=item.content_type,
            CacheControl=item.cache_control,
        )

        return self.config.public_object_url(item.object_key)

    def upload_many(self, items: Iterable[UploadItem]) -> list[str]:
        return [self.upload(item) for item in items]
