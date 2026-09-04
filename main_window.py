import datetime
import json
import os
import platform
import subprocess
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, messagebox, scrolledtext, ttk

from core.downloader import Downloader
from utils.helpers import format_duration, get_environment_diagnostics, update_ytdlp


class Tooltip:
    def __init__(self, widget, text):
        self.widget = widget
        self.text = text
        self.window = None
        widget.bind('<Enter>', self.show)
        widget.bind('<Leave>', self.hide)

    def show(self, event=None):
        if self.window or not self.text:
            return
        x = self.widget.winfo_rootx() + 8
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.window = tk.Toplevel(self.widget)
        self.window.wm_overrideredirect(True)
        self.window.wm_geometry(f'+{x}+{y}')
        ttk.Label(self.window, text=self.text, relief='solid', borderwidth=1, padding=(5, 3)).pack()

    def hide(self, event=None):
        if self.window:
            self.window.destroy()
            self.window = None


class MainApp(tk.Tk):
    """
    Tkinter application that builds GUI, interacts with Downloader, and handles threading/cleanup.
    """

    def __init__(self):
        super().__init__()
        self.title("YT Audio DLer")
        self.geometry("1400x820")
        self.minsize(1180, 720)
        self.protocol("WM_DELETE_WINDOW", self.on_closing)

        self.infos = []
        self.downloader = None
        self.worker_thread = None
        self._session_restored = False
        self._session_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "session.json")
        self._playlist_history_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "playlist_history.json")
        self.output_var = tk.StringVar(value=os.path.expanduser("~"))
        self.playlist_history = []
        self._load_playlist_history()
        self.skip_existing_var = tk.BooleanVar(value=True)
        self.force_download_var = tk.BooleanVar(value=False)
        self._progress_info_var = tk.StringVar(value="Vitesse: -- | ETA: --")
        self._overall_progress_var = tk.StringVar(value="0 / 0 terminés")
        self._loaded_count_var = tk.StringVar(value="0 éléments chargés")
        self._selected_count_var = tk.StringVar(value="0 élément sélectionné")
        self._queue_count_var = tk.StringVar(value="File restante: 0 élément")
        self._summary_var = tk.StringVar(value="0 en ligne • 0 dans la file • 0 déjà présents")
        self._dependencies_ready = True
        self._setup_widgets()
        self._refresh_playlist_history()
        self.output_var.trace_add('write', lambda *_: self._refresh_destination_files())
        self._refresh_destination_files()

        self.downloader = Downloader(
            log_callback=self._threadsafe_log,
            progress_callback=self._threadsafe_progress,
            overall_progress_callback=self._threadsafe_overall_progress,
            finished_callback=self._threadsafe_worker_finished,
            queue_update_callback=self._threadsafe_queue_update,
            session_update_callback=self._threadsafe_session_save
        )

        self.after(100, self._startup_checks)
        self.after(200, self._load_previous_session)
        self.after(500, self._refresh_queue_view)
        self.after(30000, self._periodic_session_save)
        self.after(300, self.url_entry.focus_set)

    def _load_playlist_history(self):
        try:
            with open(self._playlist_history_path, 'r', encoding='utf-8') as history_file:
                history = json.load(history_file)
            self.playlist_history = history if isinstance(history, list) else []
        except (OSError, json.JSONDecodeError, TypeError):
            self.playlist_history = []

    def _save_playlist_history(self):
        temporary_path = self._playlist_history_path + '.tmp'
        try:
            with open(temporary_path, 'w', encoding='utf-8') as history_file:
                json.dump(self.playlist_history[:20], history_file, ensure_ascii=False, indent=2)
            os.replace(temporary_path, self._playlist_history_path)
        except (OSError, TypeError, ValueError) as error:
            self._append_log(f"Impossible de sauvegarder l’historique : {error}")
            try:
                if os.path.exists(temporary_path):
                    os.remove(temporary_path)
            except OSError:
                pass

    def _refresh_playlist_history(self):
        values = [f"{entry.get('title') or entry.get('url', 'URL inconnue')} ({entry.get('count', 0)})"
                  for entry in self.playlist_history]
        self.playlist_history_combo['values'] = values
        self.playlist_history_combo.set('')

    def _record_playlist_history(self, url, infos):
        if not infos:
            return
        title = next((info.get('playlist') or info.get('playlist_title') for info in infos if info), None)
        entry = {
            'url': url,
            'title': title or (infos[0].get('title') if len(infos) == 1 else 'Playlist sans titre'),
            'loaded_at': datetime.datetime.now().isoformat(timespec='seconds'),
            'count': len(infos),
        }
        self.playlist_history = [old for old in self.playlist_history if old.get('url') != url]
        self.playlist_history.insert(0, entry)
        self.playlist_history = self.playlist_history[:20]
        self._refresh_playlist_history()
        self._save_playlist_history()

    def _select_playlist_history(self, event=None):
        index = self.playlist_history_combo.current()
        if 0 <= index < len(self.playlist_history):
            self.url_var.set(self.playlist_history[index].get('url', ''))
            self.load_info_action()

    def _delete_selected_history(self):
        index = self.playlist_history_combo.current()
        if not 0 <= index < len(self.playlist_history):
            return
        removed = self.playlist_history.pop(index)
        self._refresh_playlist_history()
        self._save_playlist_history()
        self._append_log(f"Entrée supprimée de l’historique : {removed.get('title') or removed.get('url')}")

    def _on_existing_option_changed(self):
        if self.force_download_var.get():
            self.skip_existing_var.set(False)
        self._mark_loaded_files()
        self._populate_tree(log_message=False)

    def _mark_loaded_files(self):
        if self.downloader and self.infos:
            self.downloader.mark_existing(self.infos, self.output_var.get(), self.format_var.get(),
                                          self.skip_existing_var.get())

    def _setup_widgets(self):
        pad = 6
        self._warning_var = tk.StringVar(value="")
        self._warning_label = ttk.Label(self, textvariable=self._warning_var, foreground='#9a3412',
                                        wraplength=940, justify='left')
        top_frame = ttk.Frame(self)
        self._top_frame = top_frame
        top_frame.pack(fill='x', padx=pad, pady=(pad, 0))
        ttk.Label(top_frame, text="YT Audio DLer", font=('TkDefaultFont', 15, 'bold')).pack(side='left')
        ttk.Label(top_frame, text="Téléchargement audio YouTube", foreground='#666666').pack(side='left', padx=(10, 0))
        environment_btn = ttk.Button(top_frame, text="Environnement", command=self.show_environment)
        environment_btn.pack(side='right')
        Tooltip(environment_btn, "Afficher l’état de yt-dlp et FFmpeg")
        self.log_visible = True

        self._warning_label.pack(fill='x', padx=pad, pady=(4, 0), before=top_frame)

        main_pane = ttk.Panedwindow(self, orient='horizontal')
        main_pane.pack(fill='both', expand=True, padx=pad, pady=(pad, 0))
        source_panel = ttk.LabelFrame(main_pane, text="SOURCE", padding=6)
        process_panel = ttk.LabelFrame(main_pane, text="PROCESSUS", padding=6)
        destination_panel = ttk.LabelFrame(main_pane, text="DESTINATION", padding=6)
        main_pane.add(source_panel, weight=32)
        main_pane.add(process_panel, weight=40)
        main_pane.add(destination_panel, weight=28)

        source_url_frame = ttk.Frame(source_panel)
        source_url_frame.pack(fill='x', pady=(0, 6))
        url_label = ttk.Label(source_url_frame, text="URL YouTube")
        url_label.pack(anchor='w')
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(source_url_frame, textvariable=self.url_var)
        self.url_entry.pack(fill='x', pady=(2, 4))
        history_frame = ttk.Frame(source_url_frame)
        history_frame.pack(fill='x', pady=(0, 4))
        self.playlist_history_combo = ttk.Combobox(history_frame, state='readonly')
        self.playlist_history_combo.pack(side='left', fill='x', expand=True)
        self.playlist_history_combo.bind('<<ComboboxSelected>>', self._select_playlist_history)
        self.delete_history_btn = ttk.Button(history_frame, text="Supprimer", command=self._delete_selected_history)
        self.delete_history_btn.pack(side='left', padx=(4, 0))
        source_actions = ttk.Frame(source_url_frame)
        source_actions.pack(fill='x')
        self.load_info_btn = ttk.Button(source_actions, text="Charger les infos", command=self.load_info_action)
        self.load_info_btn.pack(side='left', padx=(4, 0))
        self.availability_btn = ttk.Button(source_actions, text="Vérifier disponibilité", command=self.verify_availability)
        self.availability_btn.pack(side='left', padx=(4, 0))
        self.hide_unavailable = False
        self.filter_button = ttk.Button(source_actions, text="Masquer indisponibles", command=self.toggle_unavailable_filter)
        self.filter_button.pack(side='left', padx=(4, 0))
        Tooltip(self.load_info_btn, "Charger les informations de la vidéo ou de la playlist")
        Tooltip(self.availability_btn, "Vérifier les titres sélectionnés, ou toute la liste")
        Tooltip(self.filter_button, "Masquer ou afficher les titres non disponibles")
        ttk.Label(self, textvariable=self._summary_var).pack(fill='x', padx=pad, pady=(4, 0))

        list_frame = ttk.Frame(source_panel)
        list_frame.pack(fill='both', expand=True)

        columns = ("index", "title", "duration", "uploader", "availability")
        self.tree = ttk.Treeview(list_frame, columns=columns, show='headings', selectmode='extended')
        self.tree.heading("index", text="#")
        self.tree.column("index", width=60, anchor='center')
        self.tree.heading("title", text="Titre")
        self.tree.column("title", width=520, anchor='w')
        self.tree.heading("duration", text="Durée")
        self.tree.column("duration", width=80, anchor='center')
        self.tree.heading("uploader", text="Diffuseur")
        self.tree.column("uploader", width=180, anchor='w')
        self.tree.heading("availability", text="Disponibilité")
        self.tree.column("availability", width=120, anchor='center')
        self.tree.tag_configure('availability_ok', foreground='#187a2f')
        self.tree.tag_configure('availability_restricted', foreground='#b26a00')
        self.tree.tag_configure('availability_unavailable', foreground='#b3261e')
        self.tree.tag_configure('already_present', foreground='#777777')
        self.tree.pack(side='left', fill='both', expand=True)
        self.tree.bind('<<TreeviewSelect>>', self._update_selection_count)
        self.tree.bind('<Control-a>', self.select_all)
        self.tree.bind('<Double-1>', self.open_selected_video)

        selection_controls = ttk.Frame(source_panel)
        selection_controls.pack(fill='x', pady=(6, 0))
        select_all_btn = ttk.Button(selection_controls, text="Tout sélectionner", command=self.select_all)
        select_all_btn.pack(side='left')
        deselect_all_btn = ttk.Button(selection_controls, text="Tout désélectionner", command=self.deselect_all)
        deselect_all_btn.pack(side='left', padx=(8, 0))
        ttk.Label(selection_controls, textvariable=self._selected_count_var).pack(side='left', padx=(12, 0))
        ttk.Label(selection_controls, textvariable=self._loaded_count_var).pack(side='right')
        self.add_queue_btn = ttk.Button(source_panel, text="Ajouter à la file d’attente",
                        command=self.add_selected_to_queue)
        self.add_queue_btn.pack(fill='x', pady=(6, 0))
        Tooltip(self.add_queue_btn, "Ajouter les éléments sélectionnés à la file sans démarrer le téléchargement")
        Tooltip(select_all_btn, "Sélectionner tous les titres affichés")
        Tooltip(deselect_all_btn, "Effacer la sélection")

        vsb = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        vsb.pack(side='left', fill='y')
        self.tree.configure(yscrollcommand=vsb.set)

        self.availability_progress_var = tk.StringVar(value="")
        self.availability_progress = ttk.Progressbar(source_panel, orient='horizontal', mode='determinate')
        self.availability_progress_label = ttk.Label(source_panel, textvariable=self.availability_progress_var)
        self.availability_progress_label.pack(fill='x', pady=(6, 0))
        self.availability_progress.pack(fill='x', pady=(0, 2))
        self.availability_progress_label.pack_forget()
        self.availability_progress.pack_forget()

        queue_frame = ttk.Frame(process_panel)
        queue_frame.pack(fill='both', expand=True)
        queue_columns = ("position", "title", "duration", "status", "availability", "wait")
        self.queue_tree = ttk.Treeview(queue_frame, columns=queue_columns, show='headings',
                           selectmode='extended')
        self.queue_tree.heading("position", text="Position")
        self.queue_tree.column("position", width=65, anchor='center')
        self.queue_tree.heading("title", text="Titre")
        self.queue_tree.column("title", width=430, anchor='w')
        self.queue_tree.heading("duration", text="Durée")
        self.queue_tree.column("duration", width=80, anchor='center')
        self.queue_tree.heading("status", text="Statut")
        self.queue_tree.column("status", width=100, anchor='center')
        self.queue_tree.heading("availability", text="Disponibilité")
        self.queue_tree.column("availability", width=110, anchor='center')
        self.queue_tree.heading("wait", text="Temps d’attente estimé")
        self.queue_tree.column("wait", width=150, anchor='center')
        self.queue_tree.tag_configure('already_present', foreground='#777777')
        self.queue_tree.pack(side='left', fill='both', expand=True)
        self.queue_tree.bind('<<TreeviewSelect>>', self._update_queue_button_states)
        self.queue_tree.bind('<Double-1>', self.listen_selected_queue_item)
        self.queue_tree.bind('<Delete>', self.remove_from_queue)
        queue_vsb = ttk.Scrollbar(queue_frame, orient="vertical", command=self.queue_tree.yview)
        queue_vsb.pack(side='left', fill='y')
        self.queue_tree.configure(yscrollcommand=queue_vsb.set)

        queue_controls = ttk.Frame(process_panel)
        queue_controls.pack(fill='x', pady=(6, 0))
        self.remove_queue_btn = ttk.Button(queue_controls, text="Supprimer de la file", command=self.remove_from_queue,
                           state='disabled')
        self.remove_queue_btn.pack(side='left')
        self.move_up_btn = ttk.Button(queue_controls, text="Monter", command=lambda: self.move_queue_item(-1), state='disabled')
        self.move_up_btn.pack(side='left', padx=(8, 0))
        self.move_down_btn = ttk.Button(queue_controls, text="Descendre", command=lambda: self.move_queue_item(1), state='disabled')
        self.move_down_btn.pack(side='left', padx=(8, 0))
        self.clear_queue_btn = ttk.Button(queue_controls, text="Vider la file restante", command=self.clear_remaining_queue,
                          state='disabled')
        self.clear_queue_btn.pack(side='left', padx=(8, 0))
        ttk.Label(queue_controls, textvariable=self._queue_count_var).pack(side='left', padx=(12, 0))
        save_session_btn = ttk.Button(queue_controls, text="Sauvegarder la session maintenant", command=self._save_session)
        save_session_btn.pack(side='right')
        Tooltip(self.remove_queue_btn, "Supprimer les éléments sélectionnés de la file")
        Tooltip(self.move_up_btn, "Déplacer l’élément sélectionné vers le haut")
        Tooltip(self.move_down_btn, "Déplacer l’élément sélectionné vers le bas")
        Tooltip(self.clear_queue_btn, "Vider tous les téléchargements restants")
        Tooltip(save_session_btn, "Sauvegarder la session actuelle")
        self.queue_estimate_var = tk.StringVar(value="Prochain téléchargement: -- | Temps total estimé restant: --")
        ttk.Label(process_panel, textvariable=self.queue_estimate_var, wraplength=360).pack(fill='x', pady=(4, 0))

        out_frame = ttk.Frame(destination_panel)
        out_frame.pack(fill='x', pady=(0, 6))
        out_label = ttk.Label(out_frame, text="Dossier de sortie")
        out_label.pack(anchor='w')
        self.output_entry = ttk.Entry(out_frame, textvariable=self.output_var, width=70)
        self.output_entry.pack(fill='x', pady=(2, 4))
        output_actions = ttk.Frame(out_frame)
        output_actions.pack(fill='x')
        browse_btn = ttk.Button(output_actions, text="Parcourir", command=self.browse_output)
        browse_btn.pack(side='left')
        Tooltip(browse_btn, "Choisir le dossier de sortie")
        open_output_btn = ttk.Button(output_actions, text="Ouvrir le dossier", command=self.open_output_folder)
        open_output_btn.pack(side='left', padx=(4, 0))
        Tooltip(open_output_btn, "Ouvrir le dossier de sortie dans l’explorateur")

        destination_list_frame = ttk.Frame(destination_panel)
        destination_list_frame.pack(fill='both', expand=True)
        destination_columns = ('filename', 'size', 'modified')
        self.destination_tree = ttk.Treeview(destination_list_frame, columns=destination_columns,
                                             show='headings', selectmode='extended')
        self.destination_tree.heading('filename', text='Nom du fichier')
        self.destination_tree.heading('size', text='Taille')
        self.destination_tree.heading('modified', text='Modifié le')
        self.destination_tree.column('filename', width=250, anchor='w')
        self.destination_tree.column('size', width=90, anchor='e')
        self.destination_tree.column('modified', width=140, anchor='center')
        self.destination_tree.tag_configure('already_present', foreground='#187a2f')
        self.destination_tree.pack(side='left', fill='both', expand=True)
        self.destination_tree.bind('<Double-1>', self.listen_selected_destination_file)
        destination_scrollbar = ttk.Scrollbar(destination_list_frame, orient='vertical',
                                               command=self.destination_tree.yview)
        destination_scrollbar.pack(side='left', fill='y')
        self.destination_tree.configure(yscrollcommand=destination_scrollbar.set)
        destination_controls = ttk.Frame(destination_panel)
        destination_controls.pack(fill='x', pady=(6, 0))
        self.destination_count_var = tk.StringVar(value='0 fichier présent')
        ttk.Label(destination_controls, textvariable=self.destination_count_var).pack(side='left')
        ttk.Button(destination_controls, text='Actualiser', command=self._refresh_destination_files).pack(side='right')
        ttk.Button(destination_controls, text='Écouter', command=self.listen_selected_destination_file).pack(
            side='right', padx=(0, 6))

        opt_frame = ttk.LabelFrame(process_panel, text="Réglages", padding=6)
        opt_frame.pack(fill='x', pady=(6, 0))
        fmt_label = ttk.Label(opt_frame, text="Format:")
        fmt_label.pack(side='left', padx=(0, 4))
        self.format_var = tk.StringVar(value="Best (original)")
        fmt_combo = ttk.Combobox(opt_frame, textvariable=self.format_var, state='readonly',
                                 values=["Best (original)", "MP3 320kbps", "MP3 256kbps", "M4A", "FLAC", "OPUS"])
        fmt_combo.pack(side='left')

        ttk.Checkbutton(opt_frame, text="Ignorer les fichiers déjà présents",
                variable=self.skip_existing_var,
                command=self._on_existing_option_changed).pack(side='left', padx=(12, 0))
        ttk.Checkbutton(opt_frame, text="Forcer le retéléchargement",
                variable=self.force_download_var,
                command=self._on_existing_option_changed).pack(side='left', padx=(8, 0))

        ttk.Label(opt_frame, text="Mode de vitesse:").pack(side='left', padx=(12, 4))
        self.speed_mode_var = tk.StringVar(value="Doux")
        speed_combo = ttk.Combobox(opt_frame, textvariable=self.speed_mode_var, state='readonly',
                       values=["Turbo", "Normal", "Doux", "Très doux", "Personnalisé"], width=16)
        speed_combo.pack(side='left')
        speed_combo.bind("<<ComboboxSelected>>", self._on_speed_mode_changed)
        self.speed_help_var = tk.StringVar(value="")
        ttk.Label(process_panel, textvariable=self.speed_help_var, wraplength=420,
              foreground='#555555', justify='left').pack(fill='x', pady=(4, 0))

        self.custom_speed_frame = ttk.Frame(process_panel)
        self.custom_min_var = tk.StringVar(value="60")
        self.custom_multiplier_var = tk.StringVar(value="1.2")
        self.custom_random_min_var = tk.StringVar(value="30")
        self.custom_random_max_var = tk.StringVar(value="180")
        ttk.Label(self.custom_speed_frame, text="Délai min (s):").pack(side='left')
        ttk.Entry(self.custom_speed_frame, textvariable=self.custom_min_var, width=8).pack(side='left', padx=(4, 10))
        ttk.Label(self.custom_speed_frame, text="Multiplicateur:").pack(side='left')
        ttk.Entry(self.custom_speed_frame, textvariable=self.custom_multiplier_var, width=8).pack(side='left', padx=(4, 10))
        ttk.Label(self.custom_speed_frame, text="Random min (s):").pack(side='left')
        ttk.Entry(self.custom_speed_frame, textvariable=self.custom_random_min_var, width=8).pack(side='left', padx=(4, 10))
        ttk.Label(self.custom_speed_frame, text="Random max (s):").pack(side='left')
        ttk.Entry(self.custom_speed_frame, textvariable=self.custom_random_max_var, width=8).pack(side='left', padx=(4, 0))
        self._on_speed_mode_changed()

        ctrl_frame = ttk.Frame(process_panel)
        ctrl_frame.pack(fill='x', pady=(6, 0))
        self.start_btn = ttk.Button(ctrl_frame, text="Démarrer", command=self.start_download)
        self.start_btn.pack(side='left')
        self.pause_btn = ttk.Button(ctrl_frame, text="Pause", command=self.toggle_pause, state='disabled')
        self.pause_btn.pack(side='left', padx=(8, 0))
        self.stop_btn = ttk.Button(ctrl_frame, text="Stop", command=self.stop_download, state='disabled')
        self.stop_btn.pack(side='left', padx=(8, 0))
        self.listen_btn = ttk.Button(ctrl_frame, text="Écouter", command=self.listen_selected_queue_item)
        self.listen_btn.pack(side='left', padx=(8, 0))

        prog_frame = ttk.LabelFrame(process_panel, text="Progression", padding=6)
        prog_frame.pack(fill='x', pady=(6, 0))
        ttk.Label(prog_frame, text="Actuelle:").pack(side='left')
        self.current_pb = ttk.Progressbar(prog_frame, orient='horizontal', length=400, mode='determinate')
        self.current_pb.pack(side='left', fill='x', expand=True, padx=(4, 8))
        ttk.Label(prog_frame, text="Globale:").pack(side='left')
        self.overall_pb = ttk.Progressbar(prog_frame, orient='horizontal', length=400, mode='determinate')
        self.overall_pb.pack(side='left', fill='x', expand=True, padx=(4, 8))
        progress_info_frame = ttk.Frame(process_panel)
        progress_info_frame.pack(fill='x', pady=(2, 0))
        ttk.Label(progress_info_frame, textvariable=self._progress_info_var).pack(side='left')
        ttk.Label(progress_info_frame, textvariable=self._overall_progress_var).pack(side='right')
        Tooltip(self.start_btn, "Démarrer les téléchargements présents dans la file")
        Tooltip(self.pause_btn, "Mettre en pause ou reprendre le worker")
        Tooltip(self.stop_btn, "Arrêter le worker et les téléchargements restants")
        Tooltip(self.listen_btn, "Ouvrir le fichier audio sélectionné avec le lecteur par défaut")

        log_frame = ttk.Frame(self)
        self._log_frame = log_frame
        log_frame.pack(fill='both', expand=True, padx=pad, pady=(pad, pad))
        lbl_row = ttk.Frame(log_frame)
        lbl_row.pack(fill='x')
        ttk.Label(lbl_row, text="Logs / statut").pack(side='left', anchor='w')
        clear_btn = ttk.Button(lbl_row, text="Effacer les logs", command=lambda: self.log_text.delete('1.0', 'end'))
        clear_btn.pack(side='right')
        self.log_toggle_btn = ttk.Button(lbl_row, text="Masquer les logs", command=self.toggle_log_status)
        self.log_toggle_btn.pack(side='right', padx=(0, 6))
        self.log_text = scrolledtext.ScrolledText(log_frame, height=12, wrap='word', state='disabled')
        self.log_text.pack(fill='both', expand=True)

    def toggle_log_status(self):
        self.log_visible = not self.log_visible
        if self.log_visible:
            self._log_frame.pack(fill='both', expand=True, padx=6, pady=(6, 6))
            self.log_toggle_btn.configure(text="Masquer les logs")
        else:
            self._log_frame.pack_forget()
            self.log_toggle_btn.configure(text="Afficher les logs")

    def _threadsafe_log(self, message):
        self.after(0, lambda: self._append_log(message))

    def _append_log(self, message):
        self.log_text.configure(state='normal')
        ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        self.log_text.insert("end", f"[{ts}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state='disabled')

    def _threadsafe_progress(self, percent, speed_str, eta_str, filename):
        self.after(0, lambda: self._update_current_progress(percent, speed_str, eta_str, filename))

    def _update_current_progress(self, percent, speed_str, eta_str, filename):
        self.current_pb['value'] = percent
        self._progress_info_var.set(f"Vitesse: {speed_str} | ETA: {eta_str}")

    def _threadsafe_overall_progress(self, completed, total):
        self.after(0, lambda: self._update_overall_progress(completed, total))

    def _update_overall_progress(self, completed, total):
        self._overall_progress_var.set(f"{completed} / {total} terminés")
        if total:
            pct = completed / total * 100.0
            self.overall_pb['value'] = pct
        else:
            self.overall_pb['value'] = 0

    def _threadsafe_worker_finished(self):
        self.after(0, self._worker_finished_ui)

    def _threadsafe_queue_update(self, snapshot):
        self.after(0, lambda: self._update_queue_view(snapshot))

    def _format_wait(self, seconds):
        seconds = int(max(0, seconds))
        if not seconds:
            return "--"
        minutes, remaining = divmod(seconds, 60)
        return f"{minutes}m {remaining:02d}s"

    def _update_queue_view(self, snapshot):
        selected = set(self.queue_tree.selection())
        for item_id in self.queue_tree.get_children():
            self.queue_tree.delete(item_id)
        for item in snapshot:
            info = item['info']
            title = info.get('title', 'unknown')
            duration = format_duration(info.get('duration'))
            wait = self._format_wait(item.get('wait_seconds', 0))
            iid = str(item['id'])
            self.queue_tree.insert("", "end", iid=iid,
                           values=(item.get('position', ''), title, duration, item['status'],
                               info.get('_availability', 'unknown'), wait),
                           tags=('already_present',) if item['status'] == 'Déjà présent' else ())
        for item_id in selected:
            if self.queue_tree.exists(item_id):
                self.queue_tree.selection_add(item_id)
        pending = [item for item in snapshot if item['status'] in ('Pending', 'Downloading', 'Waiting')]
        next_wait = pending[0].get('wait_seconds', 0) if pending else 0
        total_wait = max((item.get('wait_seconds', 0) for item in pending), default=0)
        self.queue_estimate_var.set(
            f"Prochain téléchargement: {self._format_wait(next_wait)} | "
            f"Temps total estimé restant: {self._format_wait(total_wait)}"
        )
        remaining_count = len(pending)
        self._queue_count_var.set(
            f"File restante: {remaining_count} élément" + ("s" if remaining_count != 1 else "")
        )
        self._refresh_summary(snapshot)
        if not pending:
            self.queue_estimate_var.set("File d’attente vide.")
        self._update_queue_button_states()

    def _refresh_summary(self, snapshot=None):
        if snapshot is None:
            snapshot = self.downloader.get_queue_snapshot() if self.downloader else []
        queue_count = sum(1 for item in snapshot
                          if item['status'] in ('Pending', 'Downloading', 'Waiting'))
        present_count = len(self.destination_tree.get_children()) if hasattr(self, 'destination_tree') else 0
        self._summary_var.set(f"{len(self.infos)} en ligne • {queue_count} dans la file • {present_count} déjà présents")

    def _update_selection_count(self, event=None):
        count = len(self.tree.selection())
        self._selected_count_var.set(f"{count} élément" + ("s" if count != 1 else "") + " sélectionné" + ("s" if count != 1 else ""))

    def select_all(self, event=None):
        self.tree.selection_set(self.tree.get_children())
        self._update_selection_count()
        return 'break'

    def deselect_all(self):
        self.tree.selection_remove(self.tree.selection())
        self._update_selection_count()

    def open_selected_video(self, event=None):
        row = self.tree.identify_row(event.y) if event else ''
        if not row:
            return
        info = self.infos[int(row)]
        url = info.get('webpage_url') or info.get('original_url') or info.get('url')
        if url:
            webbrowser.open(url)

    def _update_queue_button_states(self, event=None):
        if not hasattr(self, 'queue_tree'):
            return
        snapshot = self.downloader.get_queue_snapshot() if self.downloader else []
        queueable_statuses = {'Pending', 'Déjà présent'}
        pending = {item['id']: item for item in snapshot if item['status'] in queueable_statuses}
        selected = [int(item_id) for item_id in self.queue_tree.selection()]
        selected_pending = [item_id for item_id in selected if item_id in pending]
        self.remove_queue_btn.configure(state='normal' if selected_pending else 'disabled')
        can_move = len(selected_pending) == 1
        if can_move:
            pending_ids = [item['id'] for item in snapshot if item['status'] in queueable_statuses]
            pending_index = pending_ids.index(selected_pending[0])
            self.move_up_btn.configure(state='normal' if pending_index > 0 else 'disabled')
            self.move_down_btn.configure(state='normal' if pending_index < len(pending_ids) - 1 else 'disabled')
        else:
            self.move_up_btn.configure(state='disabled')
            self.move_down_btn.configure(state='disabled')
        self.clear_queue_btn.configure(state='normal' if self.downloader and self.downloader.has_pending_items() else 'disabled')

    def _on_speed_mode_changed(self, event=None):
        mode = self.speed_mode_var.get()
        explanations = {
            'Turbo': "Télécharge les éléments à la suite, sans délai volontaire entre deux titres.",
            'Normal': "Ajoute une courte pause aléatoire entre les téléchargements.",
            'Doux': "Ajoute une pause plus longue, ajustée à la durée du titre, pour limiter le rythme des requêtes.",
            'Très doux': "Utilise des pauses très longues, adaptées aux téléchargements espacés.",
            'Personnalisé': "Définissez le délai minimum, le multiplicateur de durée et la plage aléatoire ci-dessous.",
        }
        self.speed_help_var.set(explanations.get(mode, ""))
        if mode == "Personnalisé":
            self.custom_speed_frame.pack(fill='x', padx=6, pady=(2, 0))
        else:
            self.custom_speed_frame.pack_forget()
        if hasattr(self, 'log_text'):
            self._append_log(f"Speed mode changed: {mode}")

    def add_selected_to_queue(self):
        if not self._dependencies_ready:
            self.show_environment()
            return
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo("Aucune sélection", "Sélectionnez au moins un élément dans la source.")
            return
        selected_infos = [self.infos[int(item_id)] for item_id in selection]
        unavailable_statuses = {'private', 'deleted', 'login_required', 'age_restricted', 'error'}
        available_infos = []
        for info in selected_infos:
            if info.get('_availability') in unavailable_statuses:
                self._append_log(f"Élément indisponible ignoré : {info.get('title', 'inconnu')} ({info.get('_availability')})")
            else:
                available_infos.append(info)
        if not available_infos:
            messagebox.showinfo("Aucun élément disponible", "Aucun élément sélectionné n’est disponible.")
            return
        output_dir = self.output_var.get().strip()
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as error:
            messagebox.showerror("Dossier invalide", f"Impossible de créer le dossier de sortie : {error}")
            return
        fmt = self.format_var.get()
        self.downloader.mark_existing(available_infos, output_dir, fmt, self.skip_existing_var.get())
        self.downloader.enqueue(available_infos, output_dir, fmt, self.skip_existing_var.get())
        self._session_restored = False
        self._populate_tree(log_message=False)
        self._append_log(f"{len(available_infos)} élément(s) ajouté(s) à la file d’attente.")

    def _get_custom_settings(self):
        try:
            settings = {
                'minimum_delay': float(self.custom_min_var.get()),
                'duration_multiplier': float(self.custom_multiplier_var.get()),
                'random_min': float(self.custom_random_min_var.get()),
                'random_max': float(self.custom_random_max_var.get()),
            }
            if any(value < 0 for value in settings.values()):
                raise ValueError("values must be non-negative")
            return settings
        except ValueError as error:
            messagebox.showerror("Invalid speed settings", f"Please enter valid non-negative numbers: {error}")
            return None

    def _refresh_queue_view(self):
        if self.downloader:
            self._update_queue_view(self.downloader.get_queue_snapshot())
        try:
            self.after(500, self._refresh_queue_view)
        except tk.TclError:
            pass

    def _threadsafe_session_save(self):
        try:
            self.after(0, self._save_session)
        except tk.TclError:
            pass

    def _save_session(self):
        if not self.downloader:
            return False
        state = self.downloader.get_session_state(
            output_dir=self.output_var.get(),
            format_choice=self.format_var.get(),
            speed_mode=self.speed_mode_var.get(),
            custom_settings={
                'minimum_delay': self.custom_min_var.get(),
                'duration_multiplier': self.custom_multiplier_var.get(),
                'random_min': self.custom_random_min_var.get(),
                'random_max': self.custom_random_max_var.get(),
            }
        )
        state['saved_at'] = datetime.datetime.now().isoformat(timespec='seconds')
        state['geometry'] = self.geometry()
        state['skip_existing'] = self.skip_existing_var.get()
        state['force_download'] = self.force_download_var.get()
        temporary_path = self._session_path + ".tmp"
        try:
            with open(temporary_path, 'w', encoding='utf-8') as session_file:
                json.dump(state, session_file, ensure_ascii=False, indent=2)
            os.replace(temporary_path, self._session_path)
            self._append_log("Session saved.")
            return True
        except (OSError, TypeError, ValueError) as error:
            self._append_log(f"Session save failed: {error}")
            try:
                if os.path.exists(temporary_path):
                    os.remove(temporary_path)
            except OSError:
                pass
            return False

    def _periodic_session_save(self):
        if self.downloader and self.downloader.worker_thread and self.downloader.worker_thread.is_alive():
            self._save_session()
        try:
            self.after(30000, self._periodic_session_save)
        except tk.TclError:
            pass

    def _load_previous_session(self):
        if not os.path.isfile(self._session_path):
            return
        try:
            with open(self._session_path, 'r', encoding='utf-8') as session_file:
                state = json.load(session_file)
            if (not isinstance(state, dict) or state.get('version') != 1 or
                    not isinstance(state.get('queue'), list) or
                    not isinstance(state.get('completed'), list)):
                raise ValueError("unsupported session format")
        except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
            self._append_log(f"Session load failed: {error}")
            return

        saved_at = state.get('saved_at', 'date inconnue')
        resume = messagebox.askyesno(
            "Session précédente",
            f"Une session précédente a été trouvée ({saved_at}). Voulez-vous la reprendre ?"
        )
        if not resume:
            self._archive_session()
            return

        self.output_var.set(state.get('output_dir') or self.output_var.get())
        if state.get('geometry'):
            try:
                self.geometry(state['geometry'])
            except tk.TclError:
                pass
        self.format_var.set(state.get('format_choice') or self.format_var.get())
        self.skip_existing_var.set(state.get('skip_existing', True))
        self.force_download_var.set(state.get('force_download', False))
        if self.force_download_var.get():
            self.skip_existing_var.set(False)
        saved_mode = state.get('speed_mode')
        if saved_mode not in ("Turbo", "Normal", "Doux", "Très doux", "Personnalisé"):
            saved_mode = "Doux" if state.get('slow_mode', True) else "Turbo"
        self.speed_mode_var.set(saved_mode)
        saved_custom = state.get('custom_settings') or {}
        self.custom_min_var.set(str(saved_custom.get('minimum_delay', 60)))
        self.custom_multiplier_var.set(str(saved_custom.get('duration_multiplier', 1.2)))
        self.custom_random_min_var.set(str(saved_custom.get('random_min', 30)))
        self.custom_random_max_var.set(str(saved_custom.get('random_max', 180)))
        self._on_speed_mode_changed()
        self.downloader.set_speed_mode(saved_mode, saved_custom)
        try:
            self.downloader.restore_session(state['queue'], state['completed'])
        except (TypeError, ValueError, KeyError) as error:
            self._append_log(f"Session restore failed: {error}")
            return
        self.infos = [item['info'] for item in state['queue']]
        self._mark_loaded_files()
        self._session_restored = True
        self._populate_tree()
        self._append_log("Previous session resumed.")

    def _archive_session(self):
        archive_path = self._session_path + ".dismissed-" + datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        try:
            os.replace(self._session_path, archive_path)
            self._append_log(f"Previous session archived: {archive_path}")
        except OSError as error:
            self._append_log(f"Previous session archive failed: {error}")

    def remove_from_queue(self):
        selected = [int(item_id) for item_id in self.queue_tree.selection()]
        if not selected:
            return
        removed = self.downloader.remove_queue_items(selected)
        for item in removed:
            self._append_log(f"Removed from queue: {item['info'].get('title', 'unknown')}")

    def move_queue_item(self, offset):
        selection = self.queue_tree.selection()
        if len(selection) != 1:
            return
        item_id = int(selection[0])
        if self.downloader.move_queue_item(item_id, offset):
            item = next((item for item in self.downloader.get_queue_snapshot()
                         if item['id'] == item_id), None)
            if item:
                direction = "up" if offset < 0 else "down"
                self._append_log(f"Moved in queue ({direction}): {item['info'].get('title', 'unknown')}")

    def clear_remaining_queue(self):
        removed = self.downloader.clear_queue()
        if not removed:
            return
        self._append_log(f"Cleared {len(removed)} remaining item(s) from queue.")

    def _worker_finished_ui(self):
        self.start_btn.configure(state='normal')
        self.pause_btn.configure(state='disabled', text='Pause')
        self.stop_btn.configure(state='disabled')
        self._append_log("All downloads finished or worker stopped.")

    def load_info_action(self):
        if not self._dependencies_ready:
            self.show_environment()
            return
        url = self.url_var.get().strip()
        if not url:
            messagebox.showinfo("No URL", "Please enter a YouTube video or playlist URL.")
            return
        self._append_log(f"Loading info for: {url}")
        self.start_btn.configure(state='disabled')
        output_dir = self.output_var.get()
        format_choice = self.format_var.get()
        skip_existing = self.skip_existing_var.get()
        t = threading.Thread(target=self._load_info_thread,
                             args=(url, output_dir, format_choice, skip_existing), daemon=True)
        t.start()

    def _load_info_thread(self, url, output_dir, format_choice, skip_existing):
        try:
            infos = self.downloader.load_info(url)
        except Exception as e:
            self._threadsafe_log(f"Failed to load info: {e}")
            self.infos = []
            self.after(0, self._populate_tree)
            self.after(0, lambda: self.start_btn.configure(state='normal'))
            return
        self.downloader.mark_existing(infos, output_dir, format_choice, skip_existing)
        self.infos = infos
        self.after(0, self._populate_tree)
        self.after(0, lambda: self._record_playlist_history(url, infos))
        self.after(0, lambda: self.start_btn.configure(state='normal'))

    def _populate_tree(self, log_message=True):
        loaded_count = len(self.infos)
        present_count = sum(1 for info in self.infos if info.get('_already_present'))
        pending_count = loaded_count - present_count
        self._loaded_count_var.set(
            f"{loaded_count} chargé(s) • {present_count} déjà présent(s) • {pending_count} à télécharger"
        )
        self._refresh_summary()
        for item in self.tree.get_children():
            self.tree.delete(item)
        if not self.infos:
            self._append_log("No items found.")
            return
        for idx, info in enumerate(self.infos):
            if self.hide_unavailable and info.get('_availability') not in (None, 'ok', 'unknown'):
                continue
            title = info.get('title', 'unknown')
            duration = format_duration(info.get('duration'))
            uploader = info.get('uploader') or info.get('channel') or info.get('uploader_id') or ""
            display_index = info.get('playlist_index') or idx + 1
            availability = info.get('_availability', 'unknown')
            tag = 'already_present' if info.get('_already_present') else self._availability_tag(availability)
            self.tree.insert("", "end", iid=str(idx),
                             values=(display_index, title, duration, uploader, availability), tags=(tag,))
        if log_message:
            self._append_log(f"Loaded {len(self.infos)} items. Select items to download (Ctrl+click for multiple).")

    @staticmethod
    def _availability_tag(status):
        if status == 'ok':
            return 'availability_ok'
        if status in ('private', 'login_required', 'age_restricted'):
            return 'availability_restricted'
        if status in ('deleted', 'error'):
            return 'availability_unavailable'
        return ''

    def toggle_unavailable_filter(self):
        self.hide_unavailable = not self.hide_unavailable
        self.filter_button.configure(text="Afficher tous les titres" if self.hide_unavailable else "Masquer indisponibles")
        self._populate_tree()

    def verify_availability(self):
        if not self._dependencies_ready:
            self.show_environment()
            return
        if not self.infos:
            self._append_log("No loaded items to verify.")
            return
        selection = self.tree.selection()
        if selection:
            selected_indices = [int(item_id) for item_id in selection]
            items = [self.infos[index] for index in selected_indices]
        else:
            items = list(self.infos)
        self.availability_progress.configure(maximum=len(items), value=0)
        self.availability_progress_var.set(f"Vérification 0/{len(items)}...")
        self.availability_progress_label.pack(fill='x', padx=6, pady=(2, 0))
        self.availability_progress.pack(fill='x', padx=6, pady=(0, 2))
        self.start_btn.configure(state='disabled')
        self.filter_button.configure(state='disabled')
        threading.Thread(target=self._verify_availability_thread, args=(items,), daemon=True).start()

    def _verify_availability_thread(self, items):
        try:
            counts = self.downloader.check_availability_batch(items, self._threadsafe_availability_progress)
        except Exception as error:
            self._threadsafe_log(f"Availability verification failed: {error}")
            try:
                self.after(0, lambda: self._finish_availability_verification(None))
            except tk.TclError:
                pass
            return
        try:
            self.after(0, lambda: self._finish_availability_verification(counts))
        except tk.TclError:
            pass

    def _threadsafe_availability_progress(self, current, total, status, info):
        try:
            self.after(0, lambda: self._update_availability_progress(current, total, status))
        except tk.TclError:
            pass

    def _update_availability_progress(self, current, total, status):
        self.availability_progress.configure(value=current)
        self.availability_progress_var.set(f"Vérification {current}/{total}... ({status})")
        self._populate_tree(log_message=False)

    def _finish_availability_verification(self, counts):
        self.start_btn.configure(state='normal')
        self.filter_button.configure(state='normal')
        if counts is None:
            self.availability_progress_var.set("Vérification interrompue.")
            return
        summary = " • ".join(f"{count} {status}" for status, count in sorted(counts.items()))
        self.availability_progress_var.set(f"Vérification terminée: {summary}")
        self._append_log(f"Availability summary: {summary}")

    def browse_output(self):
        folder = filedialog.askdirectory(initialdir=self.output_var.get() or os.path.expanduser("~"))
        if folder:
            self.output_var.set(folder)

    def open_output_folder(self):
        folder = self.output_var.get()
        if not os.path.isdir(folder):
            messagebox.showinfo("Output folder", "The output folder does not exist yet.")
            return
        try:
            if platform.system() == 'Windows':
                os.startfile(folder)
            elif platform.system() == 'Darwin':
                subprocess.Popen(['open', folder])
            else:
                subprocess.Popen(['xdg-open', folder])
        except OSError as error:
            self._append_log(f"Could not open output folder: {error}")

    @staticmethod
    def _format_file_size(size):
        units = ('o', 'Ko', 'Mo', 'Go')
        value = float(size)
        for unit in units:
            if value < 1024 or unit == units[-1]:
                return f"{value:.1f} {unit}" if unit != 'o' else f"{int(value)} {unit}"
            value /= 1024

    def _refresh_destination_files(self):
        if not hasattr(self, 'destination_tree'):
            return
        for item_id in self.destination_tree.get_children():
            self.destination_tree.delete(item_id)
        folder = self.output_var.get().strip()
        audio_extensions = {'.mp3', '.m4a', '.flac', '.opus', '.wav', '.aac', '.webm', '.ogg'}
        files = []
        try:
            if os.path.isdir(folder):
                files = [entry for entry in os.scandir(folder)
                         if entry.is_file() and os.path.splitext(entry.name)[1].lower() in audio_extensions]
        except OSError as error:
            self._append_log(f"Impossible de scanner le dossier de destination : {error}")
        files.sort(key=lambda entry: entry.name.lower())
        for index, entry in enumerate(files):
            try:
                modified = datetime.datetime.fromtimestamp(entry.stat().st_mtime).strftime('%d/%m/%Y %H:%M')
                size = self._format_file_size(entry.stat().st_size)
            except OSError:
                modified, size = '--', '--'
            self.destination_tree.insert('', 'end', iid=str(index),
                                        values=(entry.name, size, modified),
                                        tags=('already_present',))
        self.destination_count_var.set(f"{len(files)} fichier" + ('s' if len(files) != 1 else '') + " présent" + ('s' if len(files) != 1 else ''))
        self._refresh_summary()

    def _open_audio_file(self, path):
        if not os.path.isfile(path):
            self._append_log(f"Le fichier n’existe pas : {path}")
            return
        try:
            if platform.system() == 'Windows':
                os.startfile(path)
            elif platform.system() == 'Darwin':
                subprocess.Popen(['open', path])
            else:
                subprocess.Popen(['xdg-open', path])
        except OSError as error:
            self._append_log(f"Impossible d’ouvrir le fichier audio : {error}")

    def listen_selected_destination_file(self, event=None):
        selection = self.destination_tree.selection()
        if not selection and event:
            row = self.destination_tree.identify_row(event.y)
            if row:
                self.destination_tree.selection_set(row)
                selection = (row,)
        if not selection:
            self._append_log("Sélectionnez un fichier audio dans Destination pour l’écouter.")
            return
        filename = self.destination_tree.item(selection[0], 'values')[0]
        self._open_audio_file(os.path.join(self.output_var.get(), filename))

    def listen_selected_queue_item(self, event=None):
        selection = self.queue_tree.selection()
        if not selection:
            if event:
                row = self.queue_tree.identify_row(event.y)
                if row:
                    self.queue_tree.selection_set(row)
                    selection = (row,)
        if not selection:
            self._append_log("Sélectionnez un fichier audio dans la file pour l’écouter.")
            return
        item_id = int(selection[0])
        item = next((candidate for candidate in self.downloader.get_queue_snapshot()
                     if candidate['id'] == item_id), None)
        if not item:
            return
        path = item['info'].get('_expected_filepath') or self.downloader.get_expected_filepath(
            item['info'], self.output_var.get(), self.format_var.get())
        if not os.path.isfile(path):
            self._append_log(f"Le fichier n’existe pas encore : {path}")
            return
        self._open_audio_file(path)

    def start_download(self):
        if not self._dependencies_ready:
            self.show_environment()
            return
        if not self.downloader.has_pending_items():
            messagebox.showinfo("File vide", "Ajoutez d’abord des éléments à la file depuis la colonne Source.")
            return

        output_dir = self.output_var.get().strip()
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as error:
            messagebox.showerror("Dossier invalide", f"Impossible de créer le dossier de sortie : {error}")
            return

        fmt = self.format_var.get()
        skip_existing = self.skip_existing_var.get()
        force_download = self.force_download_var.get()
        custom_settings = self._get_custom_settings()
        if custom_settings is None:
            return
        speed_mode = self.speed_mode_var.get()
        self.current_pb['value'] = 0
        self.overall_pb['value'] = 0
        self.start_btn.configure(state='disabled')
        self.pause_btn.configure(state='normal', text='Pause')
        self.stop_btn.configure(state='normal')

        self._append_log(f"Démarrage de la file avec le mode : {speed_mode}")
        started = self.downloader.start_worker(output_dir=output_dir, format_choice=fmt,
                               speed_mode=speed_mode, custom_settings=custom_settings,
                               skip_existing=skip_existing, force_download=force_download)
        if started:
            self._session_restored = False
            self._append_log("File d’attente démarrée.")
        else:
            self._append_log("Impossible de démarrer : un téléchargement est déjà en cours.")

    def toggle_pause(self):
        if self.downloader.pause_event.is_set():
            self.downloader.resume()
            self.pause_btn.configure(text='Pause')
        else:
            self.downloader.pause()
            self.pause_btn.configure(text='Resume')

    def stop_download(self):
        if messagebox.askyesno("Confirm stop", "Stop and cancel all remaining downloads?"):
            self.downloader.stop()
            self.pause_btn.configure(state='disabled')
            self.stop_btn.configure(state='disabled')

    def _startup_checks(self):
        diagnostics = get_environment_diagnostics()
        self._last_environment_diagnostics = diagnostics
        ytdlp = diagnostics['yt-dlp']
        self._dependencies_ready = ytdlp['import_available'] and ytdlp['status_detail'] != 'Version trop ancienne'
        self._set_dependency_controls(ytdlp)
        if ytdlp['status'] == 'OK':
            self._append_log(f"yt-dlp disponible: {ytdlp['version']}")
        else:
            self._append_log(f"yt-dlp: {ytdlp['status_detail']}")
        if diagnostics['ffmpeg']['status'] != 'OK':
            self._append_log("Attention: FFmpeg est absent ou introuvable dans le PATH.")

    def _set_dependency_controls(self, ytdlp):
        if ytdlp['status'] == 'OK':
            self._warning_var.set('')
            self._warning_label.pack_forget()
        elif self._dependencies_ready:
            self._warning_var.set("Une mise à jour de yt-dlp est disponible. Vous pouvez la lancer depuis le dialogue Environnement.")
            self._warning_label.pack(fill='x', padx=6, pady=(4, 0), before=self._top_frame)
        elif ytdlp['system_install']:
            message = ("Attention: yt-dlp est installé sur le système mais pas dans le venv utilisé par cette application. "
                       "Activez le venv puis exécutez « pip install -U yt-dlp », ensuite relancez l’application.")
            self._warning_var.set(message)
            self._warning_label.pack(fill='x', padx=6, pady=(4, 0), before=self._top_frame)
        elif ytdlp['status_detail'] == 'Version trop ancienne':
            self._warning_var.set("Attention: la version de yt-dlp est trop ancienne. Mettez-la à jour avec « pip install -U yt-dlp ».")
            self._warning_label.pack(fill='x', padx=6, pady=(4, 0), before=self._top_frame)
        else:
            self._warning_var.set("yt-dlp est indisponible dans l’environnement Python actuel. Installez-le avec « pip install -U yt-dlp », puis relancez l’application.")
            self._warning_label.pack(fill='x', padx=6, pady=(4, 0), before=self._top_frame)
        state = 'normal' if self._dependencies_ready else 'disabled'
        self.load_info_btn.configure(state=state)
        self.availability_btn.configure(state=state)
        self.start_btn.configure(state=state)

    def show_environment(self):
        if getattr(self, '_environment_dialog', None) and self._environment_dialog.winfo_exists():
            self._environment_dialog.lift()
            return
        dialog = tk.Toplevel(self)
        self._environment_dialog = dialog
        dialog.title("Environnement / Dépendances")
        dialog.geometry("820x620")
        dialog.minsize(700, 500)
        dialog.transient(self)

        content = ttk.Frame(dialog, padding=16)
        content.pack(fill='both', expand=True)
        ttk.Label(content, text="Environnement de téléchargement", font=('TkDefaultFont', 15, 'bold')).pack(anchor='w')
        ttk.Label(content, text=("Ce dialogue vérifie les outils utilisés par YT Audio DLer. "
                                 "L’application permet de télécharger l’audio de vidéos ou playlists YouTube, "
                                 "avec file d’attente, pause, reprise de session et mode lent."),
                  wraplength=770, justify='left').pack(anchor='w', pady=(4, 14))

        self._environment_vars = {}
        self._environment_status_labels = {}
        sections = ttk.Frame(content)
        sections.pack(fill='x')
        for column, (tool, label) in enumerate((('yt-dlp', 'yt-dlp'), ('ffmpeg', 'FFmpeg'))):
            section = ttk.LabelFrame(sections, text=label, padding=10)
            section.grid(row=0, column=column, sticky='nsew', padx=(0 if column == 0 else 8, 0))
            sections.columnconfigure(column, weight=1)
            status_row = ttk.Frame(section)
            status_row.pack(fill='x', pady=(0, 6))
            ttk.Label(status_row, text="Statut:", font=('TkDefaultFont', 10, 'bold')).pack(side='left')
            status_label = ttk.Label(status_row, text="Détection en cours...", font=('TkDefaultFont', 10, 'bold'))
            status_label.pack(side='left', padx=(6, 0))
            self._environment_status_labels[tool] = status_label
            fields = ('path', 'version', 'latest_version')
            for field in fields:
                ttk.Label(section, text={
                    'path': 'Chemin trouvé:', 'version': 'Version:', 'latest_version': 'Dernière version:'
                }[field]).pack(anchor='w', pady=(2, 0))
                variable = tk.StringVar(value="Détection en cours...")
                variable_label = ttk.Label(section, textvariable=variable, wraplength=350)
                variable_label.pack(anchor='w')
                self._environment_vars[(tool, field)] = variable

        ttk.Label(content, text="Aide", font=('TkDefaultFont', 11, 'bold')).pack(anchor='w', pady=(16, 4))
        self._environment_os_var = tk.StringVar(value="")
        ttk.Label(content, textvariable=self._environment_os_var, foreground='#555555').pack(anchor='w')
        self._environment_help_var = tk.StringVar(value="")
        ttk.Label(content, textvariable=self._environment_help_var, wraplength=770, justify='left').pack(
            fill='x', anchor='w', pady=(5, 8))

        buttons = ttk.Frame(dialog)
        buttons.pack(fill='x', padx=16, pady=(0, 16))
        ttk.Button(buttons, text="Vérifier les mises à jour", command=self._refresh_environment).pack(side='left')
        ttk.Button(buttons, text="Mettre à jour yt-dlp", command=self._confirm_ytdlp_update).pack(side='left', padx=(8, 0))
        ttk.Button(buttons, text="Copier la commande", command=self._copy_ytdlp_command).pack(side='left', padx=(8, 0))
        ttk.Button(buttons, text="Ouvrir la page FFmpeg",
                   command=lambda: webbrowser.open("https://ffmpeg.org/download.html")).pack(side='left', padx=(8, 0))
        ttk.Button(buttons, text="Copier les infos de diagnostic", command=self._copy_environment_info).pack(side='right')
        self._refresh_environment()

    def _refresh_environment(self):
        if not getattr(self, '_environment_dialog', None) or not self._environment_dialog.winfo_exists():
            return
        diagnostics = get_environment_diagnostics()
        self._last_environment_diagnostics = diagnostics
        ytdlp = diagnostics['yt-dlp']
        self._dependencies_ready = ytdlp['import_available'] and ytdlp['status_detail'] != 'Version trop ancienne'
        for tool in ('yt-dlp', 'ffmpeg'):
            data = diagnostics[tool]
            status_label = self._environment_status_labels[tool]
            status_label.configure(text=data['status'], foreground={
                'OK': '#187a2f', 'Attention': '#b26a00', 'Manquant': '#b3261e'
            }.get(data['status'], '#555555'))
            self._environment_vars[(tool, 'path')].set(data['path'] or 'Non trouvé')
            self._environment_vars[(tool, 'version')].set(data['version'] or 'Non détectée')
            latest = data['latest_version'] or ('Non standardisée' if tool == 'ffmpeg' else 'Indisponible')
            self._environment_vars[(tool, 'latest_version')].set(latest)
        self._environment_os_var.set(diagnostics['os'])
        ytdlp = diagnostics['yt-dlp']
        if ytdlp['system_install']:
            help_text = ("yt-dlp est installé sur votre système mais n’est pas disponible dans le venv utilisé par cette application.\n\n"
                         "Solution:\n1. Ouvrez un terminal dans le dossier de l’application\n2. Activez le venv\n"
                         "3. Exécutez: pip install -U yt-dlp\n\nEnsuite relancez l’application.")
        elif ytdlp['status_detail'] == 'Version trop ancienne':
            help_text = "La version de yt-dlp est trop ancienne. Mettez-la à jour dans le venv avec: pip install -U yt-dlp"
        elif not ytdlp['import_available']:
            help_text = "yt-dlp est manquant dans l’environnement Python actuel. Activez le venv puis exécutez: pip install -U yt-dlp"
        else:
            help_text = "yt-dlp est disponible dans le venv. "
        if diagnostics['ffmpeg']['status'] != 'OK':
            help_text += "\n\nFFmpeg est absent ou introuvable dans le PATH. Il est requis pour convertir vers MP3, M4A, FLAC ou OPUS. Téléchargez-le depuis ffmpeg.org puis ajoutez son dossier bin au PATH."
        self._environment_help_var.set(help_text)
        self._set_dependency_controls(ytdlp)
        if diagnostics['yt-dlp']['status'] != 'OK' or diagnostics['ffmpeg']['status'] != 'OK':
            self._append_log("Dependency warning: yt-dlp or FFmpeg requires attention.")

    def _copy_ytdlp_command(self):
        self.clipboard_clear()
        self.clipboard_append('pip install -U yt-dlp')
        self._append_log("Commande d’installation yt-dlp copiée.")

    def _confirm_ytdlp_update(self):
        if not messagebox.askyesno("Mettre à jour yt-dlp", "Installer la dernière version de yt-dlp dans le venv actuel ?"):
            return
        threading.Thread(target=self._run_ytdlp_update, daemon=True).start()

    def _run_ytdlp_update(self):
        result = update_ytdlp()
        self._threadsafe_log(f"yt-dlp update result: {result['output'] or 'no output'}")
        try:
            self.after(0, self._refresh_environment)
        except tk.TclError:
            pass

    def _copy_environment_info(self):
        diagnostics = getattr(self, '_last_environment_diagnostics', get_environment_diagnostics(include_latest=False))
        lines = [f"OS: {diagnostics['os']}"]
        for tool in ('yt-dlp', 'ffmpeg'):
            data = diagnostics[tool]
            lines.append(f"{tool}: version={data['version'] or 'not detected'}, path={data['path'] or 'not found'}, status={data['status']}")
        text = "\n".join(lines)
        self.clipboard_clear()
        self.clipboard_append(text)
        self._append_log("Dependency diagnostic information copied to clipboard.")

    def on_closing(self):
        if messagebox.askokcancel("Quit", "Do you want to quit?"):
            try:
                self._append_log("Shutting down...")
                if self.downloader:
                    self.downloader.stop()
                    wt = self.downloader.worker_thread
                    if wt and wt.is_alive():
                        self._append_log("Waiting for worker to exit...")
                        wt.join(timeout=3.0)
                    self._save_session()
                self.destroy()
            except Exception:
                self.destroy()
