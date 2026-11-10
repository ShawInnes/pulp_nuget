"""Live-feed pull-through behavior without package selection or repository sync."""

import time

import requests


def _get(url):
    response = requests.get(url, timeout=60)
    response.raise_for_status()
    return response


def _cached_inventory(nuget_bindings, repo_href, expected_count):
    # Content download and repository addition can finish asynchronously.
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        repository = nuget_bindings.RepositoriesNugetApi.read(repo_href)
        if repository.latest_version_href:
            packages = nuget_bindings.ContentPackagesApi.list(
                repository_version=repository.latest_version_href, limit=100
            )
            cached = [package for package in packages.results if package.artifact]
            if len(cached) >= expected_count:
                return cached
        time.sleep(0.25)
    raise AssertionError("Downloaded packages did not appear in the linked repository")


def test_pull_unknown_package_and_dependency_without_sync(
    nuget_bindings,
    nuget_repo,
    remote_factory,
    nuget_distribution_factory,
    distribution_url_factory,
):
    """Exact-ID discovery imports no binaries; package and dependency pulls cache them."""
    remote = remote_factory(includes=[], excludes=[])
    distribution = nuget_distribution_factory(
        repository=nuget_repo.pulp_href, remote=remote.pulp_href
    )
    initial = nuget_bindings.RepositoriesNugetApi.read(nuget_repo.pulp_href)
    assert (
        initial.latest_version_href is None
        or nuget_bindings.ContentPackagesApi.list(
            repository_version=initial.latest_version_href
        ).count
        == 0
    )

    index = _get(distribution_url_factory(distribution, "v3/index.json")).json()
    resources = {item["@type"]: item["@id"] for item in index["resources"]}
    flat = resources["PackageBaseAddress/3.0.0"]
    registrations = resources["RegistrationsBaseUrl/3.6.0"]
    assert distribution.base_path in flat
    assert distribution.base_path in registrations

    versions = _get(flat + "newtonsoft.json.bson/index.json").json()["versions"]
    assert "1.0.3" in versions
    registration = _get(registrations + "newtonsoft.json.bson/1.0.3.json").json()
    entry = registration["catalogEntry"]
    dependencies = [
        dependency
        for group in entry.get("dependencyGroups", [])
        for dependency in group.get("dependencies", [])
    ]
    dependency = next(dep for dep in dependencies if dep["id"].lower() == "newtonsoft.json")
    assert dependency["registration"].startswith(registrations)
    assert registration["packageContent"].startswith(flat)

    # Metadata lookup must not fetch package bytes or enroll metadata-only candidates.
    after_lookup = nuget_bindings.RepositoriesNugetApi.read(nuget_repo.pulp_href)
    if after_lookup.latest_version_href:
        assert (
            nuget_bindings.ContentPackagesApi.list(
                repository_version=after_lookup.latest_version_href
            ).count
            == 0
        )

    package_response = _get(registration["packageContent"])
    assert package_response.content.startswith(b"PK")
    cached = _cached_inventory(nuget_bindings, nuget_repo.pulp_href, 1)
    package = next(p for p in cached if p.package_id.lower() == "newtonsoft.json.bson")
    assert package.size == len(package_response.content)

    # A NuGet client resolves dependencies itself; the cache must accept the next
    # previously unknown ID without adding it to a remote allowlist or syncing it.
    dependency_versions = _get(flat + "newtonsoft.json/index.json").json()["versions"]
    assert "13.0.3" in dependency_versions
    dependency_url = flat + "newtonsoft.json/13.0.3/newtonsoft.json.13.0.3.nupkg"
    dependency_response = _get(dependency_url)
    assert dependency_response.content.startswith(b"PK")
    cached = _cached_inventory(nuget_bindings, nuget_repo.pulp_href, 2)
    assert {p.package_id.lower() for p in cached} == {"newtonsoft.json.bson", "newtonsoft.json"}
    assert _get(registration["packageContent"]).content == package_response.content
