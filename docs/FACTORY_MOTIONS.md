# Local factory motions

Manufacturer recordings and their frame-derived variants are not distributed in this repository or covered by
its MIT grant. Obtain action groups from **your own original Hiwonder TonyPi image/SD card**, or download them
yourself from [Hiwonder's official repository](https://github.com/Hiwonder/TonyPi/tree/dc452eaac9991516600860f706f4b889f3305d1d/ActionGroups)
or official product download. We do not host or proxy those recordings. Their upstream redistribution license
is unestablished; importing locally does not grant redistribution rights.

Run as the robot's service user, before starting/restarting tonyd:

```sh
python3 tools/import_factory_motions.py /path/to/your/ActionGroups
# For an existing installation, migrate its OWN already-present files before updating/removing them:
python3 tools/import_factory_motions.py /path/to/old-checkout/data/motions
```

The importer reads SQLite `.d6a` files (ActionGroup, Index/Time/Servo columns) or legacy locally owned JSONs.
It never downloads, opens the robot hardware, changes services or executes motion. It imports into
`~/.local/share/tonypi/factory-motions/`, outside Git. `TONYPI_FACTORY_MOTIONS_DIR` overrides this directory for
both the importer and runtime; apply it consistently to all services and tools. Repository-local copies under
`factory-motions/`, legacy `data/motions/*.json`, and `.d6a` files are ignored. Never force-add them or derived
frames, archives, simulation request payloads or promoted factory snapshots to Git.

`import-report.txt` lists exact validated hash matches, missing actions and differences. Missing/different files
produce exit code 2. Existing differing local files are never overwritten. Conflicting duplicate source names
are refused. Keep your original data; choose a separate destination if comparing another source.

The catalog describes 83 actions from an older owner image. The current official download does not contain
all of them, and some same-named actions differ. The importer does **not** invent missing recordings, patch
frames to force a hash match, or mark new recordings verified. Missing actions produce explicit import errors;
changed hashes are refused by everyday playback. Run the read-only model/calibration audit described in
[MOTION_SOURCES.md](MOTION_SOURCES.md) for changed files, then review its resulting manifest before adoption.
Until then those actions remain unavailable. The current audit expects the full set of catalog sequence
sources; a partial official download cannot complete it until the missing sources are also supplied from
your own image. Model verification never means physical acceptance.

The curated compositions in `data/motion_catalog.json` contain names/hashes/results only, not frame copies.
They resolve the immutable imported source data, ignoring unpromoted overrides. User-authored skills remain in
`TONYPI_USER_DIR`; factory-derived user experiments stay local too. Ten model-screened skills remain available
when importing the exact existing library; eight failed candidates remain unavailable.
