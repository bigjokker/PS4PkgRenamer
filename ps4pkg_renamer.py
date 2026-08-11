#!/usr/bin/env python3
"""PS4PkgRenamer — reads param.sfo from PS4 .pkg files to rename them for
correct alphabetical sorting in flat package-list installers (GoldHEN)."""

import csv
import os
import re
import struct
import unicodedata
import tkinter as tk
from datetime import datetime
from tkinter import ttk, filedialog, messagebox, scrolledtext

APP_TITLE = "PS4PkgRenamer"

# ---------------------------------------------------------------------------
# Core logic: param.sfo parsing
# ---------------------------------------------------------------------------

SFO_MAGIC = b"\x00PSF"

# CATEGORY -> (etiqueta, orden, sufijo)
# El juego base tambien lleva un sufijo ("- 0 Base") en vez de quedar
# vacio: si se dejara sin sufijo, "Nombre.pkg" ordena DESPUES de
# "Nombre - 1 Update.pkg" porque el espacio (0x20) es menor que el punto
# (0x2E) en ASCII, invirtiendo el orden que se busca.
CATEGORY_MAP = {
    "gd":  ("base",   0, " - 0 Base"),
    "gde": ("base",   0, " - 0 Base"),   # demo, se trata como base por si aparece
    "gp":  ("update", 1, " - 1 Update"),
    "ac":  ("dlc",    2, " - 2 DLC"),
}
FALLBACK_DIGIT_MAP = {
    "1": ("base",   0, " - 0 Base"),
    "2": ("update", 1, " - 1 Update"),
    "3": ("dlc",    2, " - 2 DLC"),
}
OTHER = ("other", 9, " - 9 Otro")

FORBIDDEN_CHARS = re.compile(r'[\\/:*?"<>|]')

DEFAULT_SCAN_MB = 48
DEFAULT_PREFIX = "ZZZ - "


def sanitize_name(name: str) -> str:
    """Limpia un componente de nombre (titulo base, titulo de DLC, etc).
    No recorta puntos/espacios finales aqui: ese componente puede no quedar
    al final del nombre de archivo completo (ej. "P.T." en medio del
    nombre), asi que ese recorte se aplica solo una vez, en
    finalize_filename(), sobre el nombre ya armado por completo."""
    if not name:
        return "Unknown"
    name = name.replace(":", " -")
    name = FORBIDDEN_CHARS.sub("", name)
    name = re.sub(r"\s+", " ", name).strip()
    return name or "Unknown"


def finalize_filename(name: str) -> str:
    """Aplica el recorte de puntos/espacios finales (invalido en exFAT y
    Windows) sobre el nombre de archivo YA completo, antes de agregar
    la extension .pkg."""
    name = sanitize_name(name)
    name = name.rstrip(" .")
    return name or "Unknown"


def parse_sfo(data: bytes):
    """Parsea un bloque param.sfo ya completo en memoria y devuelve un dict."""
    if len(data) < 20 or data[0:4] != SFO_MAGIC:
        return None
    key_table_offset, data_table_offset, count = struct.unpack_from("<III", data, 0x08)
    if count == 0 or count > 200:
        return None
    entries = {}
    for i in range(count):
        entry_off = 0x14 + i * 16
        if entry_off + 16 > len(data):
            break
        key_offset, data_fmt, data_len, data_max_len, data_offset = struct.unpack_from(
            "<HHIII", data, entry_off
        )
        k_start = key_table_offset + key_offset
        if k_start >= len(data):
            continue
        k_end = data.find(b"\x00", k_start)
        if k_end == -1:
            k_end = k_start
        key = data[k_start:k_end].decode("utf-8", "replace")

        val_start = data_table_offset + data_offset
        val_bytes = data[val_start:val_start + data_len]
        if data_fmt in (0x0004, 0x0204):
            value = val_bytes.split(b"\x00", 1)[0].decode("utf-8", "replace")
        elif data_fmt == 0x0404:
            padded = val_bytes.ljust(4, b"\x00")[:4]
            value = struct.unpack("<I", padded)[0]
        else:
            value = val_bytes
        entries[key] = value
    return entries


def find_sfo_in_buffer(buf: bytes):
    """Busca todas las firmas SFO en el buffer y devuelve el primer bloque
    valido que contenga la clave TITLE."""
    start = 0
    while True:
        idx = buf.find(SFO_MAGIC, start)
        if idx == -1:
            return None
        start = idx + 1
        if idx + 20 > len(buf):
            continue
        try:
            key_table_offset, data_table_offset, count = struct.unpack_from(
                "<III", buf, idx + 0x08
            )
        except struct.error:
            continue
        if count == 0 or count > 200:
            continue
        if key_table_offset > 0x20000 or data_table_offset > 0x20000:
            continue
        end_guess = idx + max(key_table_offset, data_table_offset) + 8192
        end_guess = min(end_guess, len(buf))
        entries = parse_sfo(buf[idx:end_guess])
        if entries and "TITLE" in entries:
            return entries
    return None


def extract_sfo(pkg_path: str, scan_mb: int, log=None):
    """Lee el pkg en bloques hasta scan_mb buscando param.sfo. Devuelve dict o None."""
    max_bytes = scan_mb * 1024 * 1024
    chunk_size = 4 * 1024 * 1024
    buf = b""
    try:
        with open(pkg_path, "rb") as f:
            read_total = 0
            while read_total < max_bytes:
                chunk = f.read(chunk_size)
                if not chunk:
                    break
                buf += chunk
                read_total += len(chunk)
                entries = find_sfo_in_buffer(buf)
                if entries:
                    return entries
    except OSError as e:
        if log:
            log(f"  [ERROR] No se pudo leer {pkg_path}: {e}")
        return None
    return find_sfo_in_buffer(buf)


def format_fw_version(system_ver):
    """SYSTEM_VER es un entero donde el byte mas alto es la parte entera del
    firmware minimo requerido y el siguiente byte es la parte decimal, cada
    uno impreso como par de digitos hex (0x06720000 -> "6.72")."""
    if not isinstance(system_ver, int) or system_ver <= 0:
        return None
    major = (system_ver >> 24) & 0xFF
    minor = (system_ver >> 16) & 0xFF
    return f"{major:x}.{minor:02x}"


def classify(entries, original_filename: str):
    """Devuelve (label, order, suffix, titulo, version, title_id, fw_version)."""
    category = None
    version = None
    title = None
    title_id = None
    fw_version = None
    if entries:
        category = str(entries.get("CATEGORY", "")).strip().lower()
        version = entries.get("APP_VER") or entries.get("VERSION")
        title = entries.get("TITLE")
        if isinstance(title, str):
            title = title.strip()
        title_id = entries.get("TITLE_ID")
        if isinstance(title_id, str):
            title_id = title_id.strip() or None
        fw_version = format_fw_version(entries.get("SYSTEM_VER"))

    if category in CATEGORY_MAP:
        label, order, suffix = CATEGORY_MAP[category]
    else:
        # respaldo: usar el digito inicial que ya usa el nombre original (1/2/3)
        m = re.match(r"\s*([123])\b", original_filename)
        if m:
            label, order, suffix = FALLBACK_DIGIT_MAP[m.group(1)]
        else:
            label, order, suffix = OTHER

    return label, order, suffix, title, version, title_id, fw_version


def strip_base_prefix(dlc_title: str, base_title: str) -> str:
    if not dlc_title:
        return dlc_title
    bt = base_title.strip().lower()
    dt = dlc_title.strip()
    low = dt.lower()
    for sep in (":", " -", "-"):
        prefix = bt + sep
        if low.startswith(prefix):
            return dt[len(prefix):].strip(" -:")
    return dt


def scan_game_folder(folder: str, scan_mb: int, log=None):
    """Analiza los .pkg de una carpeta y devuelve una lista de dicts con la
    informacion necesaria para renombrar."""
    pkg_files = sorted(
        f for f in os.listdir(folder) if f.lower().endswith(".pkg")
    )
    if not pkg_files:
        return []

    items = []
    for fname in pkg_files:
        full_path = os.path.join(folder, fname)
        entries = extract_sfo(full_path, scan_mb, log)
        label, order, suffix, title, version, title_id, fw_version = classify(entries, fname)
        items.append({
            "path": full_path,
            "filename": fname,
            "label": label,
            "order": order,
            "suffix": suffix,
            "title": title,
            "version": version,
            "title_id": title_id,
            "fw_version": fw_version,
            "found_metadata": entries is not None,
        })

    # Si la carpeta tiene un solo pkg, ese pkg ES el juego/app completo sin
    # importar que su CATEGORY interna diga "gp" (update). Pasa con algunos
    # pkgs homebrew/conversiones PS1-PS2-PSP que se empaquetan tecnicamente
    # como "parche" aunque sean el unico archivo necesario para instalar.
    if len(items) == 1 and items[0]["order"] != 0:
        items[0]["order"] = 0
        items[0]["label"] = "base"

    # Titulo base: el del pkg marcado como "base"; si no hay, el de
    # cualquier otro pkg que tenga titulo; si no, el nombre de la carpeta.
    base_title = None
    for it in items:
        if it["order"] == 0 and it["title"]:
            base_title = it["title"]
            break
    if not base_title:
        for it in items:
            if it["title"]:
                base_title = it["title"]
                break
    if not base_title:
        base_title = os.path.basename(folder.rstrip(os.sep))

    base_title = sanitize_name(base_title)

    # Title ID (CUSAXXXXX): el del pkg base: si no hay, el de cualquier otro.
    base_title_id = None
    for it in items:
        if it["order"] == 0 and it["title_id"]:
            base_title_id = it["title_id"]
            break
    if not base_title_id:
        for it in items:
            if it["title_id"]:
                base_title_id = it["title_id"]
                break

    label = f"{base_title} [{base_title_id}]" if base_title_id else base_title

    # Si mas de un pkg quedo clasificado como "base" en la misma carpeta,
    # el supuesto de "un solo juego con base+update+dlc por carpeta" no
    # aplica (ej. una carpeta con 2 apps homebrew distintas que comparten
    # Title ID). En ese caso cada pkg usa SU PROPIO titulo en vez de
    # forzarlos a todos bajo el mismo label, para no colisionar nombres.
    ambiguous = sum(1 for it in items if it["order"] == 0) > 1

    for it in items:
        if ambiguous:
            own_title = sanitize_name(it["title"]) if it["title"] else sanitize_name(
                os.path.splitext(it["filename"])[0]
            )
            item_label = f"{own_title} [{it['title_id']}]" if it["title_id"] else own_title
        else:
            item_label = label

        # El digito de orden (0/1/2) debe quedar SIEMPRE justo despues del
        # label constante y ANTES de cualquier dato que varie por pkg (FW,
        # version, nombre de DLC). Si el FW fuera menor en el update que en
        # el base (ej. update 4.55 vs base 9.00) y el FW apareciera antes
        # del digito de orden, el orden alfabetico se invertiria.
        fw_tag = f" (FW {it['fw_version']}+)" if it["fw_version"] else ""

        if it["order"] == 0:
            new_name = f"{item_label} - 0 Base{fw_tag}"
        elif it["order"] == 1:
            new_name = f"{item_label} - 1 Update{fw_tag}"
            if it["version"]:
                new_name += f" v{it['version']}"
        elif it["order"] == 2:
            dlc_title = it["title"]
            if dlc_title:
                dlc_title = sanitize_name(dlc_title)
                dlc_title = strip_base_prefix(dlc_title, base_title)
            if not dlc_title:
                dlc_title = sanitize_name(os.path.splitext(it["filename"])[0])
            new_name = f"{item_label} - 2 DLC{fw_tag} - {dlc_title}"
        else:
            other_title = sanitize_name(it["title"] or os.path.splitext(it["filename"])[0])
            new_name = f"{item_label} - 9 Otro{fw_tag} - {other_title}"

        it["new_filename"] = finalize_filename(new_name) + ".pkg"
        it["base_title"] = label

    # evita colisiones de nombre dentro de la misma carpeta (p.ej. dos DLC iguales)
    seen = {}
    for it in items:
        key = it["new_filename"].lower()
        if key in seen:
            seen[key] += 1
            stem, ext = os.path.splitext(it["new_filename"])
            it["new_filename"] = f"{stem} ({seen[key]}){ext}"
        else:
            seen[key] = 1

    return items


def find_game_folders(root: str):
    """Cualquier carpeta que contenga directamente uno o mas .pkg se trata
    como una 'carpeta de juego'."""
    for dirpath, dirnames, filenames in os.walk(root):
        if any(f.lower().endswith(".pkg") for f in filenames):
            yield dirpath


def rename_games(root: str, apply: bool, folders: bool, scan_mb: int, log):
    """Escanea/renombra juegos, homebrew o emuladores bajo `root`.
    Devuelve un dict resumen. `log(message)` recibe cada linea de progreso."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        log(f"No existe la carpeta: {root}")
        return {"total": 0, "unknown": 0, "renamed": 0, "manifest_path": None}

    all_folders = list(find_game_folders(root))
    if not all_folders:
        log("No se encontraron archivos .pkg debajo de esa ruta.")
        return {"total": 0, "unknown": 0, "renamed": 0, "manifest_path": None}

    manifest_rows = []
    total_renamed = 0
    total_unknown = 0

    for folder in sorted(all_folders):
        items = scan_game_folder(folder, scan_mb, log)
        if not items:
            continue
        log(f"\n== {folder} ==")
        for it in items:
            flag = "" if it["found_metadata"] else "  [!] metadata no encontrada, usando respaldo"
            log(f"  {it['filename']}  ->  {it['new_filename']}{flag}")
            if not it["found_metadata"]:
                total_unknown += 1
            manifest_rows.append({
                "folder": folder,
                "old_filename": it["filename"],
                "new_filename": it["new_filename"],
                "category_detected": it["label"],
                "metadata_found": it["found_metadata"],
            })

        if apply:
            for it in items:
                old_path = it["path"]
                new_path = os.path.join(folder, it["new_filename"])
                # macOS guarda los nombres en NFD (descompuesto) mientras que
                # el texto extraido del SFO suele venir en NFC; comparar tal
                # cual haria pensar que "Ragnarök" (NFC) != "Ragnarök" (NFD)
                # aunque sean el mismo nombre visualmente.
                if unicodedata.normalize("NFC", old_path) == unicodedata.normalize("NFC", new_path):
                    continue
                if os.path.exists(new_path):
                    log(f"  [SKIP] ya existe {new_path}, no se sobreescribe.")
                    continue
                os.rename(old_path, new_path)
                total_renamed += 1

            if folders:
                base_title = finalize_filename(items[0]["base_title"])
                new_folder = os.path.join(os.path.dirname(folder.rstrip(os.sep)), base_title)
                if os.path.normcase(new_folder) != os.path.normcase(folder.rstrip(os.sep)):
                    if os.path.exists(new_folder):
                        log(f"  [SKIP] carpeta destino ya existe: {new_folder}")
                    else:
                        os.rename(folder, new_folder)
                        log(f"  Carpeta renombrada -> {new_folder}")

    log(f"\nTotal pkgs analizados: {len(manifest_rows)}")
    log(f"Sin metadata legible (se uso respaldo por numero 1/2/3): {total_unknown}")

    manifest_path = None
    if apply:
        log(f"Renombrados: {total_renamed}")
        # 'root' puede haber sido renombrado (si folders=True y root era la
        # propia carpeta del juego), asi que se cae a su carpeta padre.
        manifest_dir = root if os.path.isdir(root) else os.path.dirname(root.rstrip(os.sep))
        if not os.path.isdir(manifest_dir):
            manifest_dir = os.getcwd()
        manifest_path = os.path.join(
            manifest_dir, f"rename_manifest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        )
        with open(manifest_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "folder", "old_filename", "new_filename", "category_detected", "metadata_found"
            ])
            writer.writeheader()
            writer.writerows(manifest_rows)
        log(f"Manifest guardado en: {manifest_path}")
    else:
        log("\n(Simulacion. Nada se ha modificado.)")

    return {
        "total": len(manifest_rows),
        "unknown": total_unknown,
        "renamed": total_renamed,
        "manifest_path": manifest_path,
    }


def push_to_end(root: str, prefix: str, apply: bool, log):
    """Antepone `prefix` a cada .pkg bajo `root` (recursivo) para que ordene
    despues de todo lo demas en un listado plano. No toca el resto del
    nombre. Devuelve un dict resumen."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        log(f"No existe la carpeta: {root}")
        return {"total": 0, "already": 0, "renamed": 0}

    renamed = 0
    already = 0
    total = 0

    for dirpath, dirnames, filenames in os.walk(root):
        for fname in sorted(filenames):
            if not fname.lower().endswith(".pkg"):
                continue
            total += 1
            if fname.startswith(prefix):
                already += 1
                continue
            old_path = os.path.join(dirpath, fname)
            new_path = os.path.join(dirpath, prefix + fname)
            log(f"  {fname}  ->  {prefix}{fname}")
            if apply:
                if unicodedata.normalize("NFC", old_path) == unicodedata.normalize("NFC", new_path):
                    continue
                if os.path.exists(new_path):
                    log(f"  [SKIP] ya existe {new_path}")
                    continue
                os.rename(old_path, new_path)
                renamed += 1

    log(f"\nTotal .pkg encontrados: {total}")
    log(f"Ya tenian el prefijo: {already}")
    if apply:
        log(f"Renombrados: {renamed}")
    else:
        log("\n(Simulacion. Nada se ha modificado.)")

    return {"total": total, "already": already, "renamed": renamed}


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class PS4PkgRenamerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("720x560")
        self.minsize(620, 460)

        notebook = ttk.Notebook(self)
        notebook.pack(fill="both", expand=True, padx=10, pady=10)

        self.games_tab = ttk.Frame(notebook)
        self.themes_tab = ttk.Frame(notebook)
        notebook.add(self.games_tab, text="Rename Games")
        notebook.add(self.themes_tab, text="Push to End")

        self._build_games_tab()
        self._build_themes_tab()

    # --- Rename Games tab ---
    def _build_games_tab(self):
        frame = self.games_tab

        path_frame = ttk.Frame(frame)
        path_frame.pack(fill="x", padx=10, pady=10)
        ttk.Label(path_frame, text="Target folder (Games / Homebrew / Emulators):").pack(anchor="w")

        picker = ttk.Frame(path_frame)
        picker.pack(fill="x", pady=(5, 0))
        self.games_path_var = tk.StringVar()
        ttk.Entry(picker, textvariable=self.games_path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(picker, text="Browse...", command=lambda: self._browse_folder(self.games_path_var)).pack(
            side="left", padx=(5, 0)
        )

        options = ttk.Frame(frame)
        options.pack(fill="x", padx=10, pady=(0, 10))
        self.folders_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(options, text="Also rename folders", variable=self.folders_var).pack(side="left")

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(buttons, text="Preview", command=self._preview_games).pack(side="left")
        ttk.Button(buttons, text="Apply", command=self._apply_games).pack(side="left", padx=(5, 0))

        self.games_log = scrolledtext.ScrolledText(frame, state="disabled", wrap="word")
        self.games_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _preview_games(self):
        self._run_games(apply=False)

    def _apply_games(self):
        if not messagebox.askyesno(
            APP_TITLE,
            "This will rename .pkg files (and folders, if checked) on disk.\n\n"
            "A CSV manifest with the old/new names will be saved so you can "
            "revert manually if needed.\n\nContinue?",
        ):
            return
        self._run_games(apply=True)

    def _run_games(self, apply: bool):
        target_dir = self.games_path_var.get().strip()
        if not target_dir or not os.path.isdir(target_dir):
            messagebox.showerror(APP_TITLE, f"Path not found:\n{target_dir}")
            return

        self._clear_log(self.games_log)
        rename_games(
            target_dir,
            apply=apply,
            folders=self.folders_var.get(),
            scan_mb=DEFAULT_SCAN_MB,
            log=lambda msg: self._log(self.games_log, msg),
        )

    # --- Push to End tab ---
    def _build_themes_tab(self):
        frame = self.themes_tab

        path_frame = ttk.Frame(frame)
        path_frame.pack(fill="x", padx=10, pady=10)
        ttk.Label(path_frame, text="Target folder (e.g. Themes):").pack(anchor="w")

        picker = ttk.Frame(path_frame)
        picker.pack(fill="x", pady=(5, 0))
        self.themes_path_var = tk.StringVar()
        ttk.Entry(picker, textvariable=self.themes_path_var).pack(side="left", fill="x", expand=True)
        ttk.Button(picker, text="Browse...", command=lambda: self._browse_folder(self.themes_path_var)).pack(
            side="left", padx=(5, 0)
        )

        prefix_frame = ttk.Frame(frame)
        prefix_frame.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Label(prefix_frame, text="Prefix:").pack(side="left")
        self.prefix_var = tk.StringVar(value=DEFAULT_PREFIX)
        ttk.Entry(prefix_frame, textvariable=self.prefix_var, width=20).pack(side="left", padx=(5, 0))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", padx=10, pady=(0, 10))
        ttk.Button(buttons, text="Preview", command=self._preview_themes).pack(side="left")
        ttk.Button(buttons, text="Apply", command=self._apply_themes).pack(side="left", padx=(5, 0))

        self.themes_log = scrolledtext.ScrolledText(frame, state="disabled", wrap="word")
        self.themes_log.pack(fill="both", expand=True, padx=10, pady=(0, 10))

    def _preview_themes(self):
        self._run_themes(apply=False)

    def _apply_themes(self):
        if not messagebox.askyesno(
            APP_TITLE, "This will rename .pkg files on disk by adding the prefix.\n\nContinue?"
        ):
            return
        self._run_themes(apply=True)

    def _run_themes(self, apply: bool):
        target_dir = self.themes_path_var.get().strip()
        if not target_dir or not os.path.isdir(target_dir):
            messagebox.showerror(APP_TITLE, f"Path not found:\n{target_dir}")
            return
        prefix = self.prefix_var.get()
        if not prefix:
            messagebox.showerror(APP_TITLE, "Prefix cannot be empty.")
            return

        self._clear_log(self.themes_log)
        push_to_end(
            target_dir,
            prefix=prefix,
            apply=apply,
            log=lambda msg: self._log(self.themes_log, msg),
        )

    # --- shared helpers ---
    def _browse_folder(self, path_var):
        folder = filedialog.askdirectory(title="Select target folder")
        if folder:
            path_var.set(folder)

    def _clear_log(self, widget):
        widget.configure(state="normal")
        widget.delete("1.0", tk.END)
        widget.configure(state="disabled")

    def _log(self, widget, message):
        widget.configure(state="normal")
        widget.insert(tk.END, message + "\n")
        widget.configure(state="disabled")
        widget.see(tk.END)
        widget.update_idletasks()


def main():
    app = PS4PkgRenamerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
