"""Product Advertising API 5.0 source.

The sanctioned route for listing data and licensed image URLs. Requires an approved
Associates account with 3 qualifying sales in the trailing 180 days to retain access.
Signed with SigV4 directly so there is no third-party SDK in the trust path.
"""
from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import json
from pathlib import Path

import httpx

from ..config import Config, ConfigError
from ..models import Product
from . import canonical_url, download_images, ensure_tag, extract_asin, resolve_short_link

SERVICE = "ProductAdvertisingAPI"
TARGET = "com.amazon.paapi5.v1.ProductAdvertisingAPIv1.GetItems"
PATH = "/paapi5/getitems"
ALGORITHM = "AWS4-HMAC-SHA256"

MARKETPLACES = {
    "US": ("webservices.amazon.com", "us-east-1", "www.amazon.com"),
    "CA": ("webservices.amazon.ca", "us-east-1", "www.amazon.ca"),
    "UK": ("webservices.amazon.co.uk", "eu-west-1", "www.amazon.co.uk"),
    "DE": ("webservices.amazon.de", "eu-west-1", "www.amazon.de"),
    "FR": ("webservices.amazon.fr", "eu-west-1", "www.amazon.fr"),
    "IT": ("webservices.amazon.it", "eu-west-1", "www.amazon.it"),
    "ES": ("webservices.amazon.es", "eu-west-1", "www.amazon.es"),
    "JP": ("webservices.amazon.co.jp", "us-west-2", "www.amazon.co.jp"),
    "AU": ("webservices.amazon.com.au", "us-west-2", "www.amazon.com.au"),
}

RESOURCES = [
    "ItemInfo.Title",
    "ItemInfo.Features",
    "ItemInfo.ByLineInfo",
    "ItemInfo.ProductInfo",
    "Offers.Listings.Price",
    "Images.Primary.Large",
    "Images.Variants.Large",
]


def _sign(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def _signing_key(secret: str, datestamp: str, region: str) -> bytes:
    k = _sign(f"AWS4{secret}".encode("utf-8"), datestamp)
    k = _sign(k, region)
    k = _sign(k, SERVICE)
    return _sign(k, "aws4_request")


class PaapiSource:
    name = "paapi"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        missing = [k for k in ("access_key", "secret_key") if not cfg.paapi.get(k)]
        if missing:
            raise ConfigError(f"[paapi] missing {', '.join(missing)} in config.toml")
        self.access_key = cfg.paapi["access_key"]
        self.secret_key = cfg.paapi["secret_key"]
        self.partner_tag = cfg.paapi.get("partner_tag") or cfg.require_affiliate_tag()
        locale = cfg.paapi.get("marketplace", "US").upper()
        if locale not in MARKETPLACES:
            raise ConfigError(f"[paapi] marketplace must be one of {', '.join(MARKETPLACES)}")
        self.host, self.region, self.marketplace = MARKETPLACES[locale]

    # ----------------------------------------------------------------- #

    def fetch(self, affiliate_url: str, job_dir: Path) -> Product:
        resolved = resolve_short_link(affiliate_url)
        asin = extract_asin(resolved) or extract_asin(affiliate_url)
        if not asin:
            raise ValueError(f"could not find an ASIN in {affiliate_url!r}")

        item = self._get_item(asin)
        info = item.get("ItemInfo", {})
        title = info.get("Title", {}).get("DisplayValue", f"Amazon product {asin}")
        bullets = info.get("Features", {}).get("DisplayValues", []) or []
        brand = (
            info.get("ByLineInfo", {}).get("Brand", {}).get("DisplayValue")
            or info.get("ByLineInfo", {}).get("Manufacturer", {}).get("DisplayValue")
            or ""
        )
        listing = (item.get("Offers", {}).get("Listings") or [{}])[0]
        price = listing.get("Price", {}).get("DisplayAmount", "")

        urls = self._image_urls(item.get("Images", {}))
        images = download_images(urls, job_dir / "assets")
        if not images:
            raise RuntimeError(f"PA-API returned no usable images for {asin}")

        detail = item.get("DetailPageURL") or canonical_url(asin, self.partner_tag)
        return Product(
            asin=asin,
            title=title,
            affiliate_url=ensure_tag(detail, self.partner_tag),
            images=images,
            bullets=[b for b in bullets if b.strip()],
            brand=brand,
            price=price,
            source=self.name,
        )

    # ----------------------------------------------------------------- #

    def _get_item(self, asin: str) -> dict:
        body = json.dumps(
            {
                "ItemIds": [asin],
                "Resources": RESOURCES,
                "PartnerTag": self.partner_tag,
                "PartnerType": "Associates",
                "Marketplace": self.marketplace,
            },
            separators=(",", ":"),
        )
        headers = self._signed_headers(body)
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(f"https://{self.host}{PATH}", content=body, headers=headers)
        if resp.status_code != 200:
            raise RuntimeError(f"PA-API {resp.status_code}: {resp.text[:500]}")
        data = resp.json()
        if errors := data.get("Errors"):
            raise RuntimeError(f"PA-API error: {errors[0].get('Message', errors)}")
        items = data.get("ItemsResult", {}).get("Items") or []
        if not items:
            raise RuntimeError(f"PA-API returned no item for {asin}")
        return items[0]

    def _signed_headers(self, body: str) -> dict[str, str]:
        now = dt.datetime.now(dt.timezone.utc)
        amzdate = now.strftime("%Y%m%dT%H%M%SZ")
        datestamp = now.strftime("%Y%m%d")

        canonical_headers = (
            "content-encoding:amz-1.0\n"
            f"host:{self.host}\n"
            f"x-amz-date:{amzdate}\n"
            f"x-amz-target:{TARGET}\n"
        )
        signed_headers = "content-encoding;host;x-amz-date;x-amz-target"
        payload_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        canonical_request = "\n".join(
            ["POST", PATH, "", canonical_headers, signed_headers, payload_hash]
        )

        scope = f"{datestamp}/{self.region}/{SERVICE}/aws4_request"
        string_to_sign = "\n".join(
            [
                ALGORITHM,
                amzdate,
                scope,
                hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
            ]
        )
        signature = hmac.new(
            _signing_key(self.secret_key, datestamp, self.region),
            string_to_sign.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        return {
            "Content-Type": "application/json; charset=UTF-8",
            "Content-Encoding": "amz-1.0",
            "Host": self.host,
            "X-Amz-Date": amzdate,
            "X-Amz-Target": TARGET,
            "Authorization": (
                f"{ALGORITHM} Credential={self.access_key}/{scope}, "
                f"SignedHeaders={signed_headers}, Signature={signature}"
            ),
        }

    @staticmethod
    def _image_urls(images: dict) -> list[str]:
        urls: list[str] = []
        if primary := images.get("Primary", {}).get("Large", {}).get("URL"):
            urls.append(primary)
        for variant in images.get("Variants", []) or []:
            if url := variant.get("Large", {}).get("URL"):
                urls.append(url)
        seen: set[str] = set()
        return [u for u in urls if not (u in seen or seen.add(u))][:10]
