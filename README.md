# PS4PkgRenamer

Renames PS4 `.pkg` files using title, Title ID, category, and minimum firmware from each package’s embedded `param.sfo`, so flat installers (e.g. GoldHEN) sort **Base → Update → DLC** with readable names.

Fork of [Yirr777/PS4PkgRenamer](https://github.com/Yirr777/PS4PkgRenamer) with faster SFO reading, safer apply/undo, and a few workflow extras.

```
1 CUSA34384_FXDv1.00_[FW900]-[DLPSGAME.COM].pkg
  ->  God of War Ragnarök [CUSA34384] - 0 Base (FW 9.00+).pkg

2 [DLPSGAME.COM]-UP9000-CUSA34384_00-GOWRAGNAROK00000-A0505-V0100-CyB1K.pkg
  ->  God of War Ragnarök [CUSA34384] - 1 Update (FW 9.00+) v05.05.pkg

3 CUSA34384_GOD_OF_WAR_RAGNAROK_VALHALLA_DLC_FXD.pkg
  ->  God of War Ragnarök [CUSA34384] - 2 DLC - Valhalla.pkg
```

## Download

Grab the latest build from [Releases](https://github.com/bigjokker/PS4PkgRenamer/releases):

| OS | File |
|---|---|
| Windows | `PS4PkgRenamer-windows.exe` |
| macOS | `PS4PkgRenamer-macos.zip` |
| Linux | `PS4PkgRenamer-linux` |

No install — download and run.

> **macOS:** if Gatekeeper blocks it, right-click → **Open**.

## Usage

### Rename Games

Best layout: each game in its own folder (`Games\Title\base+update+dlc.pkg`).

1. **Browse…** to the parent folder (e.g. `PS4/Games`).
2. Optional: **Also rename folders**, **Scan subfolders**, name template.
3. **Preview** → check the log → **Apply**.
4. **Export preview…** or **Undo last…** if needed (writes `.ps4pkgrenamer_last_undo.json`).

### Push to End

For themes / odds and ends that should sort last: prepend `ZZZ - ` (or your own prefix).

## Notes

- `param.sfo` is read from the PKG **header entry table** (plaintext). Scan of the first ~48 MB is only a fallback.
- Preview / Apply run in the background; the UI stays responsive.
- Apply preflight-checks collisions and uses a two-phase rename. Undo log is written on success.
- Single-pkg folders are treated as base (homebrew / backport quirks).
- Shared folder title prefers Base → Update → DLC when base is missing.

## Run from source

Python 3.x with tkinter (stdlib):

```bash
python ps4pkg_renamer.py
```

## Build locally

```bash
pip install pyinstaller
pyinstaller --onefile --windowed --name PS4PkgRenamer ps4pkg_renamer.py   # Windows
```

CI builds Windows / macOS / Linux on every push to `main` and attaches assets to `v*` tags.

## Files

| File | Description |
|---|---|
| `ps4pkg_renamer.py` | App (logic + GUI) |
| `.github/workflows/build.yml` | Release builds |
