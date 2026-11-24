# Public distribution workflow

The private `eisy-onewire-dev` repository is the authoritative development
source. The public `eisy-onewire` repository receives reviewed source snapshots
with independent history. Private commits are never pushed, merged, or grafted
into the public repository.

## Prepare an accepted version

1. Finish development, CI, controlled hardware acceptance, and the private
   release review in `eisy-onewire-dev`.
2. Record the accepted private commit and deterministic plugin archive hash.
3. Create a documentation/release-preparation branch in the private repository.
4. Update public documentation, changelog, license notices, and package
   metadata without changing accepted runtime modules.
5. Run the complete test matrix and build the deterministic archive.
6. Export into a new empty directory:

   ```text
   python scripts/export_public_snapshot.py <empty-output-directory>
   ```

   The exporter uses an explicit allowlist, excludes private engineering
   records and Git metadata, and fails if known personal infrastructure data is
   present.
7. Run the full tests and package build from the exported directory. Compare
   runtime-file hashes with the accepted private commit and record any expected
   documentation/default/license-only archive difference.

## Publish a snapshot

After explicit publication approval, initialize independent history inside the
exported directory. Use the verified GitHub noreply identity associated with
the maintainer account:

```text
git init -b main
git config user.name mike-2545
git config user.email 79805340+mike-2545@users.noreply.github.com
git add --all
git commit -S -m "Release eisy-onewire v0.3.15 public beta"
git remote add origin https://github.com/mike-2545/eisy-onewire.git
git push -u origin main
```

The signing command assumes the maintainer's verified signing key is already
configured. If it is not, stop and establish GitHub-verified signing rather
than creating an unverified identity. For later accepted versions, export a
fresh snapshot, copy its reviewed changes into a clean checkout of the public
repository, review the diff, test, commit with the noreply identity, and push.
Never force-push the private history into the public repository.

## Distribution gates

Only after the public commit and CI are verified:

1. attach or reference the exact reviewed plugin archive and SHA-256;
2. create the GitHub release if the release convention calls for one;
3. publish the matching Plugin Store metadata;
4. verify public documentation and support links; and
5. post the prepared forum announcement.

Each action remains a separate approval and must use the same source snapshot
and artifact identity.
