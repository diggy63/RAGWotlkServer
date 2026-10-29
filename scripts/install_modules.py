#!/usr/bin/env python3
"""Build pinned modules, back up an existing deployment, then migrate it."""

import argparse
import fcntl
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
DATABASES = ("acore_auth", "acore_characters", "acore_world", "acore_playerbots")
CONFIGS = ("individualProgression.conf", "npc_enchanter.conf", "mod_ahbot.conf")


def run(args, *, capture=False, **kwargs):
    kwargs.setdefault("stdout", subprocess.PIPE if capture else None)
    result = subprocess.run(args, check=True, text=capture, **kwargs)
    return result.stdout.strip() if capture else None


def compose(server, *args, capture=False, **kwargs):
    command = ["docker", "compose", "--project-directory", str(server),
               "-f", str(server / "compose.yaml")]
    if (server / "compose.override.yaml").exists():
        command += ["-f", str(server / "compose.override.yaml")]
    return run(command + list(args), capture=capture, cwd=server, **kwargs)


def write_json(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2) + "\n")
    temporary.replace(path)


def fetch_source(path, source):
    if not re.fullmatch(r"[0-9a-f]{40}", source["commit"]):
        raise ValueError("Source revisions must be full Git commit hashes")
    if not source["url"].startswith("https://github.com/"):
        raise ValueError("Expected a public GitHub source URL")
    if not path.exists():
        run(["git", "init", "--quiet", str(path)])
        run(["git", "-C", str(path), "remote", "add", "origin", source["url"]])
    origin = run(["git", "-C", str(path), "remote", "get-url", "origin"], capture=True)
    if origin != source["url"]:
        raise ValueError(f"Unexpected Git remote in {path}")
    changes = run(["git", "-C", str(path), "status", "--porcelain", "--untracked-files=no"], capture=True)
    if changes:
        raise ValueError(f"Refusing to overwrite edited source files in {path}")
    try:
        current = run(["git", "-C", str(path), "rev-parse", "--verify", "HEAD"], capture=True,
                      stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        current = None
    if current == source["commit"]:
        return
    run(["git", "-C", str(path), "fetch", "--depth=1", "origin", source["commit"]])
    run(["git", "-C", str(path), "checkout", "--quiet", "--detach", source["commit"]])


def build_images(lock, release):
    directory = ROOT / ".module-build" / release
    directory.mkdir(parents=True, exist_ok=True)
    core = directory / "core"
    fetch_source(core, lock["core"])
    for name, source in lock["modules"].items():
        if not re.fullmatch(r"mod-[a-z0-9-]+", name):
            raise ValueError("Invalid module directory name")
        fetch_source(core / "modules" / name, source)

    # Sources are public; runtime UID 1000 must be able to read bind-mounted SQL on any host UID.
    (ROOT / ".module-build").chmod(0o755)
    directory.chmod(0o755)
    for parent, directories, files in os.walk(core):
        Path(parent).chmod(0o755)
        for name in files:
            path = Path(parent) / name
            if not path.is_symlink():
                path.chmod(path.stat().st_mode | 0o044)

    dockerfile = (core / "apps/docker/Dockerfile").read_text()
    parallelism = '-j $(($(nproc) + 1))'
    if dockerfile.count(parallelism) != 1:
        raise ValueError("Upstream Dockerfile changed; review its build parallelism before installing")
    jobs = int(lock["build_jobs"])
    if not 1 <= jobs <= 16:
        raise ValueError("build_jobs must be between 1 and 16")
    dockerfile = dockerfile.replace(parallelism, f"-j {jobs}")
    recipe = directory / "Dockerfile"
    recipe.write_text(dockerfile)
    images = {service: f"local/rag-{service}:{release}" for service in ("worldserver", "authserver")}
    for service in ("worldserver", "authserver"):
        run(["docker", "build", "--file", str(recipe), "--target", service,
             "--build-arg", "CTOOLS_BUILD=none", "--build-arg", "CTYPE=Release",
             "--build-arg", "CWITH_WARNINGS=OFF", "--tag", images[service], str(core)])
    return core, images


def query(server, sql):
    # SQL is provided over stdin; neither a password nor account data appears in argv.
    return compose(server, "exec", "-T", "ac-database", "sh", "-c",
                   'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot -N -B',
                   capture=True, input=sql)


def auction_guid(server, name):
    name_hex = name.encode("utf-8").hex()
    result = query(server, f"""
SELECT c.guid, c.online, COALESCE(b.account_type, 0)
FROM acore_characters.characters c
LEFT JOIN acore_playerbots.playerbots_account_type b ON b.account_id=c.account
WHERE c.name=CONVERT(0x{name_hex} USING utf8mb4);
""")
    rows = result.splitlines()
    if len(rows) != 1:
        raise ValueError("Auction character was not found uniquely; create a dedicated normal character first")
    guid, online, bot_type = map(int, rows[0].split("\t"))
    if guid <= 0 or online or bot_type:
        raise ValueError("Auction character must be offline and on a normal, non-Playerbots account")
    return guid


def preflight(server, character):
    if not (server / ".env").is_file():
        raise ValueError("Missing deployment .env; use --server-dir to point to the existing live server folder")
    override = server / "compose.override.yaml"
    if override.exists():
        try:
            managed = json.loads(override.read_text()).get("x-rag-module-installer") is True
        except (ValueError, AttributeError):
            managed = False
        if not managed:
            raise ValueError("Existing compose.override.yaml is not managed by this installer; refusing to replace it")
    model = json.loads(compose(server, "--profile", "tools", "config", "--format", "json", capture=True))
    containers = {}
    for service in ("ac-database", "ac-authserver", "ac-worldserver"):
        container = compose(server, "ps", "--all", "--quiet", service, capture=True)
        if not container or "\n" in container:
            raise ValueError(f"Expected one existing {service} container")
        containers[service] = json.loads(run(["docker", "inspect", container], capture=True))[0]
    if containers["ac-database"]["State"].get("Health", {}).get("Status") != "healthy":
        raise ValueError("The existing database must be running and healthy")
    world = containers["ac-worldserver"]
    config_mount = next(m for m in world["Mounts"] if m["Destination"] == "/azerothcore/env/dist/etc")
    if Path(config_mount["Source"]).resolve() != (server / "config").resolve():
        raise ValueError("This folder is not the running deployment; use --server-dir with its actual location")
    databases = query(server, "SHOW DATABASES;").splitlines()
    for name in DATABASES:
        if name not in databases:
            raise ValueError(f"Required database is missing: {name}")
    if not character:
        raise ValueError("Supply --ah-character with a dedicated offline auction character name")
    auction_guid(server, character)
    return model, containers


def verify_dump(path):
    found = set()
    tail = b""
    with gzip.open(path, "rb") as stream:
        while chunk := stream.read(1024 * 1024):
            data = tail + chunk
            for name in DATABASES:
                if f"-- Current Database: `{name}`".encode() in data:
                    found.add(name)
            tail = data[-4096:]
    if found != set(DATABASES) or b"-- Dump completed on" not in tail:
        raise ValueError("Database backup is incomplete; migration will not start")


def backup(server, model, containers):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    destination = server / "backups" / f"before-modules-{stamp}"
    destination.mkdir(parents=True, mode=0o700)
    # Capture the deployed model and exact image IDs, not movable tags from Git.
    for service, container in containers.items():
        model["services"][service]["image"] = container["Image"]
    write_json(destination / "previous.compose.json", model)
    with tarfile.open(destination / "deployment-files.tar.gz", "w:gz") as archive:
        for name in ("config", "compose.yaml", "compose.override.yaml", ".env"):
            if (server / name).exists():
                archive.add(server / name, arcname=name)
    # Keep SQL binary-safe, including account salts/verifiers. Failed dumps never get a COMPLETE marker.
    command = ["docker", "exec", containers["ac-database"]["Id"], "sh", "-c",
               'MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysqldump -uroot '
               '--single-transaction --quick --hex-blob --routines --events --triggers '
               '--set-gtid-purged=OFF --no-tablespaces --add-drop-database --databases '
               + " ".join(DATABASES)]
    with gzip.open(destination / "databases.sql.gz", "wb") as compressed:
        process = subprocess.Popen(command, stdout=subprocess.PIPE)
        try:
            with process.stdout:
                shutil.copyfileobj(process.stdout, compressed)
        except BaseException:
            process.terminate()
            process.wait()
            raise
        if process.wait() != 0:
            raise RuntimeError("Database dump failed")
    verify_dump(destination / "databases.sql.gz")
    image_ids = list(dict.fromkeys(c["Image"] for c in containers.values()))
    run(["docker", "image", "save", "--output", str(destination / "images.tar"), *image_ids])
    world = containers["ac-worldserver"]
    client = next(m for m in world["Mounts"] if m["Destination"] == "/azerothcore/env/dist/data")
    if client["Type"] != "volume":
        raise ValueError("Expected client data to use the existing named Docker volume")
    run(["docker", "run", "--rm", "--user", "0", "--entrypoint", "tar",
         "--mount", f"type=volume,src={client['Name']},dst=/data,readonly",
         "--mount", f"type=bind,src={destination},dst=/backup", world["Image"],
         "-czf", "/backup/client-data.tar.gz", "-C", "/data", "."])
    with tarfile.open(destination / "source-data.tar.gz", "w:gz") as archive:
        for mount in world["Mounts"]:
            if mount["Type"] == "bind" and mount["Destination"] in ("/azerothcore/modules", "/azerothcore/data"):
                archive.add(mount["Source"], arcname=mount["Destination"].removeprefix("/azerothcore/"))
    for name in ("deployment-files.tar.gz", "client-data.tar.gz", "source-data.tar.gz"):
        with tarfile.open(destination / name, "r:gz") as archive:
            archive.getmembers()
    checksums = []
    for path in sorted(destination.iterdir()):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        checksums.append(f"{digest}  {path.name}\n")
    (destination / "SHA256SUMS").write_text("".join(checksums))
    (destination / "COMPLETE").write_text("All backup steps completed before migration.\n")
    return destination


def configure_modules(server):
    target = server / "config/modules"
    target.mkdir(parents=True, exist_ok=True)
    for filename in CONFIGS:
        destination = target / filename
        source = ROOT / "config/modules" / filename
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
            destination.chmod(0o644)
    progression = target / "individualProgression.conf"
    text = progression.read_text()
    for key in ("StartingProgression", "tbcRacesStartingProgression", "DeathKnightStartingProgression"):
        pattern = rf"(?m)^IndividualProgression\.{key}\s*=.*$"
        if len(re.findall(pattern, text)) != 1:
            raise ValueError(f"Expected exactly one progression setting: {key}")
        text = re.sub(pattern, f"IndividualProgression.{key} = 13", text)
    progression.write_text(text)


def overlay(core, images, guid):
    mounts = [f"{core / 'modules'}:/azerothcore/modules:ro", f"{core / 'data'}:/azerothcore/data:ro"]
    environment = {"AC_ENABLE_PLAYER_SETTINGS": "1", "AC_DBC_ENFORCE_ITEM_ATTRIBUTES": "0",
                   "AC_AUCTION_HOUSE_BOT_GUIDS": str(guid), "AC_AUCTION_HOUSE_BOT_ENABLE_SELLER": "1"}
    return {
        "x-rag-module-installer": True,
        "services": {
            "ac-authserver": {"image": images["authserver"]},
            "ac-worldserver": {"image": images["worldserver"], "volumes": mounts,
                               "environment": {**environment, "AC_UPDATES_ENABLE_DATABASES": "0"}},
            "migrate": {"image": images["worldserver"], "volumes": mounts,
                        "environment": {**environment, "AC_UPDATES_ENABLE_DATABASES": "7",
                                        "AC_AUCTION_HOUSE_BOT_ENABLE_SELLER": "0",
                                        "AC_AUCTION_HOUSE_BOT_BUYER_ENABLED": "0"}},
        },
    }


def install(server, character, model, containers, core, images):
    print("Stopping game services for the backup and migration...", flush=True)
    compose(server, "stop", "--timeout", "120", "ac-worldserver", "ac-authserver")
    destination = None
    try:
        destination = backup(server, model, containers)
        print(f"Verified backup: {destination}", flush=True)
        guid = auction_guid(server, character)
        configure_modules(server)
        write_json(server / "compose.override.yaml", overlay(core, images, guid))
        compose(server, "config", "--quiet")
        print("Applying core and module SQL; see migration.log in the backup folder...", flush=True)
        with (destination / "migration.log").open("wb") as log:
            compose(server, "run", "--rm", "--no-deps", "migrate", stdout=log, stderr=subprocess.STDOUT)
        with (destination / "migration.log").open(errors="replace") as log:
            if not any("AzerothCore Dry Run Completed, Terminating." in line for line in log):
                raise RuntimeError("Migration did not report successful dry-run completion")
        # A dry run marks the realm unavailable; clear it before starting authserver.
        query(server, "UPDATE acore_auth.realmlist SET flag=0 WHERE id=1;")
        compose(server, "up", "-d", "--wait", "--wait-timeout", "600", "ac-authserver", "ac-worldserver")
        # Check the actual game listener, not just Docker's running state.
        compose(server, "exec", "-T", "ac-worldserver", "bash", "-c",
                'for attempt in {1..120}; do (exec 3<>/dev/tcp/127.0.0.1/8085) 2>/dev/null && exit 0; sleep 5; done; exit 1')
        compose(server, "ps")
        print(f"Modules installed. Backup retained at {destination}", flush=True)
    except BaseException:
        # Never automatically restart old binaries after potentially irreversible SQL changes.
        try:
            compose(server, "stop", "--timeout", "120", "ac-worldserver", "ac-authserver")
        except subprocess.CalledProcessError:
            pass
        print(f"Installation failed; game services remain stopped. Backup: {destination or server / 'backups'}", file=sys.stderr)
        print("Read MODULES.md before restoring; do not restart old images against a migrated database.", file=sys.stderr)
        raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server-dir", type=Path, default=ROOT, help="Existing live deployment folder (with .env)")
    parser.add_argument("--ah-character", help="Dedicated offline character on a normal account, for auction ownership")
    parser.add_argument("--build-only", action="store_true", help="Build images without touching the server or databases")
    args = parser.parse_args()
    os.umask(0o077)
    for executable in ("docker", "git"):
        if not shutil.which(executable):
            parser.error(f"Required program is missing: {executable}")
    build_root = ROOT / ".module-build"
    build_root.mkdir(exist_ok=True)
    with (build_root / "install.lock").open("w") as lockfile:
        fcntl.flock(lockfile, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_bytes = (ROOT / "modules.lock.json").read_bytes()
        lock = json.loads(lock_bytes)
        release = "modules-" + hashlib.sha256(lock_bytes).hexdigest()[:12]
        server = args.server_dir.expanduser().resolve()
        if args.build_only:
            _, images = build_images(lock, release)
            print(json.dumps(images, indent=2))
            return
        # Lock the deployment too: two checkouts must not migrate the same server concurrently.
        with (server / ".module-install.lock").open("w") as deployment_lock:
            fcntl.flock(deployment_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            model, containers = preflight(server, args.ah_character)
            print("Preparing pinned images while the existing server remains available...", flush=True)
            core, images = build_images(lock, release)
            # Re-read after a long build so the backup matches what is actually deployed now.
            model, containers = preflight(server, args.ah_character)
            install(server, args.ah_character, model, containers, core, images)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, RuntimeError, OSError, subprocess.CalledProcessError) as error:
        # CalledProcessError can contain resolved Compose arguments; keep the diagnostic concise.
        message = f"Command failed with exit status {error.returncode}" if isinstance(error, subprocess.CalledProcessError) else str(error)
        print(f"ERROR: {message}", file=sys.stderr)
        sys.exit(1)
