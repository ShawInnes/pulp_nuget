"""NuGet cold-cache discovery, metadata freshness, and Pulp URL rewriting."""

import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import ClientResponseError, web

from pulp_nuget.app import pull_through, v3_api

from .test_v3_api import BASE, _stub


@pytest.fixture
def remote():
    return SimpleNamespace(
        pk="remote-1",
        pulp_last_updated="2026-11-10T00:00:00Z",
        url="https://upstream.test/v3/index.json",
        includes=[],
        excludes=[],
        get_downloader=Mock(return_value=SimpleNamespace(run=AsyncMock())),
    )


@pytest.fixture
def distribution(remote):
    return SimpleNamespace(remote_id=remote.pk, remote=SimpleNamespace(cast=lambda: remote))


@pytest.fixture
def metadata_cache(monkeypatch):
    stored = {}
    backend = SimpleNamespace(
        get=lambda key: stored.get(key),
        set=Mock(side_effect=lambda key, value, timeout: stored.__setitem__(key, value)),
    )
    monkeypatch.setattr(pull_through, "cache", backend)
    return backend


def test_metadata_cache_reuses_response_but_invalidates_on_remote_change(
    remote, metadata_cache, monkeypatch, tmp_path
):
    path = tmp_path / "metadata.json"
    path.write_text('{"versions": ["1.0.0"]}')
    remote.get_downloader.return_value.run.return_value = SimpleNamespace(path=str(path))
    monkeypatch.setattr(pull_through.settings, "NUGET_METADATA_CACHE_TTL", 123, raising=False)
    url = "https://upstream.test/flat/example/index.json"
    assert pull_through.fetch_json(remote, url) == {"versions": ["1.0.0"]}
    assert not path.exists(), "Downloader metadata files must not accumulate on disk"
    assert pull_through.fetch_json(remote, url) == {"versions": ["1.0.0"]}
    remote.get_downloader.assert_called_once_with(url=url)
    assert metadata_cache.set.call_args.kwargs["timeout"] == 123

    remote.pulp_last_updated = "2026-11-10T00:01:00Z"
    path.write_text('{"versions": ["1.0.0", "2.0.0"]}')
    assert pull_through.fetch_json(remote, url)["versions"] == ["1.0.0", "2.0.0"]
    assert remote.get_downloader.call_count == 2


@pytest.mark.parametrize("body", ["not json", "[]", "null"])
def test_metadata_rejects_invalid_json_and_removes_temp_files(
    remote, metadata_cache, tmp_path, body
):
    path = tmp_path / "invalid.json"
    path.write_text(body)
    remote.get_downloader.return_value.run.return_value = SimpleNamespace(path=str(path))
    with pytest.raises(web.HTTPBadGateway):
        pull_through.fetch_json(remote, remote.url)
    assert not path.exists()
    metadata_cache.set.assert_not_called()


@pytest.mark.parametrize("status", [401, 403, 500])
def test_upstream_failure_is_not_reported_as_package_absence(remote, metadata_cache, status):
    remote.get_downloader.return_value.run.side_effect = ClientResponseError(
        request_info=Mock(), history=(), status=status
    )
    with pytest.raises(web.HTTPBadGateway):
        pull_through.fetch_json(remote, remote.url)
    metadata_cache.set.assert_not_called()


def test_unknown_upstream_package_has_short_negative_cache(remote, metadata_cache):
    remote.get_downloader.return_value.run.side_effect = ClientResponseError(
        request_info=Mock(), history=(), status=404
    )
    assert pull_through.fetch_json(remote, remote.url) is None
    assert pull_through.fetch_json(remote, remote.url) is None
    assert remote.get_downloader.call_count == 1
    assert metadata_cache.set.call_args.args[1] == (False, None)
    assert metadata_cache.set.call_args.kwargs["timeout"] == 30


@pytest.mark.parametrize(
    "url", ["file:///etc/passwd", "ftp://feed.test/x", "https://u:p@feed.test/x"]
)
def test_metadata_rejects_unsupported_resource_urls(remote, metadata_cache, url):
    with pytest.raises(web.HTTPBadGateway):
        pull_through.fetch_json(remote, url)
    remote.get_downloader.assert_not_called()


def test_service_discovery_prefers_semver2_and_resolves_relative_resources(remote, monkeypatch):
    monkeypatch.setattr(
        pull_through,
        "fetch_json",
        lambda *_: {
            "resources": [
                {"@type": "RegistrationsBaseUrl", "@id": "https://legacy.test/registrations"},
                {"@type": "RegistrationsBaseUrl/3.6.0", "@id": "../semver2"},
            ]
        },
    )
    assert (
        pull_through.resource(remote, ("RegistrationsBaseUrl/3.6.0", "RegistrationsBaseUrl"))
        == "https://upstream.test/semver2/"
    )
    assert pull_through.resource(remote, ("missing",), required=False) is None
    with pytest.raises(web.HTTPBadGateway):
        pull_through.resource(remote, ("missing",))


@pytest.mark.parametrize(
    "includes, excludes, package_id, version, expected",
    [
        ([], [], "New.Dependency", "1.0.0", True),
        ([], ["New.Dependency"], "New.Dependency", None, False),
        (["Example [1.0,2.0)"], [], "example", None, True),
        (["Example [1.0,2.0)"], [], "example", "1.5.0", True),
        (["Example [1.0,2.0)"], [], "example", "2.0.0", False),
        (["Example"], [], "Other", "1.0.0", False),
        (["*"], ["Example (,2.0)"], "example", "2.0.0-beta", False),
        (["*"], ["Example (,2.0)"], "example", "2.0.0", True),
        ([], [], "../example", "1.0.0", False),
        ([], [], "example/other", "1.0.0", False),
    ],
)
def test_cache_filters_are_applied_to_ids_and_versions(
    remote, includes, excludes, package_id, version, expected
):
    remote.includes, remote.excludes = includes, excludes
    assert pull_through.allowed(remote, package_id, version) is expected


def test_versions_normalize_nuget_versions_and_apply_range_filters(
    remote, distribution, monkeypatch
):
    remote.includes = ["Example [1.0,2.0)"]
    remote.excludes = ["Example [1.1]"]
    monkeypatch.setattr(pull_through, "resource", lambda *_: "https://upstream.test/flat/")
    fetch = Mock(return_value={"versions": ["01.0.0.0+BUILD", "1.1", "1.2.0.5", "2.0.0"]})
    monkeypatch.setattr(pull_through, "fetch_json", fetch)
    assert pull_through.versions(distribution, "Example") == ["1.0.0", "1.2.0.5"]
    fetch.assert_called_once_with(remote, "https://upstream.test/flat/example/index.json")


@pytest.mark.parametrize(
    "published, expected",
    [
        ("2020-01-02T03:04:05Z", datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)),
        ("1900-01-01T00:00:00Z", None),
        (None, None),
        ("invalid", None),
    ],
)
def test_package_age_uses_publication_date_not_unlisted_sentinel(monkeypatch, published, expected):
    from pulp_nuget.app import models

    monkeypatch.setattr(models, "NugetPackageContent", lambda **kwargs: SimpleNamespace(**kwargs))
    package = pull_through.package_from_entry(
        {
            "id": "Example",
            "version": "01.0.0.0",
            "published": published,
            "authors": ["One", "Two"],
            "tags": ["one", "two"],
            "listed": False,
            "dependencyGroups": [
                {
                    "targetFramework": "net8.0",
                    "dependencies": [
                        {"id": "New.Dependency", "range": "[1.0.0,)"},
                    ],
                }
            ],
        }
    )
    assert package.published == expected
    assert package.version_normalized == "1.0.0"
    assert package.package_id_lower == "example"
    assert package.listed is False
    assert package.authors == "One, Two"
    assert package.tags == "one two"
    assert package.dependency_groups[0]["dependencies"][0]["id"] == "New.Dependency"


def test_registration_discovery_follows_external_pages_and_catalog_urls(
    distribution, remote, monkeypatch
):
    from pulp_nuget.app import models

    monkeypatch.setattr(models, "NugetPackageContent", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr(
        pull_through, "resource", lambda *_args, **_kwargs: "https://upstream.test/reg/"
    )
    data = {
        "https://upstream.test/reg/example/index.json": {
            "items": [
                {"@id": "https://pages.test/example/page.json"},
                {"items": [{"catalogEntry": {"id": "Other", "version": "1.0.0"}}]},
            ]
        },
        "https://pages.test/example/page.json": {
            "items": [
                {"catalogEntry": "https://catalog.test/example/1.0.json"},
                {"catalogEntry": {"id": "Example", "version": "2.0.0"}},
            ]
        },
        "https://catalog.test/example/1.0.json": {"id": "Example", "version": "1.0.0"},
    }
    remote.excludes = ["Example [2.0]"]
    fetch = Mock(side_effect=lambda _remote, url: data[url])
    monkeypatch.setattr(pull_through, "fetch_json", fetch)
    assert [p.version_normalized for p in pull_through.packages(distribution, "example")] == [
        "1.0.0"
    ]
    assert fetch.call_count == 3


def test_proxy_registration_rewrites_download_and_dependency_links(monkeypatch):
    package = _stub(
        "1.0.0",
        published=datetime(2020, 1, 2, tzinfo=UTC),
        dependency_groups=[
            {
                "targetFramework": "net8.0",
                "dependencies": [
                    {"id": "New.Dependency", "range": "[1.0.0,)"},
                ],
            }
        ],
    )
    monkeypatch.setattr(v3_api, "_sorted_packages", lambda *_: [package])
    monkeypatch.setattr(v3_api, "base_url", lambda _: BASE)
    result = v3_api.registration_index(SimpleNamespace(), "test.pkg")
    leaf = result["items"][0]["items"][0]
    assert leaf["packageContent"] == BASE + "v3-flatcontainer/test.pkg/1.0.0/test.pkg.1.0.0.nupkg"
    entry = leaf["catalogEntry"]
    assert entry["published"] == "2020-01-02T00:00:00+00:00"
    assert entry["dependencyGroups"][0]["dependencies"][0]["registration"] == (
        BASE + "v3/registrations/new.dependency/index.json"
    )
    assert "upstream.test" not in json.dumps(result)


def test_local_versions_remain_available_during_upstream_outage(monkeypatch):
    class Query(list):
        def filter(self, **kwargs):
            return self

        def values_list(self, *args, **kwargs):
            return [p.version_normalized for p in self]

    package = _stub("1.0.0")
    monkeypatch.setattr(v3_api, "get_packages", lambda _: Query([package]))
    monkeypatch.setattr(pull_through, "versions", Mock(side_effect=web.HTTPBadGateway()))
    monkeypatch.setattr(pull_through, "packages", Mock(side_effect=web.HTTPBadGateway()))
    distribution = SimpleNamespace(remote_id="remote")
    assert v3_api.flatcontainer_versions(distribution, "test.pkg") == {"versions": ["1.0.0"]}
    assert v3_api._sorted_packages(distribution, "test.pkg") == [package]


@pytest.mark.parametrize("entries", [None, "1.0.0", {"version": "1.0.0"}])
def test_versions_rejects_malformed_collections(distribution, monkeypatch, entries):
    monkeypatch.setattr(pull_through, "resource", lambda *_: "https://upstream.test/flat/")
    monkeypatch.setattr(pull_through, "fetch_json", lambda *_: {"versions": entries})
    with pytest.raises(web.HTTPBadGateway):
        pull_through.versions(distribution, "example")


def test_versions_ignores_invalid_entries(distribution, monkeypatch):
    monkeypatch.setattr(pull_through, "resource", lambda *_: "https://upstream.test/flat/")
    monkeypatch.setattr(
        pull_through, "fetch_json", lambda *_: {"versions": [None, 7, {}, "../../unsafe", "1.0.0"]}
    )
    assert pull_through.versions(distribution, "example") == ["1.0.0"]


@pytest.mark.parametrize("resources", [None, "flat", {"@type": "PackageBaseAddress/3.0.0"}])
def test_service_index_rejects_malformed_resource_collections(remote, monkeypatch, resources):
    monkeypatch.setattr(pull_through, "fetch_json", lambda *_: {"resources": resources})
    with pytest.raises(web.HTTPBadGateway):
        pull_through.resource(remote, ("PackageBaseAddress/3.0.0",))


@pytest.mark.parametrize("has_metadata", [True, False])
def test_artifact_preparation_deduplicates_content_and_tracks_repository_without_fetching_bytes(
    distribution, remote, monkeypatch, has_metadata
):
    from contextlib import nullcontext

    from pulp_nuget.app import models

    metadata = _stub("1.0.0", published=datetime(2020, 1, 1, tzinfo=UTC))
    package = SimpleNamespace(relative_path="test.pkg/1.0.0/test.pkg.1.0.0.nupkg")
    deferred_artifact = SimpleNamespace(artifact=None)
    package_manager = SimpleNamespace(get_or_create=Mock(return_value=(package, True)))
    content_artifact_manager = SimpleNamespace(
        get_or_create=Mock(return_value=(deferred_artifact, True))
    )
    remote_artifact_manager = SimpleNamespace(get_or_create=Mock(return_value=(Mock(), True)))

    class Content:
        objects = package_manager
        _meta = SimpleNamespace(
            local_fields=[
                SimpleNamespace(name=name)
                for name in (
                    "content_ptr",
                    "_pulp_domain",
                    "package_id_lower",
                    "version_normalized",
                    "package_id",
                    "version",
                    "published",
                )
            ]
        )

        def __init__(self, **kwargs):
            self.published = None
            self.__dict__.update(kwargs)

    monkeypatch.setattr(models, "NugetPackageContent", Content)
    monkeypatch.setattr(
        pull_through, "ContentArtifact", SimpleNamespace(objects=content_artifact_manager)
    )
    monkeypatch.setattr(
        pull_through, "RemoteArtifact", SimpleNamespace(objects=remote_artifact_manager)
    )
    monkeypatch.setattr(pull_through.transaction, "atomic", nullcontext)
    monkeypatch.setattr(pull_through, "versions", lambda *_: ["1.0.0"])
    monkeypatch.setattr(pull_through, "packages", lambda *_: [metadata] if has_metadata else [])
    monkeypatch.setattr(pull_through, "resource", lambda *_: "https://upstream.test/flat/")
    repository = SimpleNamespace(pull_through_add_content=Mock())
    distribution.repository_id = "repository-1"
    distribution.repository = SimpleNamespace(cast=lambda: repository)
    distribution.pulp_domain = SimpleNamespace(pk="domain-1")

    assert (
        pull_through.artifact(distribution, "Test.Pkg", "1.0.0", "test.pkg.1.0.0.nupkg")
        is deferred_artifact
    )
    package_manager.get_or_create.assert_called_once()
    kwargs = package_manager.get_or_create.call_args.kwargs
    assert kwargs["package_id_lower"] == "test.pkg"
    assert kwargs["version_normalized"] == "1.0.0"
    assert kwargs["_pulp_domain"] is distribution.pulp_domain
    assert kwargs["defaults"]["published"] == (metadata.published if has_metadata else None)
    assert "content_ptr" not in kwargs["defaults"]
    content_artifact_manager.get_or_create.assert_called_once_with(
        content=package, relative_path=package.relative_path
    )
    remote_artifact_manager.get_or_create.assert_called_once_with(
        content_artifact=deferred_artifact,
        remote=remote,
        defaults={"url": "https://upstream.test/flat/" + package.relative_path},
    )
    repository.pull_through_add_content.assert_called_once_with(deferred_artifact)
    assert deferred_artifact.artifact is None
    remote.get_downloader.assert_not_called()

    # Repeated requests use the same natural-key records and repository tracker.
    package_manager.get_or_create.return_value = (package, False)
    content_artifact_manager.get_or_create.return_value = (deferred_artifact, False)
    assert (
        pull_through.artifact(distribution, "test.pkg", "1.0.0", "test.pkg.1.0.0.nupkg")
        is deferred_artifact
    )
    assert package_manager.get_or_create.call_args.kwargs["package_id_lower"] == "test.pkg"
    assert content_artifact_manager.get_or_create.call_count == 2
    remote.get_downloader.assert_not_called()


@pytest.mark.parametrize(
    "package_id, version, filename",
    [
        ("../unsafe", "1.0.0", "unsafe.1.0.0.nupkg"),
        ("test.pkg", "../../unsafe", "test.pkg.1.0.0.nupkg"),
        ("test.pkg", "1.0.0", "other.pkg.1.0.0.nupkg"),
    ],
)
def test_artifact_rejects_invalid_request_before_metadata_or_bytes(
    distribution, remote, monkeypatch, package_id, version, filename
):
    fetch_versions = Mock()
    monkeypatch.setattr(pull_through, "versions", fetch_versions)
    assert pull_through.artifact(distribution, package_id, version, filename) is None
    fetch_versions.assert_not_called()
    remote.get_downloader.assert_not_called()


def test_cold_manifest_uses_upstream_nuspec_without_fetching_package_archive(
    distribution, remote, monkeypatch, tmp_path
):
    path = tmp_path / "example.nuspec"
    body = b"<?xml version='1.0'?><package><metadata><id>Example</id></metadata></package>"
    path.write_bytes(body)
    remote.get_downloader.return_value.run.return_value = SimpleNamespace(path=str(path))
    monkeypatch.setattr(pull_through, "resource", lambda *_: "https://upstream.test/flat/")
    response = pull_through.manifest(distribution, "Example", "01.0.0.0", "example.nuspec")
    assert response.body == body
    assert response.content_type == "application/xml"
    assert not path.exists()
    remote.get_downloader.assert_called_once_with(
        url="https://upstream.test/flat/example/1.0.0/example.nuspec"
    )
    remote.get_downloader.return_value.run.assert_awaited_once()
