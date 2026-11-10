# Pulp package supply-chain protections: source assessment and implementation plan

Assessment date: 10 November 2026 (Australia/Brisbane).

## Decision

Proceed with a shared policy plugin and ecosystem adapters. Start with the existing local NuGet plugin and a custom content guard; establish the security boundary with functional tests before extending to npm and Python. Basic minimum-age enforcement does **not** require a pulpcore patch. A first-class independent admission hook and reusable verified-download flow are worthwhile follow-on core changes.

This corrects the earlier provisional assessment: pulpcore already invokes content guards before returning cached responses. Plugin `content_handler()` and repository-finalization hooks alone are insufficient, but a custom guard is an existing pre-cache interception point.

Keep package names, versions, publication metadata and registry protocol semantics in the ecosystem plugins. Keep transport, storage, cache interception and optional staged delivery in pulpcore. Put shared policy configuration, exceptions, source observations and audit records in a new plugin, provisionally named `pulp_supply_chain`; this name is a proposal, not an existing dependency.

## Repositories and evidence baseline

The requested repositories were cloned with full history into sibling directories. No application source, running service, configuration or database was changed during this assessment.

| Repository | Local checkout | Inspected HEAD | Declared development version | Deployed comparison |
| --- | --- | --- | --- | --- |
| pulp_npm | `/Users/shaw/devops/pulp-npm` | `a4fec3139920fea1fe729a310c8c34eeebd9477d` | 0.11.0.dev | 0.10.2, `ba82739c1b1f80ed94d369276a598bb6ec26a614` |
| pulp_python | `/Users/shaw/devops/pulp-python` | `6e6e0d9850b55020a46114067075a417daecdce9` | 3.39.0.dev | 3.38.0, `cd60f9b1422cf5c047712166f9cd834c1b2539d7` |
| pulpcore | `/Users/shaw/devops/pulpcore` | `4134ecd76ebc09338ca9769a4fbc276cd7122261` | 3.124.0.dev | 3.123.2 |
| Existing local pulp_nuget | `/Users/shaw/devops/pulp-nuget` | `2d5e9399fcf4710fc75bd372a91ef5b0294f17da` | Local pull-through implementation | Working checkout including existing local additions |

Source links below use commit-pinned GitHub permalinks corresponding to the inspected checkouts. Python has no application behavior differences from 3.38.0, apart from its version declaration. npm adds name/version fields to generated metadata but no new age/security controls. Core cache and distribution/guard model files are identical to 3.123.2; handler changes concern directory listings and upstream error mapping. An upgrade is not required just to obtain the guard interception point. Both plugin HEADs permit pulpcore >=3.105.0,<3.130; test actual combinations rather than assuming their declared bounds prove compatibility.

This is source-level analysis, not a demonstrated exploit or completed implementation. No new implementation tests or live policy-enforcement tests were run. The acceptance tests below are required work, not claimed results.

## Security boundary and scope

The protected operation is delivery through a policy-bound distribution. It must cover previously cached artifacts and newly discovered artifacts, exact URLs and dependency resolution. All policy checks precede a successful body, redirect or conditional 304 response.

There are three different gates:

1. **Metadata visibility:** present only eligible versions/files to package resolvers.
2. **Download eligibility:** deny direct artifact requests even if a client retained an old URL or metadata response.
3. **Artifact admission:** where configured, fetch privately, verify and scan the complete artifact, then release it.

Cooldowns implement the first two. Strict digest/signature/scanner guarantees also need the third. A valid hash or signature establishes an integrity or identity property; it does not prove package code is benign.

The boundary excludes files already downloaded into client caches, installed packages, privileged administrators exporting artifacts, and previously issued storage URLs until their expiry. Lockfiles may contain external tarball, Git or direct file URLs; CI egress/source restrictions and lockfile review are needed to prevent bypass outside Pulp. Applying policy to one distribution does not protect an unrestricted alias serving the same artifact.

## Pulpcore: existing hooks and required work

### What already works

[`AsyncContentCache` invokes its authorization callback before reading cached responses](https://github.com/pulp/pulpcore/blob/4134ecd76ebc09338ca9769a4fbc276cd7122261/pulpcore/cache/cache.py#L347). [`Handler.auth_cached()`](https://github.com/pulp/pulpcore/blob/4134ecd76ebc09338ca9769a4fbc276cd7122261/pulpcore/content/handler.py#L290) calls the configured content guard on guarded distributions; it caches guard presence, not the guard's allow decision. The uncached path also checks the guard. [`ContentGuard.permit(request)` is explicitly extensible by plugins](https://github.com/pulp/pulpcore/blob/4134ecd76ebc09338ca9769a4fbc276cd7122261/pulpcore/app/models/publication.py#L370).

Attaching a guard to a previously unguarded distribution invalidates its cache through [`Distribution.invalidate_cache()`](https://github.com/pulp/pulpcore/blob/4134ecd76ebc09338ca9769a4fbc276cd7122261/pulpcore/app/models/publication.py#L982). This makes a no-core-patch pilot practical, subject to testing custom plugin routes as well as the generic handler.

**Important composition constraint:** [`CompositeContentGuard` uses OR semantics](https://github.com/pulp/pulpcore/blob/4134ecd76ebc09338ca9769a4fbc276cd7122261/pulpcore/app/models/publication.py#L648). Do not combine an authentication guard and supply-chain guard with it. Implement an explicit AND wrapper that requires both checks. Preserve the stock composite's behavior.

### Limits of a guard-only approach

- The synchronous guard receives a request, without a resolved ContentArtifact, RemoteArtifact or source descriptor. Resolve canonical identity through adapters and persisted mappings; avoid slow upstream fetches on every guard call.
- A guard can deny a whole metadata request but cannot filter a metadata response body. Plugins must do that.
- A lockfile install can request a globally cold artifact URL without first requesting registry metadata. A guard that uses only persisted observations must quarantine this unknown identity; that is safe but can break transparent first-pull behavior. The pilot must choose and test bounded metadata discovery, a preflight/discovery task with an actionable retry flow, or an async pre-cache hook. Do not describe guard-only unknown-identity denial as a transparent drop-in cache. If preserving seamless cold lockfile restores is mandatory and bounded synchronous discovery is unacceptable, make the async core hook an earlier prerequisite.
- Python's Django PyPI endpoints and any custom plugin caches need explicit integration; they are outside the generic aiohttp handler.
- Source selection occurs later. A guard decision must be bound to the actual approved source and digest; validating an initial URL does not constrain an alternate source selected later.
- Custom guard policy changes need explicit cache invalidation. The base guard's own invalidation hook handles deletion, not arbitrary policy-record updates.

### Streaming is not quarantine

Core supports expected size/digests on RemoteArtifact and passes them into downloaders. However, [`_stream_remote_artifact()` writes bytes to the client while downloading](https://github.com/pulp/pulpcore/blob/4134ecd76ebc09338ca9769a4fbc276cd7122261/pulpcore/content/handler.py#L1330). [`BaseDownloader.finalize()` verifies the completed download](https://github.com/pulp/pulpcore/blob/4134ecd76ebc09338ca9769a4fbc276cd7122261/pulpcore/download/base.py#L162). Core closes the connection on a digest mismatch, but bytes may already have reached the client.

Strict mode therefore needs: resolve metadata → check age/source → fetch into private temporary storage → verify expected digest and optional signatures/scanners → recheck current policy → mark artifact admitted → serve. Do not use ordinary streamed policy for this guarantee. Large packages should use bounded disk/object staging and background tasks with request coalescing, not unbounded memory buffering or long-held API workers.

Pull-through repository enrollment occurs after download, so repository-addition rejection cannot prevent the first delivery. [`_stream_content_artifact()` can also choose alternate sources](https://github.com/pulp/pulpcore/blob/4134ecd76ebc09338ca9769a4fbc276cd7122261/pulpcore/content/handler.py#L1047); source rules must cover every selected remote and redirect hop. `REMOTE_CONTENT_FETCH_FAILURE_COOLDOWN` is retry backoff, unrelated to release age.

### Proposed core changes, separate from the pilot

These are new interfaces to design and review, not existing API names:

1. An independent pre-cache admission callback with request and matched distribution context, separate from authentication. It must execute with cache enabled or disabled and must not use the cached no-guard shortcut.
2. A resolved-artifact/source admission callback before serving, redirecting or starting a remote stream. Cover publication, repository, checkpoint, grace-period fallback, ACS, plugin-returned ContentArtifacts and cold pull-through paths. Dynamic plugin responses remain the plugin's responsibility.
3. A reusable optional fetch-and-verify-before-serve path. Keep this a separate change from minimum age.
4. Document cache invalidation and lifecycle contracts for policy attachment changes. Core should not import the shared policy plugin or interpret npm/Python/NuGet versions.

An async-capable contract is preferable for the new hooks, while retaining existing plugin behavior when no policy integration is enabled. Review the interfaces with core maintainers before making them a dependency for the rollout.

## npm adapter

### Current behavior

[`Package` stores name/version/domain only](https://github.com/pulp/pulp_npm/blob/a4fec3139920fea1fe729a310c8c34eeebd9477d/pulp_npm/app/models.py#L34), with uniqueness across that tuple. Original publication time, source identity and expected integrity are absent. Pull-through reconstructs identity from the tarball path after download.

[`NpmDistribution.content_handler()` returns no metadata response for a remote-only distribution](https://github.com/pulp/pulp_npm/blob/a4fec3139920fea1fe729a310c8c34eeebd9477d/pulp_npm/app/models.py#L120), so upstream packuments pass through core. Repository-backed responses synthesize minimal version metadata and omit important upstream dependency metadata. The current nginx string substitution rewrites npmjs URLs but does not implement policy.

[`NpmFirstStage` creates an empty Artifact and ignores dist.integrity/shasum](https://github.com/pulp/pulp_npm/blob/a4fec3139920fea1fe729a310c8c34eeebd9477d/pulp_npm/app/tasks/synchronizing.py#L106). Its simplified dependency traversal should not become the security policy's resolver.

### Changes

- Add a registry adapter that handles full and abbreviated packuments, exact-version metadata, scoped/encoded names, and tarball requests for remote-only and repository-backed distributions.
- Capture project `time[version]`, original version metadata, canonical source URL and expected integrity before approving artifact URLs. Version-specific metadata may require a project packument lookup for publication time.
- Preserve dependencies, peer/optional dependencies, engines, OS/CPU restrictions, deprecation and integrity fields when filtering; do not construct reduced metadata that changes installation semantics.
- Filter versions and all dist-tags consistently. Define a documented eligible fallback for `latest`, or omit it when no target is eligible; do not silently repoint arbitrary `next`/custom tags. Handle an empty eligible set without a server error.
- Parse SRI and populate expected supported strong digests in sync and cold pull-through. A weak-only/missing hash must have an explicit policy result.
- Generate local tarball URLs in the adapter. Replace the nginx npmjs substitution only after real npm/pnpm integration tests show all package traffic remains in the protected distribution.
- Detect same source/name/version with changed expected bytes. Because current Package uniqueness merges sources, keep source observations outside that global identity; do not silently overwrite a package with another upstream's metadata.

Primary files: `app/models.py`, `app/tasks/synchronizing.py`, `app/serializers.py`, distribution viewsets and new adapter/service modules. API fields and endpoint names should follow the common schema below rather than introduce incompatible per-plugin policy knobs.

Existing [`test_pull_through_caching.py`](https://github.com/pulp/pulp_npm/blob/a4fec3139920fea1fe729a310c8c34eeebd9477d/pulp_npm/tests/functional/api/test_pull_through_caching.py#L1) manually constructs local tarball URLs; it is insufficient evidence for complete real-client install behavior.

## Python adapter

### Current behavior

[`SimpleView.pull_through_package_simple()` preserves upstream upload times in transient responses](https://github.com/pulp/pulp_python/blob/6e6e0d9850b55020a46114067075a417daecdce9/pulp_python/app/pypi/views.py#L368). Stored content has no original upload timestamp: [`local Simple responses use repository-addition time`](https://github.com/pulp/pulp_python/blob/6e6e0d9850b55020a46114067075a417daecdce9/pulp_python/app/pypi/views.py#L428), as does [`local JSON metadata`](https://github.com/pulp/pulp_python/blob/6e6e0d9850b55020a46114067075a417daecdce9/pulp_python/app/utils.py#L568). This is existing tested behavior, not merely a missing JSON field.

[`PythonRemote.get_remote_artifact_url()` accepts the request's redirect query](https://github.com/pulp/pulp_python/blob/6e6e0d9850b55020a46114067075a417daecdce9/pulp_python/app/models.py#L359). A policy must validate that it matches a trusted observation, not treat it as authoritative source identity. Core's cold fallback constructs a bare RemoteArtifact; advertised metadata hashes alone do not establish an equivalent server-side expected-digest binding.

[`PythonApiCache`](https://github.com/pulp/pulp_python/blob/6e6e0d9850b55020a46114067075a417daecdce9/pulp_python/app/cache.py#L8) keys Simple responses by path, method and media type. Simple responses have public 600-second caching and repository-version-based validators. Static [`publication generation`](https://github.com/pulp/pulp_python/blob/6e6e0d9850b55020a46114067075a417daecdce9/pulp_python/app/tasks/publish.py#L43) omits upload times and can be reached through API redirects.

### Changes

- Persist original **per-file** upload times and expected hashes during sync and pull-through discovery. A wheel uploaded today under a year-old release must still be new. Keep repository-addition timestamps as a separate concept.
- Backfill from configured trusted upstream metadata. Unknown timestamps remain unknown; do not infer them from database creation times.
- Use one eligibility filter for local and upstream entries before rendering Simple HTML/JSON and local JSON API responses.
- For policy-bound distributions, serve dynamic policy-aware metadata even when a static publication exists. Handle direct content URLs for published Simple pages too; changing only Django redirects leaves another route. Preserve normal publication behavior for unbound distributions.
- Validate filename, source URL, expected digest, PEP 658 `.metadata` pairing and allowed redirect destinations before downloading. Reject caller-supplied substitutions.
- Make response caches and ETag/304 behavior sensitive to policy revisions and time-based eligibility transitions.

### Reuse existing features carefully

- [`allow_package_substitution`](https://github.com/pulp/pulp_python/blob/6e6e0d9850b55020a46114067075a417daecdce9/pulp_python/app/models.py#L402) defaults to true. Setting it false adds repository-level protection against conflicting filenames/digests, but does not establish the whole serving boundary.
- [`Repository blocklists reject new additions`](https://github.com/pulp/pulp_python/blob/6e6e0d9850b55020a46114067075a417daecdce9/pulp_python/app/models.py#L485); a new entry does not revoke an already served artifact.
- Sync sets expected SHA256; cold pull-through needs equivalent treatment.
- [`Provenance sync`](https://github.com/pulp/pulp_python/blob/6e6e0d9850b55020a46114067075a417daecdce9/pulp_python/app/tasks/sync.py#L281) fetches/parses provenance, while upload paths perform verification. Fetching provenance is not a universal publisher-verification policy.
- Vulnerability scans run after repository creation. They provide findings, not automatic quarantine before first delivery.

Primary files: `app/models.py`, `app/tasks/sync.py`, `app/pypi/views.py`, `app/utils.py`, `app/cache.py`, `app/tasks/publish.py`, serializers/viewsets, and new adapter modules.

## NuGet pilot

The existing local plugin is the smallest starting point because [`NugetPackageContent.published` already exists](https://github.com/ShawInnes/pulp_nuget/blob/2d5e9399fcf4710fc75bd372a91ef5b0294f17da/pulp_nuget/app/models.py#L85), and registration discovery captures publication dates. Flat-container version lists contain no dates; registrations or stored observations must supply them. The 1900 unlisted sentinel is not a real publication time.

Apply the shared decision to local and upstream registration entries, flat-container lists, exact package downloads, nuspecs and symbol packages where applicable. Current [`local-artifact returns`](https://github.com/ShawInnes/pulp_nuget/blob/2d5e9399fcf4710fc75bd372a91ef5b0294f17da/pulp_nuget/app/v3_api.py#L164) and [`custom content caching`](https://github.com/ShawInnes/pulp_nuget/blob/2d5e9399fcf4710fc75bd372a91ef5b0294f17da/pulp_nuget/app/content.py#L68) require explicit warm-cache tests. Replace the permissive missing-registration path with a configured quarantine/unknown-date outcome. Bind expected package hashes and later signature verification before claiming strict integrity.

The guard pilot is successful only if these custom paths and already cached packages obey changes immediately on the next Pulp request. Do not infer that from generic core cache tests alone.

## Shared policy model and contract

Use a domain-aware plugin with RBAC and explicit API permissions. Bind policies to core Distribution records inside the new plugin; this avoids a core foreign key to an optional package. Require enabled adapters to be installed and compatible; invalid configuration must fail validation rather than silently disable enforcement.

A binding row alone does not enforce a guard. Existing distribution APIs can detach/replace `content_guard`, and core's reference uses `on_delete=SET_NULL`. Protect deletion/replacement for bound distributions, or require an explicit audited policy-disable operation. Adapter serializers/viewsets and shared-plugin lifecycle checks must enforce this invariant; ordinary distribution-edit rights must not automatically grant policy-disable rights. Detect drift at startup and during configuration reconciliation. Direct database administration remains outside this application-level boundary.

| Record | Essential data |
| --- | --- |
| Policy | Mode (observe/enforce), minimum age, unknown-date handling, integrity requirements, source rules, revision |
| Distribution binding | Distribution, policy, adapter, optional delegated authentication guard |
| Source observation | Domain, configured upstream identity, normalized package/version, filename, expected digest, original upload/publication time, first/last seen, canonical download URL, metadata evidence reference |
| Artifact admission | Observation and actual digest, verification/scanner results, scanner/database versions, admitted/quarantined state |
| Block rule | Ecosystem/source/name/version/digest scope, reason, provenance of rule, active state |
| Approval | Exact source/package/version/digest where known, allowed policy exception, reason, actor, expiry/revocation |
| Decision/audit event | Distribution, policy revision, canonical identity, result/reason, actor/request correlation, timestamp |

Keep source observations separate from globally deduplicated Content/Artifact rows. Approval for one source or distribution must not authorize another. Store expected and observed digests separately. Changes to expected digest or source metadata must not silently move the record that an earlier approval covered. Use database uniqueness and locking for concurrent discovery/fetches.

The common evaluator accepts canonical descriptors supplied by the adapter and returns eligibility, reason codes, eligible-at/next-transition time and required admission work. The evaluator does not parse registry-specific metadata. Build a pure deterministic evaluator plus persistence/services; avoid making every request query every rule individually.

### Initial policy recommendation

- Public npm/Python/NuGet: 7-day publication-age window as a starting operating policy, adjustable per distribution.
- Unknown, malformed or future timestamps: quarantine by default. Optional first-seen aging is an explicit fallback; preserve its start across restarts/metadata refreshes.
- A publication timestamp is a registry assertion and can be backdated by a compromised source. Optional minimum first-seen age is stronger but delays even genuinely old packages on their first encounter. Keep these as separate settings and report which condition blocked a package.
- Internal packages: distinct trusted sources and explicit namespace/source routing. Avoid a blanket public-scope exception that bypasses both age and source checks.
- Known-malicious blocks remain effective after the age window passes. Security-fix overrides are exact, audited and expiring; they cannot override unrelated hard-deny rules unless explicitly authorized for that rule.
- No broad emergency global disable switch in the normal workflow. Provide a dry-run report and explicit scoped approvals.

Use UTC-aware timestamps and a controlled clock. Evaluate age on requests; automatic maturation need not require one scheduled task per release. Metadata caches must expire by the next eligibility transition.

### Cache and availability contract

Eligibility cache keys include domain, distribution, policy revision, source, canonical identity and digest. Response-cache keys and ETags also incorporate policy/time transitions or are invalidated accordingly. Core currently keys responses by path/method/host, not query string; do not use unvalidated query strings to select approved sources. npm content negotiation and Python's separate API caches need their own correct representation keys.

Keep admission state separate from age eligibility. A cached package is not automatically admitted, and an old publication date is not an approval. Metadata lookups may create observations without downloading package bytes; unknown direct lockfile URLs need the explicit discovery behavior described above.

Policy, block, approval and source changes invalidate affected responses and eligibility records on transaction commit. Guard changes must clear cached guard-presence state. An expired approval cannot remain valid because a positive decision was cached. Recheck policy after a long staged download before releasing it. Define in-flight request semantics explicitly; do not promise retroactive cancellation of bytes already released.

Under upstream outage, previously observed immutable eligible artifacts may remain available according to configured metadata freshness; new/unverifiable identities remain quarantined. Distinguish upstream failure from package absence or policy denial. No network lookup should be necessary for every already admitted warm download. Bound discovery concurrency, metadata size, fetch duration, temporary storage and request/audit volume.

Object-storage buckets must remain private. [`Core can issue signed storage redirects`](https://github.com/pulp/pulpcore/blob/4134ecd76ebc09338ca9769a4fbc276cd7122261/pulpcore/content/handler.py#L1218); already issued URLs cannot be revoked through a later Pulp guard decision. Use short expiries or disable redirects where immediate revocation is required. Audit the separate [`ArtifactDistribution`](https://github.com/pulp/pulpcore/blob/4134ecd76ebc09338ca9769a4fbc276cd7122261/pulpcore/app/models/publication.py#L1008), alternate distributions, checkpoints, grace-period publications and privileged artifact APIs. Policy scope must be explicit in administrative tooling.

## Additional protections and ownership

| Control | Owner | Delivery approach |
| --- | --- | --- |
| Emergency malicious-package blocks | Shared plugin + adapters | Manual rules first; enforce at metadata and binary delivery, including warm content |
| Dependency confusion | Adapters + client/CI configuration | Canonical internal namespaces, source mapping, prohibited public fallback; a single proxy URL alone is insufficient |
| Artifact substitution | Adapters + shared observations | Expected hash binding, changed-digest quarantine, preserve source evidence |
| Signature/provenance policy | Adapter verification + shared admission | Verify signatures and expected publisher identity; handle absent attestations explicitly; introduce selectively |
| Vulnerability policy | Shared plugin/tasks | Advisory ingestion, periodic rescan, severity/exception policy; block admitted digests when findings change |
| Source and redirect restrictions | Downloader/adapter + deployment egress | Validate all redirect/ACS destinations, schemes and approved hosts; account for legitimate CDNs/private registries and DNS changes |
| Install/build script exposure | Client/CI | Reviewed script execution, isolated builds, restricted credentials/egress; a proxy does not stop runtime code execution |
| Image/build supply chain | pulp-oci-images/CI | Pin remaining mutable image tags, review updates, build attestations/SBOM and scanner checks separately |
| Policy management UI | pulp-ui | Policy bindings, quarantine explanations, approvals, audit events; API-first delivery keeps UI off the critical path |

Do not delay Ubuntu security updates under the same blanket rule. Its current verbatim publications retain Ubuntu signatures. Per-package filtering requires regenerated metadata and a local signing/trust arrangement; staging whole signed snapshots is a separate project with freshness/Valid-Until considerations. Preserve the existing Ubuntu behavior during the application-package rollout.

## Implementation sequence and acceptance gates

Effort estimates are engineering days for an experienced contributor, including functional tests and documentation. They are planning estimates, not measured results or upstream-review commitments.

| Phase | Work and repository ownership | Estimate | Exit condition |
| --- | --- | --- | --- |
| 0: boundary spike | Shared plugin skeleton + local NuGet; core test fixtures | 3–5 days | Guard denies a newly blocked warm artifact with Redis on/off; authentication AND policy both required; metadata filters agree; cold lockfile discovery behavior chosen and demonstrated |
| 1: common policy | Shared plugin models, APIs, RBAC, canonical observations, manual blocks, approvals, audit, revisions and AND guard | 5–8 days | Deterministic evaluation, domain isolation, revocation/expiry and concurrent discovery covered |
| 2: NuGet complete | pulp-nuget adapter, all registration/artifact paths, timestamp/digest binding and migration/backfill tooling | 4–7 days | Real dotnet restore, transitive dependencies, offline warm restore and bypass matrix pass |
| 3: npm | pulp-npm complete metadata handling, integrity, adapter and client tests | 8–12 days | Real npm/pnpm installs stay in Pulp; tags, scoped packages, transitive dependencies and policy changes pass |
| 4: Python | pulp-python original timestamps, API/publication paths, redirect binding and validators | 8–12 days | pip/uv client tests pass across HTML/JSON, late file uploads, static publication aliases and cache transitions |
| 5: rollout | pulp-oci-images packaging, observe/enforce migration, operations and recovery docs; optional minimal UI | 3–5 days | Existing inventories reviewed, unknown metadata handled explicitly, controlled rollout and rollback demonstrated |

Total age/denylist rollout: approximately **31–49 engineering days**, or **6–10 working weeks for one contributor**. npm and Python can overlap once the contract is stable; with multiple contributors the elapsed time can be lower, but integration/testing remains necessary. This is a firmer and broader estimate than the earlier rough 3–6 weeks because it includes protocol completeness, backfill, caches and operational exceptions.

Separate follow-on estimates: first-class core hooks and reusable strict staged delivery **8–15 days**; selective signature/provenance enforcement and advisory-driven quarantine **10–20 days**, depending on ecosystem coverage and scanner choice. Guard-only age enforcement can ship before these. A private approved-repository promotion workflow can provide strict admission earlier if transparent first-request behavior is not required.

Recommended change sets:

1. Add proof-of-boundary fixtures/tests and a small shared-plugin NuGet prototype. Decide whether to retain the guard architecture for production based on measured behavior and latency.
2. Land shared schema/evaluator/API and NuGet integration as separate reviewable changes. Roll out observe mode first.
3. Land npm and Python adapters independently against the same versioned contract. Keep unbound distribution behavior compatible.
4. Package exact compatible versions in the shared Pulp image; API/content/worker and jobs must have identical plugin sets. Apply additive migrations and backfill in batches.
5. Enable enforcement per distribution after reviewing inventory and approvals. Test tighten/revoke and rollback without deleting volumes or resetting repositories.
6. Propose neutral core admission interfaces separately; retain the proven guard path until those hooks are released and validated. Do not make upstream acceptance a hidden prerequisite for the pilot.
7. Add strict staging and verified provenance/scanning only with explicit no-bytes-before-admission tests.

Rollback should disable or detach a policy only through authorized, audited operations; retain observations and decisions for diagnosis. A deployment rollback must not leave a configured policy silently unenforced because an older image lacks its adapter. Validate compatibility at startup and before rollout.

## Required verification matrix

Use local deterministic upstream fixtures with controlled publication timestamps and a controllable clock. No dependence on freshly published real packages or upstream deletions is needed.

- Core: cache enabled/disabled, response MISS/HIT, cold/warm stored artifact, direct URL, GET/HEAD/Range/304, filesystem and object storage, publication/checkpoint/grace-period paths, ACS fallback, source redirects, artifact aliases and policy update races.
- Common policy: exact boundary time; absent/malformed/future/backdated timestamps; first-seen persistence; normalized aliases; two sources sharing name/version/digest; different policies sharing the same Artifact; domain isolation; rule precedence; unknown metadata during outage; approval expiry/revocation; policy revision changes after a warm response.
- npm: full and abbreviated metadata, scoped encoded paths, exact-version metadata, empty eligible set, latest/custom tags, dependencies/peer/optional dependencies, tarball origin rewrites, changed SRI/digest and real npm/pnpm lockfile installs.
- Python: per-file later uploads, upstream/local Simple HTML and JSON, JSON API, direct published metadata, filename normalization, forged redirect and metadata pairing, `.metadata`, ETag/Last-Modified/304, pip/uv, static publications and remote-only distributions.
- NuGet: flat-container versions, registration pages/leaves, exact .nupkg, nuspec, symbol packages, unlisted sentinel, missing registration, local/upstream merges, custom handler cache, fresh and repeated real dotnet restores.
- Strict admission: corrupt expected digest, missing/invalid signature, scanner rejection, stale scanner findings, concurrent fetch deduplication, large artifacts, interrupted downloads, temporary-file cleanup and **zero package body bytes or storage redirects before successful admission**.
- Deployment: bind policy to a prepopulated distribution, batch backfill, policy-off compatibility, package-manager errors with actionable audit reasons, protected health/API behavior, restart persistence and rollback.

Extend the existing core `test_content_cache.py`, `test_contentguard.py`, `test_content_delivery.py`, `test_cdn_cache_validation.py`, artifact-distribution tests and unit handler/cache tests. Extend npm pull-through/download-policy/dist-tag tests, Python `test_pypi_apis.py`, `test_simple_cache.py`, `test_blocklist.py` and sync tests, and the local NuGet pull-through suites.

Core and Python AGENTS.md require tests to run through `oci-env`; regenerate client bindings when API schemas change. Run the matrix against deployed tags and the chosen target versions. Initial proof-of-boundary tests are a release gate: if a custom route avoids the guard, fix that route or require the independent core hook before enabling its policy.

## Recommended next action

Implement phase 0 in an isolated development environment, preserving the existing package-cache deployment. Prove pre-cache revocation, AND composition, NuGet metadata consistency and unknown-identity denial. Then finalize the shared API contract and implement the adapters. No production/development deployment modification is needed to finish reviewing this plan.
