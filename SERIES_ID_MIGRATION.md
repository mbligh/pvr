# Convert PVR series IDs to media directory names

The canonical series_id and calendar slug equal the exact case-sensitive media
folder name, for example Rugby_Womens_WXV, Rugby_Womens_PWR,
Rugby_Mens_Intl, Rugby_Mens_Nations, and Formula_1. Existing F1 f1: episode
IDs also acquire the Formula_1: prefix.

## On slagg

1. Stop all PVR importer/acquisition and calendar publishing cron jobs.
   Check no workers are still running. Do not run old code after migration.
2. Pull both checkouts: cd ~/pvr && git pull --ff-only, then
   cd ~/calendar && git pull --ff-only.
3. In ~/pvr run ./migrate_series_ids (read-only preview).
4. Run ./migrate_series_ids --apply. It creates an owner-readable, mode 0600
   gzip-compressed BSON Extended JSON backup of **all affected source records**
   under ~/pvr-series-id-backups/ before any writes. It changes episode IDs,
   series IDs, release/job/file associations, calendar snapshots and
   stored manifests. No filesystem media is renamed.
5. Run ./migrate_series_ids again to confirm 0 changes. Run ./import_rugby
   to update WXV, then preview calendars with
   cd ~/calendar && ./build_rugby_calendars.
6. Validate representative entries, file paths, and calendar history before
   publishing and restarting cron jobs.

The Nations directory is already renamed by the operator from Rugby_Nations
to Rugby_Mens_Nations. The migration updates recorded directory names;
it cannot silently rename individual media filenames. If existing Nations
recordings still have old basenames, reconcile them before publication.

Existing subscriptions: mbligh/calendar continues to update its historic
lowercase, hyphenated public ICS URLs as aliases, preserving VEVENT UIDs.

Failure recovery: backup files store the original documents as JSON Lines,
with collection and document keys, using BSON Extended JSON. A rerun after
interruption is idempotent if the partially created targets match. A conflicting
target fails closed instead of overwriting data. Retain backups until verified;
a rollback requires an explicit restore, not simply reverting code.

Both repositories must be updated together. Importers block mixed schemas,
rather than generating duplicate records. Atlas was not modified by the code
commit.
