# RAGWotlkServer

To build and install Individual Progression, NPC Enchanter, and AH Bot Plus with
a pre-install backup, follow [MODULES.md](MODULES.md). Source revisions are pinned
in `modules.lock.json`; run `bash install_modules.sh --help` for the entry point.

Map updates use four workers, configured by `AC_MAP_UPDATE_THREADS` in
`compose.yaml`. This overrides `MapUpdate.Threads` in `config/worldserver.conf`.
Check `nproc` inside WSL before deploying; the host should have at least four
logical CPUs available. Additional workers distribute work across maps, but do
not split a single map update across workers.

Apply changes from the running deployment directory with:

```sh
docker compose config --quiet && bash reload.sh
```

This recreates worldserver and disconnects players. Recreation applies Compose
environment changes as well as configuration file edits. It does not rebuild
images or run database migrations.

Run `.server info` in game before and after the change at the same online bot
count. Compare average, median, and 95th/99th percentile update times. If they
worsen, change `AC_MAP_UPDATE_THREADS` back to `"2"` and apply again.
