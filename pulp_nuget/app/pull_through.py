"""Lazy NuGet v3 metadata discovery and Pulp-managed on-demand artifacts."""

import hashlib
import json
import os
import re
from urllib.parse import urljoin, urlsplit

from aiohttp import ClientError, ClientResponseError, web
from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.cache import cache
from django.db import transaction
from django.utils.dateparse import parse_datetime

from pulpcore.plugin.models import ContentArtifact, RemoteArtifact

from .nuspec import InvalidNupkgError, canonical_version, parse_package_filter

PACKAGE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}$", re.IGNORECASE)


def _url(value):
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.netloc
        or parsed.username
        or parsed.password
    ):
        raise web.HTTPBadGateway(reason="The upstream feed returned an invalid resource URL.")
    return value


def _cache_key(remote, url):
    # Remote changes, including credentials and filtering, invalidate metadata.
    identity = f"{remote.pk}:{getattr(remote, 'pulp_last_updated', '')}:{remote.url}:{url}"
    return "nuget:v3:" + hashlib.sha256(identity.encode()).hexdigest()


def download_metadata(remote, url):
    # content_handler runs in a Django worker thread without an asyncio loop.
    # Construct the remote downloader on the content app's active async loop.
    async def download():
        return await remote.get_downloader(url=url).run()

    return async_to_sync(download)()


def fetch_json(remote, url):
    """Use the remote's downloader (authentication, TLS and proxy configuration)."""
    url = _url(url)
    key = _cache_key(remote, url)
    data = cache.get(key)
    if data is not None:
        return data[1]
    try:
        result = download_metadata(remote, url)
    except ClientResponseError as exc:
        if exc.status == 404:
            cache.set(key, (False, None), timeout=30)
            return None
        if exc.status in (401, 403):
            raise web.HTTPBadGateway(reason="The upstream NuGet feed denied access.") from exc
        raise web.HTTPBadGateway(reason="The upstream NuGet feed request failed.") from exc
    except (ClientError, OSError, TimeoutError) as exc:
        raise web.HTTPBadGateway(reason="The upstream NuGet feed is unavailable.") from exc
    try:
        with open(result.path, encoding="utf-8-sig") as fp:
            data = json.load(fp)
    except (ValueError, OSError) as exc:
        raise web.HTTPBadGateway(reason="The upstream NuGet feed returned invalid JSON.") from exc
    finally:
        if os.path.exists(result.path):
            os.unlink(result.path)
    if not isinstance(data, dict):
        raise web.HTTPBadGateway(reason="The upstream NuGet feed returned invalid metadata.")
    cache.set(key, (True, data), timeout=getattr(settings, "NUGET_METADATA_CACHE_TTL", 300))
    return data


def resource(remote, types, required=True):
    index = fetch_json(remote, remote.url)
    for kind in types:
        resources = (index or {}).get("resources", [])
        if not isinstance(resources, list):
            raise web.HTTPBadGateway(reason="The upstream NuGet service index is invalid.")
        for item in resources:
            if (
                isinstance(item, dict)
                and item.get("@type") == kind
                and isinstance(item.get("@id"), str)
            ):
                return _url(urljoin(remote.url, item["@id"])).rstrip("/") + "/"
    if required:
        raise web.HTTPBadGateway(reason="The upstream NuGet feed lacks a required v3 resource.")
    return None


def allowed(remote, package_id, version=None):
    """An empty include list permits all IDs in cache mode, unlike explicit sync."""
    if not PACKAGE_ID.fullmatch(package_id):
        return False

    def matches(entry, exclude=False):
        if entry.strip() == "*":
            return True
        name, version_range = parse_package_filter(entry)
        if name != package_id.lower():
            return False
        if version is None:
            return version_range is None if exclude else True
        return version_range is None or (
            version_range.contains(version)
            if exclude
            else version_range.matches_for_include(version)
        )

    return (not remote.includes or any(matches(e) for e in remote.includes)) and not any(
        matches(e, exclude=True) for e in remote.excludes
    )


def remote_for(distribution):
    return distribution.remote.cast() if getattr(distribution, "remote_id", None) else None


def versions(distribution, package_id):
    remote = remote_for(distribution)
    if not remote or not allowed(remote, package_id):
        return []
    base = resource(remote, ("PackageBaseAddress/3.0.0",))
    data = fetch_json(remote, f"{base}{package_id.lower()}/index.json")
    output = []
    entries = (data or {}).get("versions", [])
    if not isinstance(entries, list):
        raise web.HTTPBadGateway(reason="The upstream NuGet version list is invalid.")
    for version in entries:
        if not isinstance(version, str):
            continue
        try:
            normalized = canonical_version(version)
        except (InvalidNupkgError, ValueError, TypeError):
            continue
        if allowed(remote, package_id, normalized):
            output.append(normalized)
    return output


def package_from_entry(entry):
    """Map upstream registrations to the same representation used by local builders."""
    from .models import NugetPackageContent
    from .tasks.synchronizing import _as_string, _dependency_groups

    timestamp = entry.get("published")
    try:
        published = parse_datetime(timestamp) if isinstance(timestamp, str) else None
    except ValueError:
        published = None
    # nuget.org's 1900 timestamp is an unlisted marker, not a release date.
    if published and published.year <= 1900:
        published = None
    return NugetPackageContent(
        package_id=entry["id"],
        package_id_lower=entry["id"].lower(),
        version=entry["version"],
        version_normalized=canonical_version(entry["version"]),
        authors=_as_string(entry.get("authors")),
        description=entry.get("description") or "",
        title=entry.get("title") or "",
        summary=entry.get("summary") or "",
        tags=_as_string(entry.get("tags"), separator=" "),
        project_url=entry.get("projectUrl") or "",
        icon_url=entry.get("iconUrl") or "",
        license_expression=entry.get("licenseExpression") or "",
        license_url=entry.get("licenseUrl") or "",
        require_license_acceptance=bool(entry.get("requireLicenseAcceptance")),
        min_client_version=entry.get("minClientVersion") or "",
        dependency_groups=_dependency_groups(entry),
        listed=entry.get("listed") is not False,
        published=published,
    )


def packages(distribution, package_id):
    remote = remote_for(distribution)
    if not remote or not allowed(remote, package_id):
        return []
    base = resource(
        remote,
        ("RegistrationsBaseUrl/3.6.0", "RegistrationsBaseUrl/3.4.0", "RegistrationsBaseUrl"),
        required=False,
    )
    if not base:
        return []
    index = fetch_json(remote, f"{base}{package_id.lower()}/index.json")
    output = []
    pages = (index or {}).get("items", [])
    if not isinstance(pages, list):
        raise web.HTTPBadGateway(reason="The upstream NuGet registrations are invalid.")
    for page in pages:
        if not isinstance(page, dict):
            continue
        if "items" not in page and not isinstance(page.get("@id"), str):
            continue
        if "items" not in page:
            page = fetch_json(remote, _url(urljoin(base, page["@id"]))) or {}
        leaves = page.get("items", [])
        if not isinstance(leaves, list):
            continue
        for leaf in leaves:
            if not isinstance(leaf, dict):
                continue
            entry = leaf.get("catalogEntry", {})
            if isinstance(entry, str):
                entry = fetch_json(remote, _url(urljoin(base, entry))) or {}
            if (
                not isinstance(entry, dict)
                or not isinstance(entry.get("id"), str)
                or not isinstance(entry.get("version"), str)
            ):
                continue
            if entry["id"].lower() != package_id.lower():
                continue
            try:
                package = package_from_entry(entry)
            except (InvalidNupkgError, ValueError, KeyError, TypeError):
                continue
            if allowed(remote, package_id, package.version_normalized):
                output.append(package)
    return output


def artifact(distribution, package_id, version, filename):
    """Prepare one deferred artifact; pulpcore streams, validates and stores its bytes."""
    from .models import NugetPackageContent

    remote = remote_for(distribution)
    if not remote or not allowed(remote, package_id, version):
        return None
    try:
        version = canonical_version(version)
    except InvalidNupkgError:
        return None
    package_id = package_id.lower()
    if filename.lower() != f"{package_id}.{version}.nupkg":
        return None
    if version not in versions(distribution, package_id):
        return None
    metadata = next(
        (p for p in packages(distribution, package_id) if p.version_normalized == version), None
    )
    if metadata is None:
        metadata = NugetPackageContent(
            package_id=package_id,
            package_id_lower=package_id,
            version=version,
            version_normalized=version,
        )
    defaults = {
        field.name: getattr(metadata, field.name)
        for field in NugetPackageContent._meta.local_fields
        if field.name not in ("content_ptr", "_pulp_domain")
    }
    defaults.pop("package_id_lower", None)
    defaults.pop("version_normalized", None)
    # The same package may be requested concurrently or already belong to another
    # repository. Natural keys and get_or_create preserve one shared content unit.
    base = resource(remote, ("PackageBaseAddress/3.0.0",))
    with transaction.atomic():
        package, _ = NugetPackageContent.objects.get_or_create(
            package_id_lower=package_id,
            version_normalized=version,
            _pulp_domain=distribution.pulp_domain,
            defaults=defaults,
        )
        ca, _ = ContentArtifact.objects.get_or_create(
            content=package,
            relative_path=package.relative_path,
        )
        RemoteArtifact.objects.get_or_create(
            content_artifact=ca,
            remote=remote,
            defaults={"url": base + package.relative_path},
        )
    if distribution.repository_id:
        distribution.repository.cast().pull_through_add_content(ca)
    return ca


def manifest(distribution, package_id, version, filename):
    """Serve a cold .nuspec without downloading the corresponding .nupkg."""
    remote = remote_for(distribution)
    if not remote or not allowed(remote, package_id, version):
        return None
    try:
        version = canonical_version(version)
    except InvalidNupkgError:
        return None
    if filename.lower() != f"{package_id.lower()}.nuspec":
        return None
    base = resource(remote, ("PackageBaseAddress/3.0.0",))
    url = f"{base}{package_id.lower()}/{version}/{package_id.lower()}.nuspec"
    try:
        result = download_metadata(remote, url)
    except ClientResponseError as exc:
        if exc.status == 404:
            return None
        raise web.HTTPBadGateway(reason="The upstream NuGet manifest request failed.") from exc
    except (ClientError, OSError, TimeoutError) as exc:
        raise web.HTTPBadGateway(reason="The upstream NuGet feed is unavailable.") from exc
    try:
        with open(result.path, "rb") as fp:
            return web.Response(body=fp.read(), content_type="application/xml")
    finally:
        if os.path.exists(result.path):
            os.unlink(result.path)
