"""Live NuGet metadata must bypass stale response caches without bypassing guards."""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import web

from pulp_nuget.app import content


@pytest.fixture(autouse=True)
def metadata_settings(monkeypatch):
    # Replace the lazy Dynaconf object rather than deleting dynamic settings on undo.
    monkeypatch.setattr(
        content,
        "settings",
        SimpleNamespace(
            DOMAIN_ENABLED=False,
            CONTENT_PATH_PREFIX="/pulp/content/",
        ),
    )


@pytest.fixture
def metadata_distribution(monkeypatch):
    distribution = SimpleNamespace(content_guard=None)
    manager = Mock()
    manager.select_related.return_value = manager
    manager.get.return_value = distribution
    monkeypatch.setattr(content.NugetDistribution, "objects", manager)
    monkeypatch.setattr(content.settings, "DOMAIN_ENABLED", False)
    return distribution, manager


def test_metadata_response_checks_guard_before_building_uncached_response(
    metadata_distribution, monkeypatch
):
    distribution, manager = metadata_distribution
    request = SimpleNamespace(path="/pulp/content/feed/v3/index.json")
    order = []
    permit = Mock(side_effect=lambda *_: order.append("permit"))
    response = web.json_response({"versions": ["1.0.0", "2.0.0"]})
    handle = Mock(side_effect=lambda *_: (order.append("build"), response)[1])
    monkeypatch.setattr(content.Handler, "_permit", permit)
    monkeypatch.setattr(content.v3_api, "handle", handle)

    assert content._metadata_response("feed", "v3/index.json", request) is response
    assert order == ["permit", "build"]
    manager.select_related.assert_called_once_with("content_guard")
    manager.get.assert_called_once_with(base_path="feed")
    permit.assert_called_once_with(request, distribution)
    handle.assert_called_once_with(distribution, "v3/index.json")
    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.parametrize("denied", ["anonymous", "forbidden"])
def test_metadata_guard_denial_never_builds_or_serves_metadata(
    metadata_distribution, monkeypatch, denied
):
    distribution, _manager = metadata_distribution
    request = SimpleNamespace(path="/pulp/content/private/v3/index.json")
    guard = Mock(name="private guard")
    guard.name = "private guard"
    guard.cast.return_value = guard
    guard.permit.side_effect = (
        web.HTTPUnauthorized(headers={"WWW-Authenticate": 'Basic realm="nuget"'})
        if denied == "anonymous"
        else PermissionError("Not permitted")
    )
    distribution.content_guard = guard
    handle = Mock()
    monkeypatch.setattr(content.v3_api, "handle", handle)

    expected = web.HTTPUnauthorized if denied == "anonymous" else web.HTTPForbidden
    with pytest.raises(expected) as exc:
        content._metadata_response("private", "v3/index.json", request)
    if denied == "anonymous":
        assert exc.value.headers["WWW-Authenticate"] == 'Basic realm="nuget"'
    handle.assert_not_called()
    guard.permit.assert_called_once_with(request)


def test_metadata_response_scopes_nested_distribution_to_domain(metadata_distribution, monkeypatch):
    distribution, manager = metadata_distribution
    monkeypatch.setattr(content.settings, "DOMAIN_ENABLED", True)
    monkeypatch.setattr(content.Handler, "_permit", Mock())
    monkeypatch.setattr(content.v3_api, "handle", Mock(return_value=web.json_response({})))
    content._metadata_response(
        "tenant-a/feed/nested", "v3-flatcontainer/example/index.json", SimpleNamespace()
    )
    manager.get.assert_called_once_with(pulp_domain__name="tenant-a", base_path="feed/nested")
    content.v3_api.handle.assert_called_once_with(
        distribution, "v3-flatcontainer/example/index.json"
    )


def test_unknown_nuget_distribution_leaves_request_for_regular_handler(
    metadata_distribution, monkeypatch
):
    _distribution, manager = metadata_distribution
    manager.get.side_effect = content.NugetDistribution.DoesNotExist
    permit = Mock()
    monkeypatch.setattr(content.Handler, "_permit", permit)
    assert content._metadata_response("another-plugin", "v3/index.json", SimpleNamespace()) is None
    permit.assert_not_called()


def test_missing_package_metadata_returns_not_found_after_guard(metadata_distribution, monkeypatch):
    permit = Mock()
    monkeypatch.setattr(content.Handler, "_permit", permit)
    monkeypatch.setattr(content.v3_api, "handle", Mock(return_value=None))
    with pytest.raises(web.HTTPNotFound):
        content._metadata_response("feed", "v3-flatcontainer/unknown/index.json", SimpleNamespace())
    permit.assert_called_once()


@pytest.mark.parametrize(
    "resource",
    [
        "v3/index.json",
        "v3-flatcontainer/example/index.json",
        "v3/registrations/example/index.json",
        "v3/registrations/example/1.0.0.json",
        "v3/registrations/example/page/1.0.0/2.0.0.json",
    ],
)
@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_live_metadata_bypasses_outer_handler_for_all_metadata_resources(
    monkeypatch, resource, method
):
    monkeypatch.setattr(content.settings, "CONTENT_PATH_PREFIX", "/pulp/content/")
    request = SimpleNamespace(method=method, path=f"/pulp/content/feed/nested/{resource}")
    response = web.json_response({"fresh": True})
    builder = Mock(return_value=response)
    fallback = AsyncMock()
    monkeypatch.setattr(content, "_metadata_response", builder)
    assert asyncio.run(content.live_metadata(request, fallback)) is response
    builder.assert_called_once_with("feed/nested", resource, request)
    fallback.assert_not_awaited()


@pytest.mark.parametrize(
    "method, path",
    [
        ("GET", "/pulp/content/feed/v3-flatcontainer/example/1.0.0/example.1.0.0.nupkg"),
        ("GET", "/pulp/content/feed/v3-flatcontainer/example/1.0.0/example.nuspec"),
        ("GET", "/pulp/content/feed/v3/search"),
        ("GET", "/pulp/api/v3/status/"),
        ("GET", "/pulp/content/file/something.json"),
        ("POST", "/pulp/content/feed/v3/index.json"),
    ],
)
def test_live_metadata_leaves_unrelated_routes_and_methods_unchanged(monkeypatch, method, path):
    monkeypatch.setattr(content.settings, "CONTENT_PATH_PREFIX", "/pulp/content/")
    request = SimpleNamespace(method=method, path=path)
    response = web.Response(text="regular handler")
    builder = Mock()
    fallback = AsyncMock(return_value=response)
    monkeypatch.setattr(content, "_metadata_response", builder)
    assert asyncio.run(content.live_metadata(request, fallback)) is response
    builder.assert_not_called()
    fallback.assert_awaited_once_with(request)


def test_live_metadata_preserves_other_plugins_at_similar_paths(monkeypatch):
    monkeypatch.setattr(content.settings, "CONTENT_PATH_PREFIX", "/pulp/content/")
    request = SimpleNamespace(method="GET", path="/pulp/content/other-feed/v3/index.json")
    builder = Mock(return_value=None)
    response = web.Response(text="another plugin")
    fallback = AsyncMock(return_value=response)
    monkeypatch.setattr(content, "_metadata_response", builder)
    assert asyncio.run(content.live_metadata(request, fallback)) is response
    builder.assert_called_once_with("other-feed", "v3/index.json", request)
    fallback.assert_awaited_once_with(request)
