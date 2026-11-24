# Repository naming and transition plan

No repository rename or public repository creation is performed by this plan.

## Proposed names

- Private authoritative development repository: `mike-2545/eisy-onewire-dev`
- Public distribution repository: `mike-2545/eisy-onewire`

Because the existing private repository currently occupies the desired public
name, the private repository must be renamed before the new public repository
can be created. GitHub normally redirects old repository URLs after a rename,
but relying on that redirect would make private/public intent ambiguous once a
new repository reuses the old name.

## Rename impact to review

- Update the private local `origin` URL to `eisy-onewire-dev` immediately after
  the rename; do not rely on redirects.
- Existing private PR and issue numbers remain attached to the renamed private
  repository. Links should be updated to the new private URL and must not be
  presented as public project history.
- Private Actions workflows continue with the repository, but branch
  protections, environment secrets, Pages, webhooks, deploy keys, apps, and
  external status integrations must be inspected after the rename.
- Search scripts and documentation for hard-coded GitHub URLs. Public-facing
  links should point to `eisy-onewire`; private engineering references should
  point to `eisy-onewire-dev` or remain local.
- Review local worktrees, automation, package/store metadata, and any release
  tooling for the old remote URL. Runtime deployment must continue to use only
  accepted archives, not a repository checkout.
- Create the public repository empty—without README, license, or `.gitignore`—
  so the independently initialized snapshot is its first commit.
- Use the verified noreply commit identity and enable the same four-job CI
  matrix before any store or forum publication.

## Safe order

1. Merge the approved private preparation PR.
2. Record private settings, integrations, open PRs/issues, and remote URLs.
3. Rename the private repository to `eisy-onewire-dev`.
4. Update and verify all private local remotes and integrations.
5. Create the empty private-by-default `eisy-onewire` repository.
6. Push the independently initialized, audited snapshot.
7. Verify public CI and content before changing public visibility.
8. Make only the distribution repository public after a final audit.

Stopping between any two steps leaves the accepted private development history
intact and does not require a force-push or history rewrite.
