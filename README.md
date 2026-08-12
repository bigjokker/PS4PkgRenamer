# PS4PkgRenamer

Renames PS4 `.pkg` files using the real title, Title ID, category and minimum firmware read from each package's embedded `param.sfo`, so a flat package installer (e.g. GoldHEN) lists everything alphabetically in the right order — Base, then Update, then DLC — instead of cryptic scene-release filenames. Ships as a desktop app for Windows, macOS, and Linux — no Python or dependencies required.

**v1.1.1:** multi-folder undo with “Also rename folders” no longer corrupts earlier game paths; broader Title ID / category recognition; safer filenames (Windows reserved names, length limits); separate preview export buffers; Undo on both tabs; folder rename still runs when pkgs are already correctly named.

```
1 CUSA34384_FXDv1.00_[FW900]-[DLPSGAME.COM].pkg
  ->  God of War Ragnarök [CUSA34384] - 0 Base (FW 9.00+).pkg

2 [DLPSGAME.COM]-UP9000-CUSA34384_00-GOWRAGNAROK00000-A0505-V0100-CyB1K.pkg
  ->  God of War Ragnarök [CUSA34384] - 1 Update (FW 9.00+) v05.05.pkg

3 CUSA34384_GOD_OF_WAR_RAGNAROK_VALHALLA_DLC_FXD.pkg
  ->  God of War Ragnarök [CUSA34384] - 2 DLC - Valhalla.pkg
```

---

## Download

Grab the latest build for your OS from the [Releases](../../releases) page:

| OS | File |
|---|---|
| Windows | `PS4PkgRenamer-windows.exe` |
| macOS | `PS4PkgRenamer-macos.zip` (unzip, then open `PS4PkgRenamer.app`) |
| Linux | `PS4PkgRenamer-linux` (make it executable: `chmod +x PS4PkgRenamer-linux`) |

No installation needed — just download and run.

> macOS may show an "unidentified developer" warning since the app isn't notarized. Right-click the app and choose **Open** to bypass it.

---

## Files

| File | Description |
|---|---|
| `ps4pkg_renamer.py` | App source (logic + GUI, single file) |

---

## Usage

Open the app. It has two tabs:

### Rename Games

For a folder of games, homebrew apps, or PS1/PS2/PSP-on-PS4 conversions (each game/app in its own subfolder, containing its base + update + DLC pkgs).

1. Click **Browse...** and select the folder (e.g. `PS4/Games`, `PS4/Homebrew`, `PS4/Emulators`).
2. Check **Also rename folders** if you also want each game's containing folder renamed to `Title [TitleID]`.
3. Check **Scan subfolders** for a full recursive walk (default on). Uncheck to only scan the selected folder and its immediate children (safer for large trees).
4. Optionally pick a **Name template** (GoldHEN sort, without firmware, compact, title-only).
5. Click **Preview** to see what would change without touching any files.
6. Click **Apply** to actually rename. A confirmation dialog appears first.
7. Use **Export preview...** to save the log as `.txt` or `.csv`.
8. Use **Undo last...** to reverse the last apply (reads `.ps4pkgrenamer_last_undo.json` written next to the target folder).

### Push to End

For a flat folder of unrelated pkgs (e.g. a PS4 theme pack) that you want sorted to the very end of the installer's list, without touching the rest of each filename.

1. Browse to the folder.
2. (Optional) change the prefix — defaults to `ZZZ - `.
3. Preview, then Apply.

Running it again on an already-processed folder is safe — files that already have the prefix (or are already correctly named) are skipped.

---

## Notes

- **SFO extraction:** the app first reads the PS4 PKG header and meta entry table (entry id `0x1000` = plaintext `param.sfo`). If that fails, it falls back to scanning the first ~48 MB for the SFO magic. Reading SFO does not require decrypting game data.
- **Background jobs:** Preview / Apply run on a worker thread so the UI stays responsive; a progress bar and summary strip show status.
- **Safe apply:** renames are preflight-checked for target collisions, then applied with a two-phase temp rename. An undo log is written on successful apply. File undo paths are rewritten only for the folder that was just renamed (so multi-game Apply + Undo stays consistent).
- Names are cleaned for exFAT/Windows compatibility (forbidden characters removed, no trailing dot/space, reserved device names avoided, long stems truncated) without corrupting titles that contain a dot mid-name (e.g. `P.T.`).
- The sort-order marker (`- 0 Base` / `- 1 Update` / `- 2 DLC`) always comes immediately after the title, before any firmware/version text — so the order never flips even if an update happens to require an *older* firmware than the base game.
- A folder with a single pkg is always treated as the base app, regardless of its internal category — this matters for some homebrew/backport packages that are technically tagged as a "patch" even though they're the only file needed.
- If two unrelated pkgs end up sharing a folder (same Title ID, different content — e.g. an emulator plus a separate cores installer), each keeps its own title instead of being forced under one shared name.
- Title IDs are matched as four letters + five digits (CUSA, PCAS, PLAS, PPSA, …). Theme / extra SFO categories map to `- 8 Theme` / `- 9 Other` when Rename Games is used on them.

---

## Running from source

Requires Python 3.x (tkinter included).

```
python ps4pkg_renamer.py
```

### Building the executables yourself

Executables for all three platforms are built automatically by the [`build` GitHub Actions workflow](.github/workflows/build.yml) on every push to `main` and on tagged releases (`v*`). To build locally:

```
pip install pyinstaller
pyinstaller --onefile --windowed --name PS4PkgRenamer ps4pkg_renamer.py   # Windows
pyinstaller --windowed --name PS4PkgRenamer ps4pkg_renamer.py             # macOS
pyinstaller --onefile --name PS4PkgRenamer ps4pkg_renamer.py              # Linux
```
