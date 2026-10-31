import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import json
import os
import re
import shutil
import csv
from datetime import datetime, timedelta
from pathlib import Path
from collections import Counter

# ----------------------------------------------------------------------------
# Constants
# ----------------------------------------------------------------------------
BG = "#d4d0c8"
DATE_FMT = "%Y-%m-%d %H:%M"
STATUSES = ["Pending", "In Progress", "Repaired", "Canceled"]
CLOSED = ("Repaired", "Canceled")
PRIORITIES = ["Low", "Normal", "High", "Urgent"]
PRIORITY_RANK = {p: i for i, p in enumerate(PRIORITIES)}
STATUS_COLORS = {
    "Pending": "#b8860b",
    "In Progress": "#1f4e9c",
    "Repaired": "#006400",
    "Canceled": "#8b0000",
}
MAX_BACKUPS = 10
OVERDUE_DAYS = 7


def now():
    return datetime.now().strftime(DATE_FMT)


def parse_date(text):
    try:
        return datetime.strptime(text, DATE_FMT)
    except (ValueError, TypeError):
        return None


def fmt_duration(td):
    seconds = td.total_seconds()
    if seconds < 3600:
        return f"{int(seconds // 60)} min"
    hours = seconds / 3600
    if hours < 48:
        return f"{hours:.1f} hours"
    return f"{hours / 24:.1f} days"


def ticket_age_days(device):
    """Days a ticket has been open (or how long it took if it is closed)."""
    start = parse_date(device.get("date_submitted"))
    if not start:
        return None
    end = None
    if device["status"] in CLOSED:
        end = parse_date(device.get("date_repaired"))
    end = end or datetime.now()
    return max((end - start).days, 0)


class QueueRepairApp:
    def __init__(self, root):
        self.root = root
        self.root.title("QueueRepair - IT Repair Tracker")
        self.root.geometry("1150x780")
        self.root.minsize(900, 600)
        self.root.configure(bg=BG)

        # Data setup
        self.data_dir = Path("QueueRepairData")
        self.data_file = self.data_dir / "repair_data.json"
        self.backup_dir = self.data_dir / "backups"
        self.data_dir.mkdir(exist_ok=True)

        # State
        self.undo_stack = []        # lists of deleted tickets
        self.visible_devices = []   # what the table currently shows
        self.sort_col = "id"
        self.sort_reverse = False
        self.dashboard_win = None

        self.auto_backup()
        self.devices = self.load_data()

        self.create_menu()
        self.create_widgets()
        self.create_context_menu()
        self.setup_keyboard_shortcuts()
        self.filter_devices()

    # ------------------------------------------------------------------
    # Data handling
    # ------------------------------------------------------------------
    @staticmethod
    def normalize(d):
        """Fill in fields that older data files do not have."""
        d["id"] = int(d["id"])
        d.setdefault("device", "")
        d.setdefault("serial", "")
        d.setdefault("issue", "")
        d.setdefault("submitted", "Unknown")
        d.setdefault("contact", "")
        d.setdefault("date_submitted", "")
        d.setdefault("date_repaired", "")
        if d.get("status") not in STATUSES:
            d["status"] = "Pending"
        if d.get("priority") not in PRIORITIES:
            d["priority"] = "Normal"
        if not isinstance(d.get("notes"), list):
            d["notes"] = []
        return d

    def load_data(self):
        try:
            if self.data_file.exists():
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                if not isinstance(data, list):
                    raise ValueError("Unexpected data format")
                return [self.normalize(d) for d in data]
            return []
        except (ValueError, TypeError, KeyError):
            # Keep a copy of the broken file so nothing is lost for good
            try:
                shutil.copy2(self.data_file, self.data_file.with_suffix(".corrupt.json"))
            except OSError:
                pass
            messagebox.showwarning(
                "Warning",
                "Data file is corrupted. Starting with empty data.\n"
                "A copy of the broken file was kept as repair_data.corrupt.json.\n"
                "You can also use File > Restore Backup.")
            return []
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load data: {str(e)}")
            return []

    def save_data(self):
        try:
            # Write to a temp file first so a crash can't leave a half-written file
            tmp = self.data_file.with_suffix(".tmp")
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self.devices, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self.data_file)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to save data: {str(e)}")

    def auto_backup(self):
        """Copy the data file into backups/ on every start, keep the newest few."""
        if not self.data_file.exists():
            return
        try:
            self.backup_dir.mkdir(exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2(self.data_file, self.backup_dir / f"repair_data_{stamp}.json")
            backups = sorted(self.backup_dir.glob("repair_data_*.json"))
            for old in backups[:-MAX_BACKUPS]:
                old.unlink()
        except OSError:
            pass

    def backup_now(self):
        self.save_data()
        self.auto_backup()
        self.status_var.set("Backup created")
        messagebox.showinfo("Backup", f"Backup saved in:\n{os.path.abspath(self.backup_dir)}")

    def restore_backup(self):
        self.backup_dir.mkdir(exist_ok=True)
        path = filedialog.askopenfilename(
            title="Restore backup", initialdir=self.backup_dir,
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            restored = [self.normalize(d) for d in data]
        except Exception as e:
            messagebox.showerror("Restore Error", f"Could not read that backup:\n{e}")
            return
        if not messagebox.askyesno(
                "Confirm Restore",
                f"Replace the current {len(self.devices)} ticket(s) with {len(restored)} ticket(s) from the backup?"):
            return
        self.auto_backup()  # keep the current state just in case
        self.devices = restored
        self.undo_stack.clear()
        self.save_data()
        self.filter_devices()
        self.status_var.set("Backup restored")

    def next_id(self):
        return max([d['id'] for d in self.devices], default=0) + 1

    @staticmethod
    def add_log(device, text):
        device.setdefault("notes", []).append({"time": now(), "text": text})

    # ------------------------------------------------------------------
    # Menu, shortcuts, context menu
    # ------------------------------------------------------------------
    def create_menu(self):
        menubar = tk.Menu(self.root)

        file_menu = tk.Menu(menubar, tearoff=0)
        file_menu.add_command(label="Export All to CSV...", accelerator="Ctrl+E", command=self.export_to_csv)
        file_menu.add_command(label="Export Current View to CSV...",
                              command=lambda: self.export_to_csv(view_only=True))
        file_menu.add_command(label="Import from CSV...", command=self.import_from_csv)
        file_menu.add_separator()
        file_menu.add_command(label="Backup Now", command=self.backup_now)
        file_menu.add_command(label="Restore Backup...", command=self.restore_backup)
        file_menu.add_separator()
        file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="New Ticket", accelerator="Ctrl+N", command=self.focus_new_ticket)
        edit_menu.add_command(label="Edit Ticket / Notes...", accelerator="Enter", command=self.edit_device)
        edit_menu.add_separator()
        edit_menu.add_command(label="Start Repair (In Progress)", command=self.start_repair)
        edit_menu.add_command(label="Mark Repaired", command=self.mark_repaired)
        edit_menu.add_command(label="Cancel Repair", command=self.cancel_repair)
        edit_menu.add_command(label="Reopen (Pending)", command=self.reopen)
        edit_menu.add_separator()
        edit_menu.add_command(label="Delete", accelerator="Del", command=self.delete_device)
        edit_menu.add_command(label="Undo Delete", accelerator="Ctrl+Z", command=self.undo_delete)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        view_menu = tk.Menu(menubar, tearoff=0)
        view_menu.add_command(label="Dashboard", accelerator="Ctrl+D", command=self.show_dashboard)
        view_menu.add_command(label="Find", accelerator="Ctrl+F", command=self.focus_search)
        view_menu.add_command(label="Clear Filters", accelerator="Esc", command=self.clear_filters)
        view_menu.add_command(label="Refresh", accelerator="F5", command=self.filter_devices)
        menubar.add_cascade(label="View", menu=view_menu)

        self.root.config(menu=menubar)

    def setup_keyboard_shortcuts(self):
        self.root.bind('<Control-n>', lambda e: self.focus_new_ticket())
        self.root.bind('<Control-f>', lambda e: self.focus_search())
        self.root.bind('<Control-e>', lambda e: self.export_to_csv())
        self.root.bind('<Control-d>', lambda e: self.show_dashboard())
        self.root.bind('<Control-z>', lambda e: self.undo_delete())
        self.root.bind('<F5>', lambda e: self.filter_devices())
        self.root.bind('<Escape>', self.clear_filters)

        # Delete only works while the table has focus, so pressing Delete
        # inside a text box can no longer delete a ticket by accident.
        self.tree.bind('<Delete>', lambda e: self.delete_device())
        self.tree.bind('<Return>', lambda e: self.edit_device())
        self.tree.bind('<Double-1>', self.on_double_click)

        for entry in (self.device_entry, self.serial_entry, self.issue_entry,
                      self.submitted_entry, self.contact_entry):
            entry.bind('<Return>', lambda e: self.add_device())

    def create_context_menu(self):
        self.context_menu = tk.Menu(self.tree, tearoff=0)
        self.context_menu.add_command(label="Edit Ticket / Notes...", command=self.edit_device)
        self.context_menu.add_separator()

        status_menu = tk.Menu(self.context_menu, tearoff=0)
        for status in STATUSES:
            status_menu.add_command(label=status, command=lambda s=status: self.set_status(s))
        self.context_menu.add_cascade(label="Set Status", menu=status_menu)

        priority_menu = tk.Menu(self.context_menu, tearoff=0)
        for priority in PRIORITIES:
            priority_menu.add_command(label=priority, command=lambda p=priority: self.set_priority(p))
        self.context_menu.add_cascade(label="Set Priority", menu=priority_menu)

        self.context_menu.add_separator()
        self.context_menu.add_command(label="Copy Details", command=self.copy_details)
        self.context_menu.add_command(label="Save Ticket as Text...", command=self.export_ticket)
        self.context_menu.add_separator()
        self.context_menu.add_command(label="Delete", command=self.delete_device)

        self.tree.bind("<Button-3>", self.show_context_menu)
        self.tree.bind("<Button-2>", self.show_context_menu)  # macOS

    def show_context_menu(self, event):
        item = self.tree.identify_row(event.y)
        if item:
            if item not in self.tree.selection():
                self.tree.selection_set(item)
            self.context_menu.post(event.x_root, event.y_root)

    def on_double_click(self, event):
        if self.tree.identify_row(event.y):
            self.edit_device()

    def focus_new_ticket(self):
        self.device_entry.focus_set()
        return "break"

    def focus_search(self):
        self.search_entry.focus_set()
        self.search_entry.select_range(0, tk.END)
        return "break"

    # ------------------------------------------------------------------
    # Widgets
    # ------------------------------------------------------------------
    def create_widgets(self):
        # Status bar first so it never gets squeezed out of the window
        self.status_var = tk.StringVar()
        self.status_var.set("Ready • Ctrl+N: New • Ctrl+F: Search • Ctrl+D: Dashboard • "
                            "Ctrl+Z: Undo delete • Double-click a ticket to edit")
        tk.Label(self.root, textvariable=self.status_var, bd=1,
                 relief="sunken", anchor="w", bg=BG).pack(side=tk.BOTTOM, fill=tk.X)

        main_frame = tk.Frame(self.root, bd=1, relief="sunken", bg=BG)
        main_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)

        # Title row
        title_frame = tk.Frame(main_frame, bg=BG)
        title_frame.pack(fill=tk.X, pady=(10, 5))

        tk.Label(title_frame, text="QueueRepair - IT Repair Tracker",
                 font=("Arial", 16, "bold"), bg=BG).pack(side=tk.LEFT, padx=10)

        self.dashboard_btn = ttk.Button(title_frame, text="Dashboard", command=self.show_dashboard)
        self.dashboard_btn.pack(side=tk.RIGHT, padx=10)
        self.export_btn = ttk.Button(title_frame, text="Export CSV", command=self.export_to_csv)
        self.export_btn.pack(side=tk.RIGHT, padx=10)

        # Search + filter row
        search_frame = tk.Frame(main_frame, bg=BG)
        search_frame.pack(fill=tk.X, padx=5, pady=5)

        tk.Label(search_frame, text="Search:", bg=BG, font=("Arial", 10)).pack(side=tk.LEFT)
        self.search_entry = tk.Entry(search_frame, width=30, relief="sunken", font=("Arial", 10))
        self.search_entry.pack(side=tk.LEFT, padx=5)
        self.search_entry.bind("<KeyRelease>", self.filter_devices)

        tk.Label(search_frame, text="Status:", bg=BG, font=("Arial", 10)).pack(side=tk.LEFT, padx=(10, 0))
        self.status_filter = ttk.Combobox(search_frame, values=["All"] + STATUSES,
                                          state="readonly", width=12)
        self.status_filter.set("All")
        self.status_filter.pack(side=tk.LEFT, padx=5)
        self.status_filter.bind("<<ComboboxSelected>>", self.filter_devices)

        tk.Label(search_frame, text="Priority:", bg=BG, font=("Arial", 10)).pack(side=tk.LEFT, padx=(10, 0))
        self.priority_filter = ttk.Combobox(search_frame, values=["All"] + PRIORITIES,
                                            state="readonly", width=10)
        self.priority_filter.set("All")
        self.priority_filter.pack(side=tk.LEFT, padx=5)
        self.priority_filter.bind("<<ComboboxSelected>>", self.filter_devices)

        self.hide_closed = tk.BooleanVar(value=False)
        tk.Checkbutton(search_frame, text="Hide closed", variable=self.hide_closed, bg=BG,
                       activebackground=BG, command=self.filter_devices).pack(side=tk.LEFT, padx=10)

        ttk.Button(search_frame, text="Clear", command=self.clear_filters).pack(side=tk.LEFT)

        # Form
        form_frame = tk.Frame(main_frame, bg=BG)
        form_frame.pack(fill=tk.X, padx=20, pady=10)

        tk.Label(form_frame, text="Device Name:", bg=BG, font=("Arial", 10)).grid(row=0, column=0, padx=5, pady=5, sticky="e")
        self.device_entry = tk.Entry(form_frame, width=30, relief="sunken", font=("Arial", 10))
        self.device_entry.grid(row=0, column=1, padx=5, pady=5)

        tk.Label(form_frame, text="Serial Number:", bg=BG, font=("Arial", 10)).grid(row=0, column=2, padx=5, pady=5, sticky="e")
        self.serial_entry = tk.Entry(form_frame, width=20, relief="sunken", font=("Arial", 10))
        self.serial_entry.grid(row=0, column=3, padx=5, pady=5)

        tk.Label(form_frame, text="Issue Description:", bg=BG, font=("Arial", 10)).grid(row=1, column=0, padx=5, pady=5, sticky="e")
        self.issue_entry = tk.Entry(form_frame, width=30, relief="sunken", font=("Arial", 10))
        self.issue_entry.grid(row=1, column=1, padx=5, pady=5)

        tk.Label(form_frame, text="Submitted By:", bg=BG, font=("Arial", 10)).grid(row=1, column=2, padx=5, pady=5, sticky="e")
        self.submitted_entry = tk.Entry(form_frame, width=20, relief="sunken", font=("Arial", 10))
        self.submitted_entry.grid(row=1, column=3, padx=5, pady=5)

        tk.Label(form_frame, text="Contact Info:", bg=BG, font=("Arial", 10)).grid(row=2, column=0, padx=5, pady=5, sticky="e")
        self.contact_entry = tk.Entry(form_frame, width=30, relief="sunken", font=("Arial", 10))
        self.contact_entry.grid(row=2, column=1, padx=5, pady=5)

        tk.Label(form_frame, text="Priority:", bg=BG, font=("Arial", 10)).grid(row=2, column=2, padx=5, pady=5, sticky="e")
        self.priority_combo = ttk.Combobox(form_frame, values=PRIORITIES, state="readonly", width=17)
        self.priority_combo.set("Normal")
        self.priority_combo.grid(row=2, column=3, padx=5, pady=5, sticky="w")

        # Buttons
        button_frame = tk.Frame(main_frame, bg=BG)
        button_frame.pack(pady=10)

        style = ttk.Style()
        style.configure("TButton", padding=6, relief="raised", background=BG, font=("Arial", 9))

        # Work around a Tk bug where row colours don't show on some Windows builds
        def fixed_map(option):
            return [elm for elm in style.map("Treeview", query_opt=option)
                    if elm[:2] != ("!disabled", "!selected")]
        style.map("Treeview", foreground=fixed_map("foreground"), background=fixed_map("background"))

        buttons = [
            ("Add New Device", self.add_device),
            ("Start Repair", self.start_repair),
            ("Mark Repaired", self.mark_repaired),
            ("Cancel Repair", self.cancel_repair),
            ("Reopen", self.reopen),
            ("Edit / Notes", self.edit_device),
            ("Delete Selected", self.delete_device),
        ]
        for text, command in buttons:
            ttk.Button(button_frame, text=text, command=command).pack(side=tk.LEFT, padx=5)

        # Table
        tree_container = tk.Frame(main_frame, bg=BG)
        tree_container.pack(fill=tk.BOTH, expand=True, padx=20, pady=10)

        self.counter_label = tk.Label(tree_container, text="", bg=BG, font=("Arial", 9))
        self.counter_label.pack(anchor="w")

        tree_frame = tk.Frame(tree_container, bg=BG)
        tree_frame.pack(fill=tk.BOTH, expand=True)

        columns = ("id", "priority", "device", "serial", "issue", "submitted",
                   "contact", "status", "days", "date_repaired")
        self.tree = ttk.Treeview(tree_frame, columns=columns, show="headings",
                                 height=12, selectmode="extended")

        col_configs = [
            ("id", "ID", 45, "center"),
            ("priority", "Priority", 70, "center"),
            ("device", "Device Name", 130, "w"),
            ("serial", "Serial Number", 100, "w"),
            ("issue", "Issue Description", 190, "w"),
            ("submitted", "Submitted By", 100, "w"),
            ("contact", "Contact Info", 120, "w"),
            ("status", "Status", 90, "center"),
            ("days", "Days", 55, "center"),
            ("date_repaired", "Date Closed", 115, "w"),
        ]
        self.heading_text = {}
        for col, heading, width, anchor in col_configs:
            self.heading_text[col] = heading
            self.tree.heading(col, text=heading, command=lambda c=col: self.sort_treeview(c))
            self.tree.column(col, width=width, anchor=anchor, minwidth=40)

        # Row colours
        self.tree.tag_configure("urgent", background="#ffc9c9")
        self.tree.tag_configure("high", background="#ffe2b8")
        self.tree.tag_configure("progress", background="#d6e6ff")
        self.tree.tag_configure("repaired", background="#e3f4e3", foreground="#2e6b2e")
        self.tree.tag_configure("canceled", foreground="#777777")

        vsb = ttk.Scrollbar(tree_frame, orient="vertical", command=self.tree.yview)
        hsb = ttk.Scrollbar(tree_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        hsb.pack(side="bottom", fill="x")
        vsb.pack(side="right", fill="y")
        self.tree.pack(fill=tk.BOTH, expand=True)

        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_counter())

    # ------------------------------------------------------------------
    # Table: filtering, sorting
    # ------------------------------------------------------------------
    @staticmethod
    def search_blob(d):
        parts = [str(d["id"]), d["device"], d["serial"], d["issue"], d["submitted"],
                 d["contact"], d["priority"], d["status"]]
        parts += [n.get("text", "") for n in d.get("notes", [])]
        return " ".join(parts).lower()

    @staticmethod
    def row_tags(d):
        if d["status"] == "Repaired":
            return ("repaired",)
        if d["status"] == "Canceled":
            return ("canceled",)
        if d["priority"] == "Urgent":
            return ("urgent",)
        if d["priority"] == "High":
            return ("high",)
        if d["status"] == "In Progress":
            return ("progress",)
        return ()

    def sort_key(self, d, col):
        if col == "id":
            return d["id"]
        if col == "priority":
            return PRIORITY_RANK[d["priority"]]
        if col == "status":
            return STATUSES.index(d["status"])
        if col == "days":
            age = ticket_age_days(d)
            return age if age is not None else -1
        if col == "date_repaired":
            return d.get("date_repaired", "")
        return str(d.get(col, "")).lower()

    def sort_treeview(self, col):
        if col == self.sort_col:
            self.sort_reverse = not self.sort_reverse
        else:
            self.sort_col = col
            # Most useful first: urgent tickets and oldest tickets on top
            self.sort_reverse = col in ("priority", "days")
        self.filter_devices()

    def update_headings(self):
        for col, text in self.heading_text.items():
            arrow = ""
            if col == self.sort_col:
                arrow = " ▼" if self.sort_reverse else " ▲"
            self.tree.heading(col, text=text + arrow)

    def filter_devices(self, event=None):
        term = self.search_entry.get().strip().lower()
        status_f = self.status_filter.get()
        prio_f = self.priority_filter.get()
        hide_closed = self.hide_closed.get()

        visible = []
        for d in self.devices:
            if status_f != "All" and d["status"] != status_f:
                continue
            if prio_f != "All" and d["priority"] != prio_f:
                continue
            if hide_closed and d["status"] in CLOSED:
                continue
            if term and term not in self.search_blob(d):
                continue
            visible.append(d)

        visible.sort(key=lambda d: self.sort_key(d, self.sort_col), reverse=self.sort_reverse)
        self.visible_devices = visible

        previously_selected = set(self.tree.selection())
        self.tree.delete(*self.tree.get_children())
        for d in visible:
            age = ticket_age_days(d)
            values = (
                d['id'], d['priority'], d['device'], d['serial'], d['issue'],
                d['submitted'], d['contact'], d['status'],
                "" if age is None else age, d['date_repaired'],
            )
            self.tree.insert("", "end", iid=str(d['id']), values=values, tags=self.row_tags(d))

        keep = [i for i in previously_selected if self.tree.exists(i)]
        if keep:
            self.tree.selection_set(keep)

        self.update_headings()
        self.update_counter()

    def update_counter(self):
        open_count = len([d for d in self.devices if d["status"] not in CLOSED])
        text = (f"Showing {len(self.visible_devices)} of {len(self.devices)} devices"
                f"  •  {open_count} open")
        selected = len(self.tree.selection())
        if selected:
            text += f"  •  {selected} selected"
        self.counter_label.config(text=text)

    def clear_filters(self, event=None):
        self.search_entry.delete(0, tk.END)
        self.status_filter.set("All")
        self.priority_filter.set("All")
        self.hide_closed.set(False)
        self.filter_devices()

    # ------------------------------------------------------------------
    # Selection helpers
    # ------------------------------------------------------------------
    def selected_devices(self):
        ids = set()
        for iid in self.tree.selection():
            try:
                ids.add(int(iid))
            except ValueError:
                pass
        return [d for d in self.devices if d['id'] in ids]

    # ------------------------------------------------------------------
    # Ticket actions
    # ------------------------------------------------------------------
    def add_device(self):
        device = self.device_entry.get().strip()
        serial = self.serial_entry.get().strip()
        issue = self.issue_entry.get().strip()
        submitted = self.submitted_entry.get().strip()
        contact = self.contact_entry.get().strip()
        priority = self.priority_combo.get() or "Normal"

        if not device or not issue:
            messagebox.showwarning("Input Error", "Device name and issue description are required!")
            return

        if serial:
            dupes = [d for d in self.devices
                     if d['serial'].lower() == serial.lower() and d['status'] not in CLOSED]
            if dupes and not messagebox.askyesno(
                    "Duplicate Serial",
                    f"Ticket #{dupes[0]['id']} is already open for serial '{serial}'.\n\n"
                    "Add another ticket anyway?"):
                return

        new_device = {
            "id": self.next_id(),
            "device": device,
            "serial": serial,
            "issue": issue,
            "submitted": submitted if submitted else "Unknown",
            "contact": contact,
            "priority": priority,
            "status": "Pending",
            "date_submitted": now(),
            "date_repaired": "",
            "notes": [],
        }
        self.add_log(new_device, "Ticket created")

        self.devices.append(new_device)
        self.save_data()
        self.filter_devices()

        for entry in (self.device_entry, self.serial_entry, self.issue_entry,
                      self.submitted_entry, self.contact_entry):
            entry.delete(0, tk.END)
        self.priority_combo.set("Normal")
        self.device_entry.focus_set()

        self.status_var.set(f"Device '{device}' added for repair (ticket #{new_device['id']})")

    def apply_status(self, device, new_status):
        device['status'] = new_status
        device['date_repaired'] = now() if new_status in CLOSED else ""
        self.add_log(device, f"Status changed to {new_status}")

    def set_status(self, new_status):
        targets = self.selected_devices()
        if not targets:
            messagebox.showwarning("Selection Error", "Please select at least one device first")
            return
        changed = [d for d in targets if d['status'] != new_status]
        if not changed:
            messagebox.showinfo("Info", f"Selected ticket(s) are already marked as {new_status}")
            return
        for d in changed:
            self.apply_status(d, new_status)
        self.save_data()
        self.filter_devices()
        if len(changed) == 1:
            self.status_var.set(f"'{changed[0]['device']}' is now {new_status}")
        else:
            self.status_var.set(f"{len(changed)} tickets are now {new_status}")

    def start_repair(self):
        self.set_status("In Progress")

    def mark_repaired(self):
        self.set_status("Repaired")

    def cancel_repair(self):
        self.set_status("Canceled")

    def reopen(self):
        self.set_status("Pending")

    def set_priority(self, new_priority):
        targets = self.selected_devices()
        if not targets:
            messagebox.showwarning("Selection Error", "Please select at least one device first")
            return
        for d in targets:
            if d['priority'] != new_priority:
                self.add_log(d, f"Priority changed from {d['priority']} to {new_priority}")
                d['priority'] = new_priority
        self.save_data()
        self.filter_devices()
        self.status_var.set(f"Priority set to {new_priority} for {len(targets)} ticket(s)")

    def delete_device(self):
        targets = self.selected_devices()
        if not targets:
            messagebox.showwarning("Selection Error", "Please select a device to delete")
            return

        if len(targets) == 1:
            question = f"Are you sure you want to delete '{targets[0]['device']}'?"
        else:
            question = f"Are you sure you want to delete these {len(targets)} tickets?"
        if not messagebox.askyesno("Confirm Delete", question):
            return

        ids = {d['id'] for d in targets}
        self.devices = [d for d in self.devices if d['id'] not in ids]
        self.undo_stack.append(targets)
        self.save_data()
        self.filter_devices()
        self.status_var.set(f"🗑️ Deleted {len(targets)} ticket(s) • Press Ctrl+Z to undo")

    def undo_delete(self):
        if not self.undo_stack:
            self.status_var.set("Nothing to undo")
            return
        restored = self.undo_stack.pop()
        existing = {d['id'] for d in self.devices}
        for d in restored:
            if d['id'] in existing:      # id was reused by a newer ticket
                d['id'] = self.next_id()
            self.devices.append(d)
            existing.add(d['id'])
        self.save_data()
        self.filter_devices()
        self.status_var.set(f"Restored {len(restored)} deleted ticket(s)")

    # ------------------------------------------------------------------
    # Edit dialog (details + repair log)
    # ------------------------------------------------------------------
    def center_window(self, win, width, height):
        self.root.update_idletasks()
        x = self.root.winfo_rootx() + (self.root.winfo_width() - width) // 2
        y = self.root.winfo_rooty() + (self.root.winfo_height() - height) // 2
        win.geometry(f"{width}x{height}+{max(x, 0)}+{max(y, 0)}")

    def edit_device(self):
        targets = self.selected_devices()
        if not targets:
            messagebox.showwarning("Selection Error", "Please select a device to edit")
            return
        if len(targets) > 1:
            messagebox.showinfo("Info", "Please select just one ticket to edit")
            return
        self.open_edit_dialog(targets[0])

    def open_edit_dialog(self, d):
        win = tk.Toplevel(self.root)
        win.title(f"Ticket #{d['id']}")
        win.configure(bg=BG)
        win.transient(self.root)
        self.center_window(win, 620, 640)
        win.update_idletasks()
        try:
            win.grab_set()
        except tk.TclError:
            pass

        form = tk.Frame(win, bg=BG)
        form.pack(fill=tk.X, padx=15, pady=10)
        form.columnconfigure(1, weight=1)

        fields = {}
        rows = [("Device Name:", "device"), ("Serial Number:", "serial"), ("Issue:", "issue"),
                ("Submitted By:", "submitted"), ("Contact Info:", "contact")]
        for r, (label, key) in enumerate(rows):
            tk.Label(form, text=label, bg=BG, font=("Arial", 10)).grid(row=r, column=0, sticky="e", padx=5, pady=4)
            entry = tk.Entry(form, width=45, relief="sunken", font=("Arial", 10))
            entry.insert(0, d[key])
            entry.grid(row=r, column=1, sticky="ew", padx=5, pady=4)
            fields[key] = entry

        r = len(rows)
        tk.Label(form, text="Priority:", bg=BG, font=("Arial", 10)).grid(row=r, column=0, sticky="e", padx=5, pady=4)
        prio_var = tk.StringVar(value=d["priority"])
        ttk.Combobox(form, textvariable=prio_var, values=PRIORITIES, state="readonly",
                     width=15).grid(row=r, column=1, sticky="w", padx=5, pady=4)

        tk.Label(form, text="Status:", bg=BG, font=("Arial", 10)).grid(row=r + 1, column=0, sticky="e", padx=5, pady=4)
        status_var = tk.StringVar(value=d["status"])
        ttk.Combobox(form, textvariable=status_var, values=STATUSES, state="readonly",
                     width=15).grid(row=r + 1, column=1, sticky="w", padx=5, pady=4)

        info = f"Submitted: {d.get('date_submitted') or 'unknown'}"
        if d["status"] in CLOSED and d.get("date_repaired"):
            info += f"   •   Closed: {d['date_repaired']}"
        age = ticket_age_days(d)
        if age is not None:
            info += f"   •   {age} day(s)"
        tk.Label(form, text=info, bg=BG, font=("Arial", 9), fg="#444444").grid(
            row=r + 2, column=0, columnspan=2, sticky="w", padx=5, pady=(6, 0))

        # Repair log
        tk.Label(win, text="Repair Log / Notes:", bg=BG, font=("Arial", 10, "bold")).pack(anchor="w", padx=20)
        log_frame = tk.Frame(win, bg=BG)
        log_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=5)
        log_text = tk.Text(log_frame, height=10, wrap="word", font=("Arial", 9), state="disabled")
        log_scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
        log_text.configure(yscrollcommand=log_scroll.set)
        log_scroll.pack(side="right", fill="y")
        log_text.pack(fill=tk.BOTH, expand=True)

        notes = d["notes"]  # edited in place; only kept if Save is pressed

        original_notes = list(notes)

        def refresh_log():
            log_text.config(state="normal")
            log_text.delete("1.0", tk.END)
            for n in notes:
                log_text.insert(tk.END, f"[{n.get('time', '')}] {n.get('text', '')}\n")
            log_text.config(state="disabled")
            log_text.see(tk.END)

        note_row = tk.Frame(win, bg=BG)
        note_row.pack(fill=tk.X, padx=20, pady=5)
        note_entry = tk.Entry(note_row, relief="sunken", font=("Arial", 10))
        note_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)

        def add_note(event=None):
            text = note_entry.get().strip()
            if text:
                notes.append({"time": now(), "text": text})
                note_entry.delete(0, tk.END)
                refresh_log()

        note_entry.bind("<Return>", add_note)
        ttk.Button(note_row, text="Add Note", command=add_note).pack(side=tk.LEFT, padx=(8, 0))

        refresh_log()

        def cancel():
            notes[:] = original_notes  # throw away notes added in this dialog
            win.destroy()

        def save():
            values = {k: e.get().strip() for k, e in fields.items()}
            if not values["device"] or not values["issue"]:
                messagebox.showwarning("Input Error", "Device name and issue are required!", parent=win)
                return
            add_note()  # include a note that was typed but not added yet
            d.update(device=values["device"], serial=values["serial"], issue=values["issue"],
                     submitted=values["submitted"] or "Unknown", contact=values["contact"])
            if prio_var.get() != d["priority"]:
                self.add_log(d, f"Priority changed from {d['priority']} to {prio_var.get()}")
                d["priority"] = prio_var.get()
            if status_var.get() != d["status"]:
                self.apply_status(d, status_var.get())
            self.save_data()
            self.filter_devices()
            self.status_var.set(f"Ticket #{d['id']} updated")
            win.destroy()

        btns = tk.Frame(win, bg=BG)
        btns.pack(pady=10)
        ttk.Button(btns, text="Save", command=save).pack(side=tk.LEFT, padx=8)
        ttk.Button(btns, text="Cancel", command=cancel).pack(side=tk.LEFT, padx=8)
        win.protocol("WM_DELETE_WINDOW", cancel)
        win.bind("<Escape>", lambda e: cancel())
        fields["device"].focus_set()

    # ------------------------------------------------------------------
    # Copy / export single ticket
    # ------------------------------------------------------------------
    @staticmethod
    def ticket_text(d):
        lines = [
            f"Ticket #{d['id']}",
            f"Device:       {d['device']}",
            f"Serial:       {d['serial']}",
            f"Issue:        {d['issue']}",
            f"Submitted by: {d['submitted']}",
            f"Contact:      {d['contact']}",
            f"Priority:     {d['priority']}",
            f"Status:       {d['status']}",
            f"Submitted:    {d.get('date_submitted', '')}",
        ]
        if d['status'] in CLOSED and d.get('date_repaired'):
            lines.append(f"Closed:       {d['date_repaired']}")
        age = ticket_age_days(d)
        if age is not None:
            lines.append(f"Days:         {age}")
        if d.get("notes"):
            lines.append("")
            lines.append("Repair log:")
            for n in d["notes"]:
                lines.append(f"  [{n.get('time', '')}] {n.get('text', '')}")
        return "\n".join(lines)

    def copy_details(self):
        targets = self.selected_devices()
        if not targets:
            return
        text = "\n\n".join(self.ticket_text(d) for d in targets)
        self.root.clipboard_clear()
        self.root.clipboard_append(text)
        self.status_var.set("Details copied to clipboard")

    def export_ticket(self):
        targets = self.selected_devices()
        if not targets:
            messagebox.showwarning("Selection Error", "Please select a ticket first")
            return
        d = targets[0]
        path = filedialog.asksaveasfilename(
            title="Save ticket", defaultextension=".txt", initialfile=f"ticket_{d['id']}.txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path:
            return
        try:
            with open(path, 'w', encoding='utf-8') as f:
                f.write(self.ticket_text(d) + "\n")
            self.status_var.set(f"Ticket #{d['id']} saved to {path}")
        except OSError as e:
            messagebox.showerror("Save Error", f"Failed to save ticket: {e}")

    # ------------------------------------------------------------------
    # CSV export / import
    # ------------------------------------------------------------------
    CSV_HEADERS = ['ID', 'Device', 'Serial', 'Issue', 'Submitted By', 'Contact', 'Priority',
                   'Status', 'Date Closed', 'Date Submitted', 'Notes']

    def export_to_csv(self, view_only=False):
        rows = self.visible_devices if view_only else self.devices
        if not rows:
            messagebox.showinfo("Export", "There are no tickets to export.")
            return
        default = f"QueueRepair_Export_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        filename = filedialog.asksaveasfilename(
            title="Export to CSV", defaultextension=".csv", initialfile=default,
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not filename:
            return
        try:
            # utf-8-sig so Excel opens Greek / accented text correctly
            with open(filename, 'w', newline='', encoding='utf-8-sig') as f:
                writer = csv.writer(f)
                writer.writerow(self.CSV_HEADERS)
                for d in rows:
                    notes = " | ".join(f"[{n.get('time', '')}] {n.get('text', '')}" for n in d.get("notes", []))
                    writer.writerow([
                        d['id'], d['device'], d['serial'], d['issue'], d['submitted'],
                        d['contact'], d['priority'], d['status'], d['date_repaired'],
                        d.get('date_submitted', ''), notes,
                    ])
            self.status_var.set(f"Exported {len(rows)} ticket(s) to {filename}")
            messagebox.showinfo("Export Successful",
                                f"{len(rows)} ticket(s) exported to:\n{os.path.abspath(filename)}")
        except Exception as e:
            messagebox.showerror("Export Error", f"Failed to export: {str(e)}")

    def import_from_csv(self):
        path = filedialog.askopenfilename(
            title="Import tickets from CSV",
            filetypes=[("CSV files", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        existing = {(d['device'].lower(), d['serial'].lower(), d['issue'].lower(), d.get('date_submitted', ''))
                    for d in self.devices}
        added = skipped = 0
        try:
            with open(path, newline='', encoding='utf-8-sig') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    device = (row.get("Device") or "").strip()
                    issue = (row.get("Issue") or "").strip()
                    if not device or not issue:
                        skipped += 1
                        continue
                    serial = (row.get("Serial") or "").strip()
                    date_sub = (row.get("Date Submitted") or "").strip() or now()
                    key = (device.lower(), serial.lower(), issue.lower(), date_sub)
                    if key in existing:
                        skipped += 1
                        continue
                    existing.add(key)

                    status = (row.get("Status") or "").strip()
                    priority = (row.get("Priority") or "").strip()
                    notes = []
                    for part in (row.get("Notes") or "").split(" | "):
                        part = part.strip()
                        if not part:
                            continue
                        m = re.match(r"^\[(.*?)\]\s*(.*)$", part)
                        notes.append({"time": m.group(1), "text": m.group(2)} if m
                                     else {"time": "", "text": part})
                    if not notes:
                        notes.append({"time": now(), "text": "Imported from CSV"})

                    self.devices.append({
                        "id": self.next_id(),
                        "device": device,
                        "serial": serial,
                        "issue": issue,
                        "submitted": (row.get("Submitted By") or "").strip() or "Unknown",
                        "contact": (row.get("Contact") or "").strip(),
                        "priority": priority if priority in PRIORITIES else "Normal",
                        "status": status if status in STATUSES else "Pending",
                        "date_submitted": date_sub,
                        "date_repaired": (row.get("Date Closed") or row.get("Date Repaired") or "").strip(),
                        "notes": notes,
                    })
                    added += 1
        except Exception as e:
            messagebox.showerror("Import Error", f"Failed to import: {e}")
            return

        if added:
            self.save_data()
            self.filter_devices()
        self.status_var.set(f"Imported {added} ticket(s), skipped {skipped}")
        messagebox.showinfo("Import Finished",
                            f"Imported {added} ticket(s).\nSkipped {skipped} (duplicates or missing device/issue).")

    # ------------------------------------------------------------------
    # Dashboard
    # ------------------------------------------------------------------
    def show_dashboard(self):
        if self.dashboard_win is not None and self.dashboard_win.winfo_exists():
            self.dashboard_win.destroy()

        dashboard = tk.Toplevel(self.root)
        self.dashboard_win = dashboard
        dashboard.title("QueueRepair - Dashboard")
        dashboard.configure(bg=BG)
        self.center_window(dashboard, 680, 780)

        total = len(self.devices)
        counts = Counter(d['status'] for d in self.devices)
        open_tickets = [d for d in self.devices if d['status'] not in CLOSED]

        tk.Label(dashboard, text="Dashboard", font=("Arial", 18, "bold"), bg=BG).pack(pady=10)

        # Headline numbers
        stats_frame = tk.Frame(dashboard, bg=BG, relief="raised", bd=2)
        stats_frame.pack(fill=tk.X, padx=20, pady=8)
        stats = [("Total", total, "#333333")] + [(s, counts[s], STATUS_COLORS[s]) for s in STATUSES]
        for i, (label, value, color) in enumerate(stats):
            frame = tk.Frame(stats_frame, bg=BG)
            frame.grid(row=0, column=i, padx=14, pady=8)
            tk.Label(frame, text=label, bg=BG, font=("Arial", 10)).pack()
            tk.Label(frame, text=str(value), bg=BG, font=("Arial", 20, "bold"), fg=color).pack()

        # Status distribution
        dist_frame = tk.Frame(dashboard, bg=BG, relief="raised", bd=2)
        dist_frame.pack(fill=tk.X, padx=20, pady=8)
        tk.Label(dist_frame, text="Status Distribution", font=("Arial", 12, "bold"), bg=BG).pack(pady=6)
        if total > 0:
            bar_container = tk.Frame(dist_frame, bg=BG)
            bar_container.pack(fill=tk.X, padx=20, pady=6)
            for status in STATUSES:
                count = counts[status]
                if count > 0:
                    percentage = (count / total) * 100
                    row = tk.Frame(bar_container, bg=BG)
                    row.pack(fill=tk.X, pady=3)
                    tk.Label(row, text=f"{status}:", bg=BG, width=12, anchor="w").pack(side=tk.LEFT)
                    tk.Frame(row, bg=STATUS_COLORS[status], height=18,
                             width=max(int(percentage * 3), 3)).pack(side=tk.LEFT, padx=5)
                    tk.Label(row, text=f"{count} ({percentage:.1f}%)", bg=BG).pack(side=tk.LEFT)
        else:
            tk.Label(dist_frame, text="No data available", bg=BG, font=("Arial", 12)).pack(pady=15)

        # Insights
        insight_frame = tk.Frame(dashboard, bg=BG, relief="raised", bd=2)
        insight_frame.pack(fill=tk.X, padx=20, pady=8)
        tk.Label(insight_frame, text="Insights", font=("Arial", 12, "bold"), bg=BG).pack(pady=6)
        grid = tk.Frame(insight_frame, bg=BG)
        grid.pack(fill=tk.X, padx=20, pady=(0, 8))

        repair_times = []
        for d in self.devices:
            if d['status'] == "Repaired":
                s, e = parse_date(d.get('date_submitted')), parse_date(d.get('date_repaired'))
                if s and e and e >= s:
                    repair_times.append(e - s)
        avg_repair = (fmt_duration(sum(repair_times, timedelta()) / len(repair_times))
                      if repair_times else "n/a")

        aged = [(ticket_age_days(d), d) for d in open_tickets if ticket_age_days(d) is not None]
        if aged:
            days, oldest = max(aged, key=lambda x: x[0])
            oldest_text = f"#{oldest['id']} {oldest['device']} ({days} days)"
        else:
            oldest_text = "n/a"
        overdue = len([1 for days, _ in aged if days >= OVERDUE_DAYS])

        open_by_prio = Counter(d['priority'] for d in open_tickets)
        prio_text = "  ".join(f"{p}: {open_by_prio[p]}" for p in reversed(PRIORITIES))

        submitters = Counter(d['submitted'] for d in self.devices if d['submitted'] != "Unknown")
        top_text = ", ".join(f"{name} ({n})" for name, n in submitters.most_common(3)) or "n/a"

        repaired_rate = f"{counts['Repaired'] / total * 100:.0f}%" if total else "n/a"

        insights = [
            ("Average repair time:", avg_repair),
            ("Repair success rate:", repaired_rate),
            ("Oldest open ticket:", oldest_text),
            (f"Open {OVERDUE_DAYS}+ days:", str(overdue)),
            ("Open by priority:", prio_text),
            ("Top submitters:", top_text),
        ]
        for r, (label, value) in enumerate(insights):
            tk.Label(grid, text=label, bg=BG, font=("Arial", 10, "bold"), anchor="w").grid(
                row=r, column=0, sticky="w", pady=2)
            tk.Label(grid, text=value, bg=BG, font=("Arial", 10), anchor="w").grid(
                row=r, column=1, sticky="w", padx=12, pady=2)

        # Recent activity
        recent_frame = tk.Frame(dashboard, bg=BG, relief="raised", bd=2)
        recent_frame.pack(fill=tk.BOTH, expand=True, padx=20, pady=8)
        tk.Label(recent_frame, text="Recent Activity", font=("Arial", 12, "bold"), bg=BG).pack(pady=5)
        recent_list = tk.Listbox(recent_frame, height=6)
        recent_list.pack(fill=tk.BOTH, expand=True, padx=10, pady=8)
        for d in sorted(self.devices, key=lambda x: x.get('date_submitted', ''), reverse=True)[:10]:
            issue = d['issue'] if len(d['issue']) <= 30 else d['issue'][:30] + "..."
            recent_list.insert(tk.END, f"[{d['status']}] {d['device']} - {issue}")

        ttk.Button(dashboard, text="Close", command=dashboard.destroy).pack(pady=8)
        dashboard.bind("<Escape>", lambda e: dashboard.destroy())


if __name__ == "__main__":
    root = tk.Tk()
    app = QueueRepairApp(root)
    root.mainloop()
