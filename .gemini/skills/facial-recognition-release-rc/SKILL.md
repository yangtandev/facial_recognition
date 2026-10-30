---
name: facial-recognition-release-rc
description: Apply this facial_recognition repository's SemVer release-candidate policy when preparing a commit, version tag, release notes, RC release, or promotion to a production release. Applies only to this repository; ordinary commits do not require a release.
---

# Facial Recognition RC Releases

## Scope

Use only for this facial_recognition repository. Keep user-level Codex and
Gemini skills unchanged. When a general Conventional Commit/SemVer skill also
applies, use its commit-message conventions and this skill's release lifecycle
and version selection instead of its stable-only version assumptions.

Preparing a commit does not automatically publish a release. Follow the user's
Git authorization separately for staging, committing, tagging, and pushing.

## Version selection

- Git tags use `vMAJOR.MINOR.PATCH` or `vMAJOR.MINOR.PATCH-rc.N`.
- Displayed application versions may omit the leading `v`.
- Identify the latest stable SemVer tag on the relevant release lineage.
  Exclude every prerelease from the stable baseline; a wildcard tag listing
  such as `v[0-9]*.[0-9]*.[0-9]*` also matches RC tags and is not sufficient.
  Compare SemVer components numerically, not as strings.
- Choose a new target from all release changes since that stable baseline:
  compatible fixes/performance changes -> patch; compatible features -> minor;
  breaking changes -> major. Documentation-only commits need no release unless
  the user explicitly requests one.
- First test release for that target: `vX.Y.Z-rc.1`.
- Fixes during that RC cycle increment only `N`, for example
  `v1.0.1-rc.1` -> `v1.0.1-rc.2`, rather than starting `v1.0.2-rc.1`.
  If release scope changes the required major/minor target, recompute the target
  from the stable baseline and start the new target's RC cycle.
- Derive the next RC number from existing tags for that exact target; never
  reuse a published version or move its tag to different content.
- Promote the tested candidate to the same target without a prerelease suffix:
  `v1.0.1-rc.2` -> `v1.0.1`, not `v1.0.2`.
- After `v1.0.1` is released, a new compatible bug-fix cycle starts at
  `v1.0.2-rc.1`.
- If no stable baseline or active release target can be established, report
  that missing context rather than inventing an existing version.

## Release notes and promotion

For a release-bearing commit, put the exact chosen tag in the newest
`RELEASE_NOTES.md` entry: `## vX.Y.Z-rc.N - YYYY-MM-DD` for RCs, or
`## vX.Y.Z - YYYY-MM-DD` for production. Use Traditional Chinese change notes.
Include the notes in the release commit before tagging that final commit.

Promote only after the user confirms the candidate passed testing. Preserve
the tested executable source, models, and dependency versions. A metadata-only
promotion may change version labels/release notes; verify the final package
and record its identity. Executable, model, or dependency changes require a new
candidate and testing. Version naming alone does not constitute deployment
approval. Verify the final tag, source commit, and release-note entry agree.

## Git tag publication gate

Hosts using the tag-only updater discover Git tags, not GitHub Releases.
Existing Release-based agents require one-time tool maintenance before migration.

- A deployable tag must contain the versioned `ota/deploy.json` contract: explicit
  projects, Python/CPU, schemas, configuration defaults, requirements identity,
  complete locked wheels and model sizes/SHA256. Historical tags without it are
  not deployment candidates. Never promote an intentional failure candidate.
- Pushing an approved stable tag is the manual production approval step, after
  candidate testing. Do not push stable tags before production authorization.
  A branch commit or GitHub Release publication alone is not the new gate.
- Parse RC/stable eligibility from SemVer suffixes. Release flags, drafts and
  assets do not participate. Annotated/lightweight tags resolve to fixed commits;
  persist observed identities and reject moved tags. Never reuse a pushed tag.
- Select by numeric SemVer, role, project and contract, not API order. Test hosts
  consider RC/stable tags; production only stable tags. No ordinary downgrades
  or infinite automatic retries of failed versions.
- Verify models and full environments before reuse; changed dependencies get a
  separate environment and verified cached/missing wheels. Never modify the
  running or rollback environment with pip.
- Preserve host-local audit, health checks, durable failure-email queues,
  checkpoint recovery and rollback. Camera connectivity alone must not fail
  update validation or trigger rollback.

## Deployment eligibility

- Development host: develops and publishes; does not follow field OTA.
- Test host: receives assigned RCs and the approved stable version when its
  tested target is promoted. Do not downgrade an active newer RC cycle merely
  because an older stable release is published.
- Production hosts: receive approved stable releases for their registered
  project. Each host updates independently; one host's failure does not block
  unrelated hosts.
- The independent updater applies its enrolled role and any project-targeting
  policy to Git tags carrying the deployment contract. Do not implement production eligibility
  as an unrestricted search for the highest Git tag.
- A failed update may restore an older known-good release without violating
  release immutability; rollback does not create or rewrite a release tag.
