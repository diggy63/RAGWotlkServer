# Installing the three server modules

The live server and its databases stay on your friend's computer. Git shares
configuration, scripts, and source revisions; his computer builds the images.
No developer database or player data is copied to his server. Here, "migration"
means the modules' required SQL updates to his existing databases, after a local
backup. Configuration-only changes do not require compilation.

The installer builds the existing Playerbots core with these additional modules:

- [Individual Progression](https://github.com/ZhengPeiRu21/mod-individual-progression)
- [NPC Enchanter](https://github.com/azerothcore/mod-npc-enchanter)
- [AH Bot Plus](https://github.com/NathanHandley/mod-ah-bot-plus)

Their upstream authors retain credit and their respective licenses. Source code
is fetched from the exact revisions in `modules.lock.json`, including the core
and Playerbots versions from the original deployment. Both computers therefore
use the same revisions. Changing a pin produces a new build directory and image
tag; old sources and images are retained for recovery. The Dockerfile comes from
the pinned core, with compiler parallelism limited to one job.

## Before the first installation

Run this on the machine hosting the game, inside Ubuntu WSL. Install Git and
Python 3.11 or later if missing; Ubuntu 24.04 provides a suitable Python version:

```sh
sudo apt update
sudo apt install git python3
```

Docker Engine, Buildx, and the Compose plugin must already work for your Linux user.
Keep the deployment and build files inside the WSL Linux filesystem. Allow disk
space for a full build, the database dump, saved images, and a compressed copy
of the client data; backup size and build time depend on your current server.
Keep WSL and the Windows host running until the command finishes.

Builds run in a dedicated Docker-container Buildx builder limited to one CPU,
3 GiB RAM, and 4 GiB total RAM plus swap (swap availability depends on WSL).
The installer checks Docker's resource-limit support and the builder's actual
limits. It also requires at least 4 GiB available inside WSL before starting.
It refuses to fall back to an unrestricted build. The builder stops afterward,
but its cache stays available for future runs. These limits affect compilation,
not the running game server. They make compilation slower and cannot account
for unrelated Windows applications using up host memory.

If a previous unrestricted installation build is making the host unresponsive,
press Ctrl+C in that terminal. If necessary, open another WSL terminal and run:

```sh
pkill -INT -f '^docker build --file .*\.module-build/'
```

Wait for it to stop before pulling and running the fixed script. If WSL has
less than 4 GiB available, schedule a maintenance window and stop the game
services yourself to free memory, leaving the database running. Do not increase
compiler jobs or remove the memory limit to get past a failed build.

Create a dedicated character on a normal player account for auction ownership;
an existing player account is fine, so a new account is not required.
For example, create a character named `Auctioneer` through the normal client,
then log it out. Do not use a roaming bot, an addclass bot, or a character you
intend to play. It needs no GM privileges. The installer looks up its GUID and
rejects generated bot accounts and online auction characters.

Check for other auction-house modules before adding more modules to the lock
file. This build includes only AH Bot Plus as the auction-house module.

## Pull and install

From your checkout of this repository:

```sh
git pull --ff-only
bash install_modules.sh \
  --server-dir "$HOME/wow-migration-20261027T194548Z" \
  --ah-character Auctioneer
```

Replace `Auctioneer` with the character you actually created. `--server-dir`
must be the existing running deployment directory with its `.env`, `config/`,
and `compose.yaml`. It can be this repository's directory if you already run
the game from here. The installer checks the existing world's config mount to
avoid modifying a different checkout by mistake. Do not copy an unrelated
`.env` or create empty Docker volumes just to bypass those checks.

The command:

1. Checks the existing deployment and auction character.
2. Fetches pinned sources into `.module-build/` and builds both server images.
   The current server remains online during the build. A build failure does not
   stop the running server or change its database.
3. Stops worldserver and authserver, allowing up to 120 seconds for clean shutdown.
4. Saves a private backup under the live deployment's `backups/before-modules-*`:
   all four databases, config, `.env`, Compose files, exact running images,
   client data, and the mounted module/core SQL directories. Dump completion,
   compressed archives, and SHA-256 checksums are checked before migration.
5. Installs the three module configs from this repository after backing up the
   previous copies. Git is the source of truth for those three files; existing
   Playerbots and worldserver configuration files are left intact. The three
   starting-progression settings are set to Wrath stage 13 as requested.
6. Writes a local `compose.override.yaml` selecting the built images and their
   matching SQL sources. It enables Player Settings, allows module item stats,
   and supplies the auction character GUID and seller enable flag. An unrelated
   existing override is rejected rather than overwritten.
7. Runs the existing `migrate` service with database updates enabled and bots and
   auction selling/buying disabled. Only the compiled modules' standard SQL is
   applied; no `optional/` SQL or client patches are installed.
8. Clears the realm's startup flags, starts both game services, and checks the
   world port. The public Playit addresses and local game ports are unchanged.

Both the checkout and the live deployment are locked against concurrent runs.
An installation failure after shutdown leaves the game services stopped, with
the database running for inspection/recovery. It never silently starts an old
binary against a possibly migrated database. Inspect `migration.log` in the
backup directory for migration errors. A complete backup has a `COMPLETE` file;
do not treat an interrupted backup as a verified recovery point.

Backups contain account credentials and private server configuration. They are
private local files and must not be committed or shared publicly. Copy a
completed backup to another disk/machine if you need protection from disk loss.

## Wrath progression and configuration

Stage 13 starts at Wrath's first raid tier (Naxxramas, Eye of Eternity, Obsidian
Sanctum); later tiers unlock through the module's progression rules. It is a
content-progression stage, not a character starting level. Ordinary characters,
Blood Elves/Draenei, and Death Knights use stage 13. Existing characters below
that stage are advanced by the module when they log in; higher progression is
not reset. Generated bot accounts retain the module's upstream bot exclusion.

The new templates are in `config/modules/`:

- `individualProgression.conf`: enabled, starting stage 13, no progression cap.
- `npc_enchanter.conf`: enabled. A GM can place the NPC in game with
  `.npc add 601015`; installing the template does not choose a spawn location.
- `mod_ahbot.conf`: upstream defaults. The installer supplies the real auction
  character GUID and enables selling through Compose environment overrides.
  Buying remains at its configured default (disabled). Do not log in and play
  the auction character. Population of the auction house takes time.

Edit these three module configs in the shared checkout, then commit/push/pull
changes so both computers agree. Re-running the installer copies those three
files to `--server-dir` after backing up the previous copies. Environment
settings in the generated override take precedence over `.conf` values. Keep the checkout's `.module-build/`
directory: the generated override mounts that exact release's SQL sources.

Optional Vanilla client/DBC patches are deliberately omitted. No client patch
is required for this installation. This module changes world content and item
data even when starting progression at Wrath; test your existing characters,
bots, dungeon access, and auction offerings after the installation.

## Build without deploying

```sh
bash install_modules.sh --build-only
```

This requires neither `.env` nor a running game/database. It only fetches code
and builds images. It does not publish them to a registry. Changing config alone
needs no new image build.

The Git-only workflow builds on the machine running the installer. Docker
images are local build outputs and are not committed to Git. Your friend pulls
the scripts/configuration/source pins and builds his own matching images;
this machine's verification build does not transfer to his server. No image
archive or container registry is required.

## Recover from a failed installation

A SQL migration can change or delete data. Do not roll back only the image or
delete the database volume. Restore the pre-install databases and configuration
together. These commands replace current progress with the selected backup.

First set the actual paths and verify the backup:

```sh
cd "$HOME/wow-migration-20261027T194548Z"
BACKUP="$PWD/backups/before-modules-REPLACE_WITH_ACTUAL_TIMESTAMP"
test -f "$BACKUP/COMPLETE"
(cd "$BACKUP" && sha256sum -c SHA256SUMS)
```

Only proceed if both verification commands succeed:

```sh
docker compose stop --timeout 120 ac-worldserver ac-authserver
docker image load --input "$BACKUP/images.tar"

# Set aside the generated override before restoring the previous files.
if test -f compose.override.yaml; then
  mv compose.override.yaml "compose.override.failed-$(date -u +%Y%m%dT%H%M%SZ).yaml"
fi
tar -xzf "$BACKUP/deployment-files.tar.gz" -C "$PWD"

docker compose --project-directory "$PWD" -f "$BACKUP/previous.compose.json" \
  up -d --wait ac-database

set -o pipefail
gzip -dc "$BACKUP/databases.sql.gz" | \
  docker compose --project-directory "$PWD" -f "$BACKUP/previous.compose.json" \
  exec -T ac-database sh -c 'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot'
```

The SQL backup includes `DROP DATABASE` statements to remove newly introduced
tables as well as restore old rows. If import fails, leave the game stopped and
fix the import; do not continue to startup. After a successful import:

```sh
docker compose --project-directory "$PWD" -f "$BACKUP/previous.compose.json" \
  exec -T ac-database sh -c \
  'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -e "UPDATE acore_auth.realmlist SET flag=0 WHERE id=1;"'

docker compose --project-directory "$PWD" -f "$BACKUP/previous.compose.json" \
  up -d ac-authserver ac-worldserver
```

The saved resolved Compose model uses the exact previous image IDs and original
mount paths. Do not delete old source directories or images while relying on
them for rollback. Client data and source archives are additional copies for
disaster recovery; the installer does not modify the original client-data
volume or old source directories, so a same-host rollback does not need to
overwrite them. Recovery on a different machine needs those volumes and paths
restored first.

## Installer tests

```sh
python3 -m unittest discover -s scripts -p 'test_*.py' -v
```

These exercise backup validation, unsafe auction identities, Wrath settings,
and stopping safely on backup/migration failures without running a live stack.
