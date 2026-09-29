import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import zipfile
import io
import json
import re


KNOWN_SUBFOLDERS = {"Calibration", "Correlation", "Report", "Tables", "Validation"}


def detect_folders(file_list, sens_name):
    """
    Détecte les dossiers à proposer à l'utilisateur, quelle que soit la profondeur
    à laquelle se trouvent les dossiers *_inputs / *_outputs dans l'arborescence.

    Exemples de chemins supportés :
      seed_1/RN_inputs/file.csv                  -> RN_inputs
      seed_1/HT_copy/RN_inputs/file.csv          -> HT_copy/RN_inputs
      seed_1/HT_copy/RN_outputs/Tables/file.csv  -> HT_copy/RN_outputs/Tables

    Règles :
    - Un seul dossier *_inputs (toute arborescence parente conservée).
    - Pour *_outputs : uniquement les sous-dossiers dont le nom est dans KNOWN_SUBFOLDERS.
    """
    inputs_folder = None
    outputs_subfolders = set()

    for fp in file_list:
        stripped = fp[len(sens_name) + 1:] if sens_name and fp.startswith(sens_name + "/") else fp
        parts = stripped.split("/")

        for idx, part in enumerate(parts):
            if part.endswith("_inputs"):
                folder_path = "/".join(parts[:idx + 1])
                inputs_folder = folder_path
                break
            elif part.endswith("_outputs"):
                if idx + 1 < len(parts) and parts[idx + 1] in KNOWN_SUBFOLDERS:
                    folder_path = "/".join(parts[:idx + 2])
                    outputs_subfolders.add(folder_path)
                break

    result = []
    if inputs_folder:
        result.append(inputs_folder)
    result.extend(sorted(outputs_subfolders))
    return result


def build_curl(method, url, headers, body=None):
    """Génère une commande cURL lisible depuis les paramètres d'une requête."""
    lines = [f"curl -X {method} \\"]
    lines.append(f'  "{url}" \\')
    for k, v in headers.items():
        if k.lower() == "authorization":
            v = v[:15] + "..." + v[-4:] if len(v) > 20 else v
        lines.append(f'  -H "{k}: {v}" \\')
    if body is not None:
        escaped = json.dumps(body, ensure_ascii=False).replace("'", "'\\''")
        lines.append(f"  -d '{escaped}' \\")
    lines.append("  --insecure")
    return "\n".join(lines)


def show_curl(parent, curl_cmd):
    """Ouvre une petite fenêtre affichant la commande cURL."""
    win = tk.Toplevel(parent)
    win.title("Commande cURL")
    win.geometry("700x280")
    win.resizable(True, True)

    ttk.Label(win, text="Commande cURL utilisée :", font=("", 10, "bold")).pack(
        anchor="w", padx=12, pady=(10, 4)
    )

    frame = ttk.Frame(win)
    frame.pack(fill="both", expand=True, padx=12, pady=(0, 6))

    sb_y = ttk.Scrollbar(frame, orient="vertical")
    sb_x = ttk.Scrollbar(frame, orient="horizontal")
    txt = tk.Text(
        frame,
        wrap="none",
        font=("Courier", 9),
        yscrollcommand=sb_y.set,
        xscrollcommand=sb_x.set,
        height=10
    )
    sb_y.config(command=txt.yview)
    sb_x.config(command=txt.xview)
    sb_y.pack(side="right", fill="y")
    sb_x.pack(side="bottom", fill="x")
    txt.pack(side="left", fill="both", expand=True)
    txt.insert("1.0", curl_cmd)
    txt.config(state="disabled")

    def copy_to_clipboard():
        win.clipboard_clear()
        win.clipboard_append(curl_cmd)
        copy_btn.config(text="Copié !")
        win.after(1500, lambda: copy_btn.config(text="Copier"))

    copy_btn = ttk.Button(win, text="Copier", command=copy_to_clipboard)
    copy_btn.pack(pady=(0, 10))


def parse_project_url(value: str):
    """
    Extrait automatiquement :
    - base_url : https://esg-xxx.milliman-mind.com
    - project_id : après /p/<projectId>
    """
    value = value.strip()
    m = re.match(r"^(https?://[^/]+)/p/([^/?#]+)", value)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def normalize_universe_for_files_api(raw_universe):
    """
    Convertit différentes valeurs possibles de l'API vers le format
    attendu par l'endpoint /zip/files/{universe}/{versionId}.
    """
    if not raw_universe:
        return "RW"

    value = str(raw_universe).strip()

    mapping = {
        "RealWorld": "RW",
        "RiskNeutral": "RN",
        "RW": "RW",
        "RN": "RN",
        "realworld": "RW",
        "riskneutral": "RN",
    }
    return mapping.get(value, value)


class APIDownloaderApp:
    def __init__(self, root):
        self.root = root
        self.root.title("ESG File Downloader")
        self.root.geometry("760x430")
        self.root.resizable(False, False)

        self.file_list = []
        self.sensitivities = []
        self.table_options = []
        self.table_metadata_by_name = {}
        self.selected_table_meta = None

        self._build_config_window()

    def _get_headers(self, content_type=None):
        h = {"Authorization": f"Bearer {self.token_var.get().strip()}"}
        if content_type:
            h["Content-Type"] = content_type
            h["accept"] = "application/octet-stream"
        else:
            h["accept"] = "application/json"
        return h

    def _get_project_info_url(self):
        base = self.base_url_var.get().rstrip("/")
        project_id = self.project_id_var.get().strip()
        return f"{base}/api/projects/{project_id}"

    def _get_operation_states_url(self):
        base = self.base_url_var.get().rstrip("/")
        project_id = self.project_id_var.get().strip()
        return f"{base}/api/projects/{project_id}/operations/tables"

    def _build_files_url(self, sensitivity_id=None):
        base = self.base_url_var.get().rstrip("/")
        project_id = self.project_id_var.get().strip()
        table_id = self.table_id_var.get().strip()
        universe = self.universe_var.get().strip()
        version_id = self.version_id_var.get().strip()

        url = (
            f"{base}/api/projects/{project_id}"
            f"/operations/tables/{table_id}"
            f"/zip/files/{universe}/{version_id}"
        )
        if sensitivity_id:
            url += f"?sensitivityId={sensitivity_id}"
        return url

    def _build_sensitivities_url(self):
        base = self.base_url_var.get().rstrip("/")
        project_id = self.project_id_var.get().strip()
        table_id = self.table_id_var.get().strip()
        return f"{base}/api/projects/{project_id}/tables/{table_id}/sensitivities"

    def _fetch_project_info(self):
        url = self._get_project_info_url()
        headers = self._get_headers()
        r = requests.get(url, headers=headers, timeout=30, verify=False)
        r.raise_for_status()
        return r.json()

    def _fetch_operation_states(self):
        url = self._get_operation_states_url()
        headers = self._get_headers()
        r = requests.get(url, headers=headers, timeout=30, verify=False)
        r.raise_for_status()
        return r.json()

    def _extract_table_metadata(self, op_states):
        by_table = {}

        for st in op_states:
            table_id = st.get("tableId")
            table_name = st.get("tableName")
            version_id = st.get("versionId")
            last_status = st.get("lastOperationStatus")
            sensitivity_id = st.get("sensitivityId")

            if not table_id or not table_name or not version_id:
                continue

            if last_status is not None and last_status != 2:
                continue

            is_sensitivity = sensitivity_id is not None

            raw_universe = (
                st.get("universe")
                or st.get("universeType")
                or st.get("projectionUniverse")
                or "RW"
            )
            universe = normalize_universe_for_files_api(raw_universe)

            candidate = {
                "tableId": table_id,
                "tableName": table_name,
                "versionId": version_id,
                "universe": universe,
                "rawUniverse": raw_universe,
                "isSensitivityRow": is_sensitivity,
                "source": st,
            }

            if table_id not in by_table:
                by_table[table_id] = candidate
            else:
                if by_table[table_id]["isSensitivityRow"] and not is_sensitivity:
                    by_table[table_id] = candidate

        tables = sorted(by_table.values(), key=lambda x: x["tableName"].lower())
        return tables

    def _build_config_window(self):
        self.config_frame = ttk.Frame(self.root, padding=15)
        self.config_frame.pack(fill="both", expand=True)

        ttk.Label(
            self.config_frame,
            text="ESG File Downloader",
            font=("", 14, "bold")
        ).grid(row=0, column=0, columnspan=4, pady=(0, 12))

        ttk.Label(self.config_frame, text="Project URL :").grid(row=1, column=0, sticky="w", pady=4)
        self.project_url_var = tk.StringVar(
            value="https://esg-caa.milliman-mind.com/p/609add27-24e1-47e6-b4dc-7a5763a45fa1/t"
        )
        ttk.Entry(self.config_frame, textvariable=self.project_url_var, width=72).grid(
            row=1, column=1, columnspan=3, sticky="ew", pady=4
        )

        ttk.Label(self.config_frame, text="Bearer Token :").grid(row=2, column=0, sticky="w", pady=4)
        self.token_var = tk.StringVar()
        self._token_entry = ttk.Entry(
            self.config_frame,
            textvariable=self.token_var,
            width=72,
            show="•"
        )
        self._token_entry.grid(row=2, column=1, columnspan=3, sticky="ew", pady=4)

        self.show_token = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            self.config_frame,
            text="Afficher le token",
            variable=self.show_token,
            command=lambda: self._token_entry.config(
                show="" if self.show_token.get() else "•"
            )
        ).grid(row=3, column=1, sticky="w")

        load_frame = ttk.Frame(self.config_frame)
        load_frame.grid(row=4, column=0, columnspan=4, sticky="ew", pady=(16, 8))

        self.load_project_btn = ttk.Button(
            load_frame,
            text="Charger le projet et les tables",
            command=self._load_project_and_tables
        )
        self.load_project_btn.pack(side="left")

        self.load_status_lbl = ttk.Label(load_frame, text="")
        self.load_status_lbl.pack(side="left", padx=10)

        ttk.Label(self.config_frame, text="Table :").grid(row=5, column=0, sticky="w", pady=6)
        self.table_choice_var = tk.StringVar()
        self.table_combo = ttk.Combobox(
            self.config_frame,
            textvariable=self.table_choice_var,
            state="disabled",
            width=65
        )
        self.table_combo.grid(row=5, column=1, columnspan=3, sticky="ew", pady=6)
        self.table_combo.bind("<<ComboboxSelected>>", self._on_table_selected)

        info_frame = ttk.LabelFrame(self.config_frame, text="Métadonnées détectées", padding=10)
        info_frame.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(10, 8))

        ttk.Label(info_frame, text="Base URL :").grid(row=0, column=0, sticky="w", pady=2)
        self.base_url_display_var = tk.StringVar(value="-")
        ttk.Label(info_frame, textvariable=self.base_url_display_var).grid(row=0, column=1, sticky="w", pady=2)

        ttk.Label(info_frame, text="Project ID :").grid(row=1, column=0, sticky="w", pady=2)
        self.project_id_display_var = tk.StringVar(value="-")
        ttk.Label(info_frame, textvariable=self.project_id_display_var).grid(row=1, column=1, sticky="w", pady=2)

        ttk.Label(info_frame, text="Table ID :").grid(row=2, column=0, sticky="w", pady=2)
        self.table_id_display_var = tk.StringVar(value="-")
        ttk.Label(info_frame, textvariable=self.table_id_display_var).grid(row=2, column=1, sticky="w", pady=2)

        ttk.Label(info_frame, text="Version ID :").grid(row=0, column=2, sticky="w", padx=(20, 0), pady=2)
        self.version_id_display_var = tk.StringVar(value="-")
        ttk.Label(info_frame, textvariable=self.version_id_display_var).grid(row=0, column=3, sticky="w", pady=2)

        ttk.Label(info_frame, text="Universe :").grid(row=1, column=2, sticky="w", padx=(20, 0), pady=2)
        self.universe_display_var = tk.StringVar(value="-")
        ttk.Label(info_frame, textvariable=self.universe_display_var).grid(row=1, column=3, sticky="w", pady=2)

        ttk.Label(info_frame, text="Project name :").grid(row=2, column=2, sticky="w", padx=(20, 0), pady=2)
        self.project_name_display_var = tk.StringVar(value="-")
        ttk.Label(info_frame, textvariable=self.project_name_display_var).grid(row=2, column=3, sticky="w", pady=2)

        btn_frame = ttk.Frame(self.config_frame)
        btn_frame.grid(row=7, column=0, columnspan=4, pady=(18, 0))

        self.single_btn = ttk.Button(
            btn_frame,
            text="Téléchargement simple",
            command=self._open_single_window,
            width=28,
            state="disabled"
        )
        self.single_btn.pack(side="left", padx=8)

        self.batch_btn = ttk.Button(
            btn_frame,
            text="Téléchargements multiples",
            command=self._open_batch_window,
            width=28,
            state="disabled"
        )
        self.batch_btn.pack(side="left", padx=8)

        self.config_frame.columnconfigure(1, weight=1)
        self.config_frame.columnconfigure(3, weight=1)

        self.base_url_var = tk.StringVar(value="")
        self.project_id_var = tk.StringVar(value="")
        self.table_id_var = tk.StringVar(value="")
        self.universe_var = tk.StringVar(value="")
        self.version_id_var = tk.StringVar(value="")
        self.project_name_var = tk.StringVar(value="")

    def _validate_basic_config(self):
        if not self.token_var.get().strip():
            messagebox.showerror("Erreur", "Veuillez renseigner un Bearer Token.")
            return False

        if not self.project_url_var.get().strip():
            messagebox.showerror("Erreur", "Veuillez renseigner une URL projet complète.")
            return False

        return True

    def _validate_table_selected(self):
        if not self._validate_basic_config():
            return False

        if not self.project_id_var.get().strip():
            messagebox.showerror("Erreur", "Veuillez d'abord charger le projet.")
            return False

        if not self.table_id_var.get().strip():
            messagebox.showerror("Erreur", "Veuillez sélectionner une table.")
            return False

        if not self.version_id_var.get().strip():
            messagebox.showerror("Erreur", "Version ID introuvable pour la table sélectionnée.")
            return False

        if not self.universe_var.get().strip():
            messagebox.showerror("Erreur", "Universe introuvable pour la table sélectionnée.")
            return False

        return True

    def _load_project_and_tables(self):
        if not self._validate_basic_config():
            return

        project_url = self.project_url_var.get().strip()
        base_url, project_id = parse_project_url(project_url)
        if not base_url or not project_id:
            messagebox.showerror(
                "Erreur",
                "Impossible d'extraire le base URL et le project ID depuis l'URL fournie."
            )
            return

        self.load_project_btn.config(state="disabled")
        self.table_combo.config(state="disabled")
        self.single_btn.config(state="disabled")
        self.batch_btn.config(state="disabled")
        self.load_status_lbl.config(text="Chargement du projet et des tables...")

        def run():
            try:
                self.base_url_var.set(base_url)
                self.project_id_var.set(project_id)

                project_info = self._fetch_project_info()
                op_states = self._fetch_operation_states()
                tables = self._extract_table_metadata(op_states)

                if not tables:
                    raise RuntimeError("Aucune table exploitable trouvée via l'API.")

                self.table_options = tables
                self.table_metadata_by_name = {}

                display_values = []
                for t in tables:
                    label = f"{t['tableName']}  |  {t['tableId']}"
                    display_values.append(label)
                    self.table_metadata_by_name[label] = t

                project_name = project_info.get("name", "")
                self.project_name_var.set(project_name)

                def update_ui():
                    self.base_url_display_var.set(base_url)
                    self.project_id_display_var.set(project_id)
                    self.project_name_display_var.set(project_name or "-")

                    self.table_combo["values"] = display_values
                    self.table_combo.config(state="readonly")

                    if display_values:
                        self.table_choice_var.set(display_values[0])
                        self._apply_selected_table(display_values[0])

                    self.load_status_lbl.config(text=f"{len(display_values)} table(s) chargée(s)")
                    self.single_btn.config(state="normal")
                    self.batch_btn.config(state="normal")

                self.root.after(0, update_ui)

            except requests.exceptions.RequestException as e:
                self.root.after(0, lambda: messagebox.showerror("Erreur API", str(e)))
                self.root.after(0, lambda: self.load_status_lbl.config(text="Erreur API"))
            except Exception as e:
                self.root.after(0, lambda: messagebox.showerror("Erreur", str(e)))
                self.root.after(0, lambda: self.load_status_lbl.config(text="Erreur"))
            finally:
                self.root.after(0, lambda: self.load_project_btn.config(state="normal"))

        threading.Thread(target=run, daemon=True).start()

    def _apply_selected_table(self, label):
        meta = self.table_metadata_by_name.get(label)
        if not meta:
            return

        self.selected_table_meta = meta
        self.table_id_var.set(meta["tableId"])
        self.version_id_var.set(meta["versionId"])
        self.universe_var.set(meta["universe"])

        self.table_id_display_var.set(meta["tableId"])
        self.version_id_display_var.set(meta["versionId"])
        self.universe_display_var.set(meta["universe"])

    def _on_table_selected(self, event=None):
        label = self.table_choice_var.get()
        self._apply_selected_table(label)

    def _selected_folders(self, check_vars):
        return [f for f, v in check_vars.items() if v.get()]

    def _files_for_folders(self, all_files, folders, sens_name):
        result = []
        for fp in all_files:
            stripped = fp[len(sens_name) + 1:] if sens_name and fp.startswith(sens_name + "/") else fp
            for folder in folders:
                if stripped == folder or stripped.startswith(folder + "/"):
                    result.append(fp)
                    break
        return result

    def _extract_zip_flat(self, zip_bytes, dest_folder, sens_name):
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            for member in zf.infolist():
                if member.filename.endswith("/"):
                    continue
                basename = os.path.basename(member.filename)
                if not basename:
                    continue
                out_path = os.path.join(dest_folder, f"{sens_name}_{basename}")
                with zf.open(member) as src, open(out_path, "wb") as dst:
                    dst.write(src.read())

    def _build_folder_checklist(self, parent, folders):
        check_vars = {}
        cols = 2
        for i, folder in enumerate(folders):
            var = tk.BooleanVar(value=False)
            check_vars[folder] = var
            ttk.Checkbutton(parent, text=folder, variable=var).grid(
                row=i // cols, column=i % cols, sticky="w", padx=8, pady=2)
        return check_vars

    def _open_single_window(self):
        if not self._validate_table_selected():
            return

        win = tk.Toplevel(self.root)
        win.title("Téléchargement simple")
        win.geometry("680x460")

        self._single_folder_vars = {}
        self._single_all_files = []
        self._single_sens_name = ""
        self._single_last_get_curl = ""
        self._single_last_post_curl = ""

        top = ttk.Frame(win, padding=(10, 8))
        top.pack(fill="x")
        ttk.Label(top, text="Sensitivity ID (optionnel):").pack(side="left")
        self.sensitivity_id_var = tk.StringVar(value="")
        ttk.Entry(top, textvariable=self.sensitivity_id_var, width=34).pack(side="left", padx=6)
        single_fetch_btn = ttk.Button(top, text="Détecter les dossiers")
        single_fetch_btn.pack(side="left", padx=4)
        single_status_lbl = ttk.Label(top, text="")
        single_status_lbl.pack(side="left", padx=6)

        curl_get_btn = ttk.Button(
            top,
            text="cURL",
            width=7,
            state="disabled",
            command=lambda: show_curl(win, self._single_last_get_curl)
        )
        curl_get_btn.pack(side="right", padx=4)
        ttk.Label(top, text="liste :").pack(side="right")

        folder_frame = ttk.LabelFrame(win, text="Dossiers à télécharger", padding=10)
        folder_frame.pack(fill="x", padx=10, pady=6)
        ttk.Label(
            folder_frame,
            text="Cliquez sur 'Détecter les dossiers' pour charger la liste.",
            foreground="gray"
        ).pack(anchor="w")

        opt_frame = ttk.Frame(win, padding=(10, 2))
        opt_frame.pack(fill="x")
        self.single_extract_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opt_frame,
            text="Extraire les fichiers directement (préfixés par le nom de la sensibilité)",
            variable=self.single_extract_var
        ).pack(anchor="w")

        bot = ttk.Frame(win, padding=(10, 6))
        bot.pack(fill="x")
        status_lbl = ttk.Label(bot, text="")
        status_lbl.pack(side="left")

        curl_post_btn = ttk.Button(
            bot,
            text="cURL",
            width=7,
            state="disabled",
            command=lambda: show_curl(win, self._single_last_post_curl)
        )
        ttk.Label(bot, text="download :").pack(side="right")
        curl_post_btn.pack(side="right", padx=4)

        progress = ttk.Progressbar(win, mode="indeterminate")
        progress.pack(fill="x", padx=10, pady=(0, 8))

        download_btn = ttk.Button(
            bot,
            text="Télécharger",
            command=lambda: self._single_download(
                win, download_btn, curl_post_btn, status_lbl, progress
            )
        )
        download_btn.pack(side="right", padx=8)

        def on_single_fetch():
            single_fetch_btn.config(state="disabled")
            curl_get_btn.config(state="disabled")
            single_status_lbl.config(text="Chargement...")

            def fetch():
                try:
                    sid = self.sensitivity_id_var.get().strip()
                    url = self._build_files_url(sensitivity_id=sid if sid else None)
                    headers = self._get_headers()
                    self._single_last_get_curl = build_curl("GET", url, headers)

                    r = requests.get(url, headers=headers, timeout=30, verify=False)
                    r.raise_for_status()
                    all_files = r.json()
                    sens_name = sid if sid else ""
                    folders = detect_folders(all_files, sens_name)
                    self._single_all_files = all_files
                    self._single_sens_name = sens_name

                    def update_ui():
                        for w in folder_frame.winfo_children():
                            w.destroy()
                        if folders:
                            self._single_folder_vars = self._build_folder_checklist(folder_frame, folders)
                            single_status_lbl.config(text=f"{len(folders)} dossier(s) détecté(s)")
                        else:
                            ttk.Label(folder_frame, text="Aucun dossier détecté.", foreground="gray").pack(anchor="w")
                            single_status_lbl.config(text="Aucun dossier trouvé")
                        curl_get_btn.config(state="normal")

                    self.root.after(0, update_ui)
                except requests.exceptions.RequestException as e:
                    self.root.after(0, lambda: messagebox.showerror("Erreur API", str(e)))
                    self.root.after(0, lambda: single_status_lbl.config(text="Erreur"))
                finally:
                    self.root.after(0, lambda: single_fetch_btn.config(state="normal"))

            threading.Thread(target=fetch, daemon=True).start()

        single_fetch_btn.config(command=on_single_fetch)

    def _single_download(self, win, download_btn, curl_post_btn, status_lbl, progress):
        selected_folders = self._selected_folders(self._single_folder_vars)
        if not selected_folders:
            messagebox.showwarning("Attention", "Veuillez sélectionner au moins un dossier.")
            return

        extract = self.single_extract_var.get()
        sens_name = self._single_sens_name

        if extract:
            dest_folder = filedialog.askdirectory(title="Choisir le dossier de destination")
            if not dest_folder:
                return
            save_path = None
        else:
            save_path = filedialog.asksaveasfilename(
                defaultextension=".zip",
                filetypes=[("ZIP files", "*.zip"), ("All files", "*.*")],
                title="Enregistrer le fichier ZIP"
            )
            if not save_path:
                return
            dest_folder = None

        download_btn.config(state="disabled")
        curl_post_btn.config(state="disabled")
        status_lbl.config(text="Téléchargement en cours...")
        progress.start()

        all_files = self._single_all_files

        def run():
            try:
                selected_files = self._files_for_folders(all_files, selected_folders, sens_name)
                if not selected_files:
                    self.root.after(0, lambda: messagebox.showwarning(
                        "Attention", "Aucun fichier trouvé pour les dossiers sélectionnés."
                    ))
                    return

                sid = self.sensitivity_id_var.get().strip()
                url = self._build_files_url(sensitivity_id=sid if sid else None)
                headers = self._get_headers(content_type="application/json")
                body = {"filePaths": selected_files}
                self._single_last_post_curl = build_curl("POST", url, headers, body)

                self.root.after(0, lambda: curl_post_btn.config(state="normal"))

                r = requests.post(url, headers=headers, json=body, timeout=300, stream=True, verify=False)
                r.raise_for_status()
                zip_bytes = b"".join(r.iter_content(chunk_size=8192))

                if extract:
                    self._extract_zip_flat(zip_bytes, dest_folder, sens_name or "download")
                    self.root.after(0, lambda: messagebox.showinfo(
                        "Succès", f"Fichiers extraits dans :\n{dest_folder}"
                    ))
                else:
                    with open(save_path, "wb") as f:
                        f.write(zip_bytes)
                    self.root.after(0, lambda: messagebox.showinfo(
                        "Succès", f"Fichier sauvegardé :\n{save_path}"
                    ))

                self.root.after(0, lambda: status_lbl.config(text="Téléchargement terminé"))
            except requests.exceptions.RequestException as e:
                self.root.after(0, lambda: messagebox.showerror("Erreur", str(e)))
                self.root.after(0, lambda: status_lbl.config(text="Erreur"))
            finally:
                self.root.after(0, lambda: download_btn.config(state="normal"))
                self.root.after(0, progress.stop)

        threading.Thread(target=run, daemon=True).start()

    def _open_batch_window(self):
        if not self._validate_table_selected():
            return

        win = tk.Toplevel(self.root)
        win.title("Téléchargements multiples")
        win.geometry("720x580")

        self._batch_folder_vars = {}
        self._batch_last_sens_curl = ""
        self._batch_last_files_curl = ""
        self._batch_last_post_curl = ""
        self.batch_workers_var = tk.IntVar(value=4)

        step1 = ttk.LabelFrame(win, text="Étape 1 – Récupérer les sensibilités & détecter les dossiers", padding=10)
        step1.pack(fill="x", padx=10, pady=6)

        row1 = ttk.Frame(step1)
        row1.pack(fill="x")
        batch_fetch_btn = ttk.Button(row1, text="Récupérer sensibilités & dossiers")
        batch_fetch_btn.pack(side="left")
        batch_status_lbl = ttk.Label(row1, text="")
        batch_status_lbl.pack(side="left", padx=8)

        curl_sens_btn = ttk.Button(
            row1,
            text="cURL",
            width=7,
            state="disabled",
            command=lambda: show_curl(win, self._batch_last_sens_curl)
        )
        ttk.Label(row1, text="sensibilités :").pack(side="right")
        curl_sens_btn.pack(side="right", padx=2)

        sens_info_lbl = ttk.Label(step1, text="", foreground="gray")
        sens_info_lbl.pack(anchor="w", pady=(4, 0))

        step2 = ttk.LabelFrame(win, text="Étape 2 – Sélectionner les dossiers à télécharger", padding=10)
        step2.pack(fill="x", padx=10, pady=6)

        step2_hdr = ttk.Frame(step2)
        step2_hdr.pack(fill="x", pady=(0, 4))
        curl_files_btn = ttk.Button(
            step2_hdr,
            text="cURL",
            width=7,
            state="disabled",
            command=lambda: show_curl(win, self._batch_last_files_curl)
        )
        ttk.Label(step2_hdr, text="liste fichiers :").pack(side="right")
        curl_files_btn.pack(side="right", padx=2)

        self._step2_placeholder = ttk.Label(
            step2,
            text="Les dossiers seront détectés après l'étape 1.",
            foreground="gray"
        )
        self._step2_placeholder.pack(anchor="w")

        step2_check_frame = ttk.Frame(step2)
        step2_check_frame.pack(fill="x")

        step3 = ttk.LabelFrame(win, text="Étape 3 – Options & destination", padding=10)
        step3.pack(fill="x", padx=10, pady=6)

        dest_row = ttk.Frame(step3)
        dest_row.pack(fill="x")
        ttk.Label(dest_row, text="Dossier de destination:").pack(side="left")
        self.batch_dest_var = tk.StringVar(value=os.path.expanduser("~"))
        ttk.Entry(dest_row, textvariable=self.batch_dest_var, width=44).pack(side="left", padx=5)
        ttk.Button(dest_row, text="Parcourir...", command=self._browse_dest).pack(side="left")

        self.batch_extract_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            step3,
            text="Extraire les fichiers directement (préfixés par le nom de la sensibilité)",
            variable=self.batch_extract_var
        ).pack(anchor="w", pady=(6, 0))

        concurrency_row = ttk.Frame(step3)
        concurrency_row.pack(fill="x", pady=(6, 0))
        ttk.Label(concurrency_row, text="Téléchargements parallèles :").pack(side="left")
        workers_lbl = ttk.Label(concurrency_row, text="4", width=3)
        workers_lbl.pack(side="right")
        ttk.Scale(
            concurrency_row,
            from_=1,
            to=10,
            orient="horizontal",
            variable=self.batch_workers_var,
            length=160,
            command=lambda v: workers_lbl.config(text=str(int(float(v))))
        ).pack(side="right", padx=6)

        step4 = ttk.LabelFrame(win, text="Étape 4 – Lancement", padding=10)
        step4.pack(fill="x", padx=10, pady=6)

        launch_row = ttk.Frame(step4)
        launch_row.pack(fill="x")
        curl_post_btn = ttk.Button(
            launch_row,
            text="cURL",
            width=7,
            state="disabled",
            command=lambda: show_curl(win, self._batch_last_post_curl)
        )
        ttk.Label(launch_row, text="dernier POST :").pack(side="right")
        curl_post_btn.pack(side="right", padx=2)
        batch_dl_btn = ttk.Button(launch_row, text="Lancer le batch download", state="disabled")
        batch_dl_btn.pack(side="right", padx=8)

        batch_progress = ttk.Progressbar(win, mode="determinate")
        batch_progress.pack(fill="x", padx=10, pady=(4, 0))
        batch_progress_lbl = ttk.Label(win, text="")
        batch_progress_lbl.pack(pady=(2, 6))

        batch_fetch_btn.config(command=lambda: self._batch_fetch(
            win, batch_fetch_btn, batch_status_lbl, sens_info_lbl,
            step2_check_frame, batch_dl_btn, curl_sens_btn, curl_files_btn
        ))
        batch_dl_btn.config(command=lambda: self._batch_download(
            win, batch_fetch_btn, batch_dl_btn, curl_post_btn,
            batch_progress, batch_progress_lbl
        ))

    def _batch_fetch(self, win, fetch_btn, status_lbl, sens_info_lbl,
                     folder_frame, dl_btn, curl_sens_btn, curl_files_btn):
        fetch_btn.config(state="disabled")
        curl_sens_btn.config(state="disabled")
        curl_files_btn.config(state="disabled")
        status_lbl.config(text="Récupération des sensibilités...")
        dl_btn.config(state="disabled")

        def fetch():
            try:
                sens_url = self._build_sensitivities_url()
                sens_headers = self._get_headers()
                self._batch_last_sens_curl = build_curl("GET", sens_url, sens_headers)
                self.root.after(0, lambda: curl_sens_btn.config(state="normal"))

                r_sens = requests.get(sens_url, headers=sens_headers, timeout=30, verify=False)
                r_sens.raise_for_status()
                sensitivities = r_sens.json()

                if not sensitivities:
                    self.root.after(0, lambda: messagebox.showwarning(
                        "Attention", "Aucune sensibilité trouvée pour cette table."
                    ))
                    return

                self.sensitivities = sensitivities
                names = [s["sensitivityName"] for s in sensitivities]
                self.root.after(0, lambda: status_lbl.config(text="Détection des dossiers..."))

                first_id = sensitivities[0]["id"]
                first_name = sensitivities[0]["sensitivityName"]
                files_url = self._build_files_url(sensitivity_id=first_id)
                files_headers = self._get_headers()
                self._batch_last_files_curl = build_curl("GET", files_url, files_headers)
                self.root.after(0, lambda: curl_files_btn.config(state="normal"))

                r_files = requests.get(files_url, headers=files_headers, timeout=30, verify=False)
                r_files.raise_for_status()
                sample_files = r_files.json()
                folders = detect_folders(sample_files, first_name)

                def update_ui():
                    sens_info_lbl.config(
                        text=f"{len(sensitivities)} sensibilité(s) : {', '.join(names)}"
                    )
                    self._step2_placeholder.pack_forget()
                    for w in folder_frame.winfo_children():
                        w.destroy()
                    if folders:
                        self._batch_folder_vars = self._build_folder_checklist(folder_frame, folders)
                        status_lbl.config(
                            text=f"{len(sensitivities)} sensibilité(s), {len(folders)} dossier(s) détecté(s)"
                        )
                    else:
                        ttk.Label(folder_frame, text="Aucun dossier détecté.", foreground="gray").pack(anchor="w")
                        status_lbl.config(text="Aucun dossier détecté")
                    dl_btn.config(state="normal" if folders else "disabled")

                self.root.after(0, update_ui)

            except requests.exceptions.RequestException as e:
                self.root.after(0, lambda: messagebox.showerror("Erreur API", str(e)))
                self.root.after(0, lambda: status_lbl.config(text="Erreur"))
            finally:
                self.root.after(0, lambda: fetch_btn.config(state="normal"))

        threading.Thread(target=fetch, daemon=True).start()

    def _browse_dest(self):
        folder = filedialog.askdirectory(title="Choisir le dossier de destination")
        if folder:
            self.batch_dest_var.set(folder)

    def _batch_download(self, win, fetch_btn, dl_btn, curl_post_btn, progress, progress_lbl):
        selected_folders = self._selected_folders(self._batch_folder_vars)
        if not selected_folders:
            messagebox.showwarning("Attention", "Veuillez sélectionner au moins un dossier.")
            return
        dest_folder = self.batch_dest_var.get().strip()
        if not dest_folder or not os.path.isdir(dest_folder):
            messagebox.showerror("Erreur", "Le dossier de destination est invalide.")
            return
        if not self.sensitivities:
            messagebox.showerror("Erreur", "Veuillez d'abord récupérer les sensibilités (Étape 1).")
            return

        extract = self.batch_extract_var.get()
        max_workers = self.batch_workers_var.get()
        dl_btn.config(state="disabled")
        fetch_btn.config(state="disabled")
        curl_post_btn.config(state="disabled")
        total = len(self.sensitivities)
        progress["maximum"] = total
        progress["value"] = 0

        lock = threading.Lock()
        counters = {"downloaded": 0, "skipped": 0, "done": 0}

        def process_one(sens):
            sens_name = sens["sensitivityName"]
            sens_id = sens["id"]
            try:
                url = self._build_files_url(sensitivity_id=sens_id)
                r_files = requests.get(url, headers=self._get_headers(), timeout=30, verify=False)
                r_files.raise_for_status()
                all_files = r_files.json()

                files_to_dl = self._files_for_folders(all_files, selected_folders, sens_name)
                if not files_to_dl:
                    return "skipped", sens_name, None, None

                headers = self._get_headers(content_type="application/json")
                body = {"filePaths": files_to_dl}
                curl = build_curl("POST", url, headers, body)

                r = requests.post(url, headers=headers, json=body, timeout=300, stream=True, verify=False)
                if r.status_code >= 400:
                    return "skipped", sens_name, None, None

                zip_bytes = b"".join(r.iter_content(chunk_size=131072))
                return "ok", sens_name, zip_bytes, curl

            except requests.exceptions.RequestException:
                return "skipped", sens_name, None, None

        def download_all():
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(process_one, s): s for s in self.sensitivities}
                for future in as_completed(futures):
                    status, sens_name, zip_bytes, curl = future.result()
                    with lock:
                        counters["done"] += 1
                        done_now = counters["done"]
                        if status == "ok":
                            counters["downloaded"] += 1
                            if curl:
                                self._batch_last_post_curl = curl
                                self.root.after(0, lambda: curl_post_btn.config(state="normal"))
                            if extract:
                                self._extract_zip_flat(zip_bytes, dest_folder, sens_name)
                            else:
                                save_path = os.path.join(dest_folder, f"{sens_name}.zip")
                                with open(save_path, "wb") as f:
                                    f.write(zip_bytes)
                        else:
                            counters["skipped"] += 1

                    self.root.after(0, lambda v=done_now, n=sens_name: (
                        progress.config(value=v),
                        progress_lbl.config(text=f"{v}/{total} traité(s) - dernier : {n}")
                    ))

            dl = counters["downloaded"]
            skip = counters["skipped"]
            summary = (
                f"Batch terminé !\n\n"
                f"Téléchargés : {dl}\n"
                f"Ignorés (non disponibles) : {skip}"
            )
            self.root.after(0, lambda: messagebox.showinfo("Batch terminé", summary))
            self.root.after(0, lambda: progress_lbl.config(text=f"Terminé - {dl} téléchargé(s)."))
            self.root.after(0, lambda: dl_btn.config(state="normal"))
            self.root.after(0, lambda: fetch_btn.config(state="normal"))

        threading.Thread(target=download_all, daemon=True).start()


if __name__ == "__main__":
    root = tk.Tk()
    app = APIDownloaderApp(root)
    root.mainloop()
