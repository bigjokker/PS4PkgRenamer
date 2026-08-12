# PS4PkgRenamer

Renames PS4 `.pkg` files using the real title, Title ID, category and minimum firmware read from each package's embedded `param.sfo`, so a flat package installer (e.g. GoldHEN) lists everything alphabetically in the right order — Base, then Update, then DLC — instead of cryptic scene-release filenames. Ships as a desktop app for Windows, macOS, and Linux — no Python or dependencies required.

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

Open the app. It has three tabs:

### Rename Games

For folders of games, homebrew apps, or PS1/PS2/PSP-on-PS4 conversions (each game/app in its own subfolder, containing its base + update + DLC pkgs).

1. **Add Folder...** one or more roots (e.g. `Games`, `Homebrew`, `Emulators`) — everything gets scanned and renamed together.
2. Check **Also rename folders** if you also want each game's containing folder renamed to `Title [TitleID]` too. Unchecked by default — leaves folder names untouched, only renames the `.pkg` files.
3. Optionally check **Order by install size (heaviest first)** — instead of pure alphabetical order, this prefixes every name with a rank number (`001 - `, `002 - `, ...) so the heaviest games install first, while there's still plenty of free space on the console's internal storage. Base/Update/DLC ordering within each game is unaffected. You lose the ability to find a game alphabetically by name in exchange for this.
4. **Pin first** (comma-separated folder name matches) forces specific apps to install before everything else, ahead of the size ranking — defaults to `RetroArch, PS4-Xplorer`, since those are useful to have working early (a file explorer, an emulator whose ROM/data folder you still need to copy over manually). Edit or clear it for your own setup.
5. Click **Preview** to see what would change without touching any files.
6. Click **Apply** to actually rename. A confirmation dialog appears first.

Re-running this after adding a new game recomputes the ranking from scratch across everything, since a new heavy game can shift every rank number after it.

### Push to End

For a flat folder of unrelated pkgs (e.g. a PS4 theme pack) that you want sorted to the very end of the installer's list, without touching the rest of each filename.

1. Browse to the folder.
2. (Optional) change the prefix — defaults to `ZZZ - `.
3. Preview, then Apply.

Running it again on an already-processed folder is safe — files that already have the prefix (or are already correctly named) are skipped.

### Install Order

The PS4 needs free space on its internal storage to install each game. If you install in alphabetical order, a heavy game near the end of the list can end up with no room left, even though it would have fit fine if installed earlier. This tab doesn't rename anything — it just reads file sizes and gives you a reference list, heaviest first, so you know what to install while space is still abundant.

1. **Add Folder...** one or more roots (e.g. `Games`, `Homebrew`, `Emulators`, `Themes`).
2. Click **Generate Report** — it sums up every game folder's `.pkg` sizes (base + update + DLC combined) and lists them from heaviest to lightest, with a running total.
3. **Save Report...** to keep it as a text file.

The pkg file size is a close approximation of the actual installed size, not an exact figure — leave some extra headroom rather than installing right up to the limit.

---

## Notes

- Reading `param.sfo` doesn't require decrypting the pkg: it's stored unencrypted so the console itself can show the title before installing. The app reads only the first ~48 MB of each file looking for it — it never reads the (potentially huge) game data.
- Names are cleaned for exFAT/Windows compatibility (forbidden characters removed, no trailing dot/space) without corrupting titles that contain a dot mid-name (e.g. `P.T.`).
- The sort-order marker (`- 0 Base` / `- 1 Update` / `- 2 DLC`) always comes immediately after the title, before any firmware/version text — so the order never flips even if an update happens to require an *older* firmware than the base game.
- A folder with a single pkg is always treated as the base app, regardless of its internal category — this matters for some homebrew/backport packages that are technically tagged as a "patch" even though they're the only file needed.
- If two unrelated pkgs end up sharing a folder (same Title ID, different content — e.g. an emulator plus a separate cores installer), each keeps its own title instead of being forced under one shared name.
- Category matching is by prefix, not exact match (`gd`, `gp`, `ac`) — some PS2-classics conversions use variants like `gdo`/`gpo` instead of `gd`/`gp`.

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
