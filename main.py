import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import sqlite3
import csv
import os
import shutil
import re
import threading
from datetime import datetime, date, timedelta
from collections import defaultdict
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders

try:
    import matplotlib
    matplotlib.use("TkAgg")
    import matplotlib.pyplot as plt
    from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
    from matplotlib.figure import Figure
    MATPLOTLIB_AVAILABLE = True
except ImportError:
    MATPLOTLIB_AVAILABLE = False

# ─────────────────────────────────────────────
#  COLOR PALETTE  (Dark Slate + Cyan Accent)
# ─────────────────────────────────────────────
BG_DARK    = "#0D1117"
BG_PANEL   = "#161B22"
BG_CARD    = "#1C2330"
BG_INPUT   = "#21262D"
BORDER     = "#30363D"
ACCENT     = "#00C9A7"
ACCENT2    = "#0EA5E9"
WARN       = "#F59E0B"
DANGER     = "#EF4444"
SUCCESS    = "#22C55E"
TEXT_PRI   = "#E6EDF3"
TEXT_SEC   = "#8B949E"
TEXT_DIM   = "#484F58"
SEL_BG     = "#1D3A5F"

FONT_HEAD  = ("Segoe UI", 22, "bold")
FONT_SUB   = ("Segoe UI", 11, "bold")
FONT_BODY  = ("Segoe UI", 10)
FONT_SMALL = ("Segoe UI", 9)
FONT_MONO  = ("Consolas", 10)

DB_FILE  = "attendance.db"
CSV_FILE = "attendance.csv"
BACKUP_DIR = "backups"


# ══════════════════════════════════════════════
#  DATABASE LAYER
# ══════════════════════════════════════════════
class Database:
    def __init__(self, db_path=DB_FILE):
        self.db_path = db_path
        self.conn = sqlite3.connect(db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_tables()

    def _create_tables(self):
        cur = self.conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS persons (
                id       TEXT PRIMARY KEY,
                name     TEXT NOT NULL,
                dept     TEXT DEFAULT '',
                created  TEXT DEFAULT (date('now'))
            );
            CREATE TABLE IF NOT EXISTS attendance (
                rec_id   INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id TEXT NOT NULL,
                name      TEXT NOT NULL,
                date      TEXT NOT NULL,
                status    TEXT NOT NULL CHECK(status IN ('Present','Absent','Late')),
                note      TEXT DEFAULT '',
                ts        TEXT DEFAULT (datetime('now','localtime')),
                UNIQUE(person_id, date)
            );
        """)
        self.conn.commit()

    # ── Persons ──────────────────────────────
    def upsert_person(self, pid, name, dept=""):
        pid = pid.strip().upper()
        name = name.strip()
        self.conn.execute(
            "INSERT INTO persons(id,name,dept) VALUES(?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, dept=excluded.dept",
            (pid, name, dept))
        self.conn.commit()

    def get_all_persons(self):
        return self.conn.execute(
            "SELECT id,name,dept FROM persons ORDER BY id").fetchall()

    def person_exists(self, pid):
        r = self.conn.execute(
            "SELECT 1 FROM persons WHERE id=?", (pid.strip().upper(),)).fetchone()
        return r is not None

    # ── Attendance ────────────────────────────
    def mark(self, pid, name, att_date, status, note=""):
        pid  = pid.strip().upper()
        name = name.strip()
        try:
            self.conn.execute(
                "INSERT INTO attendance(person_id,name,date,status,note) "
                "VALUES(?,?,?,?,?)",
                (pid, name, att_date, status, note))
            self.conn.commit()
            self.upsert_person(pid, name)
            return True, "Attendance marked successfully."
        except sqlite3.IntegrityError:
            return False, f"Attendance for {pid} on {att_date} already exists."

    def update(self, rec_id, status, note=""):
        self.conn.execute(
            "UPDATE attendance SET status=?, note=? WHERE rec_id=?",
            (status, note, rec_id))
        self.conn.commit()

    def delete(self, rec_id):
        self.conn.execute("DELETE FROM attendance WHERE rec_id=?", (rec_id,))
        self.conn.commit()

    def search(self, pid=None, att_date=None, name_like=None,
               status=None, month=None):
        q  = ("SELECT rec_id, person_id, name, date, status, note, ts "
              "FROM attendance WHERE 1=1")
        params = []
        if pid:
            q += " AND UPPER(person_id)=UPPER(?)"
            params.append(pid.strip())
        if att_date:
            q += " AND date=?"
            params.append(att_date)
        if name_like:
            q += " AND UPPER(name) LIKE UPPER(?)"
            params.append(f"%{name_like.strip()}%")
        if status:
            q += " AND status=?"
            params.append(status)
        if month:          # "YYYY-MM"
            q += " AND strftime('%Y-%m', date)=?"
            params.append(month)
        q += " ORDER BY date DESC, person_id"
        return self.conn.execute(q, params).fetchall()

    def get_record(self, rec_id):
        return self.conn.execute(
            "SELECT * FROM attendance WHERE rec_id=?", (rec_id,)).fetchone()

    # ── Reports ───────────────────────────────
    def summary(self, pid=None, month=None):
        """Returns list of (person_id, name, present, absent, late, total, pct)"""
        where, params = "WHERE 1=1", []
        if pid:
            where += " AND UPPER(person_id)=UPPER(?)"
            params.append(pid.strip())
        if month:
            where += " AND strftime('%Y-%m', date)=?"
            params.append(month)
        rows = self.conn.execute(f"""
            SELECT person_id, name,
                   SUM(status='Present') AS present,
                   SUM(status='Absent')  AS absent,
                   SUM(status='Late')    AS late,
                   COUNT(*)              AS total
            FROM attendance {where}
            GROUP BY person_id
            ORDER BY person_id
        """, params).fetchall()
        result = []
        for r in rows:
            pct = round((r["present"] + r["late"]) / r["total"] * 100, 1) if r["total"] else 0
            result.append({
                "id": r["person_id"], "name": r["name"],
                "present": r["present"], "absent": r["absent"],
                "late": r["late"], "total": r["total"], "pct": pct
            })
        return result

    def close(self):
        self.conn.close()


# ══════════════════════════════════════════════
#  CSV SYNC
# ══════════════════════════════════════════════
class CSVManager:
    HEADERS = ["rec_id","person_id","name","date","status","note","timestamp"]

    def __init__(self, path=CSV_FILE):
        self.path = path
        if not os.path.exists(path):
            with open(path, "w", newline="") as f:
                csv.writer(f).writerow(self.HEADERS)

    def sync_from_db(self, db: Database):
        rows = db.search()
        with open(self.path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(self.HEADERS)
            for r in rows:
                w.writerow([r["rec_id"], r["person_id"], r["name"],
                             r["date"], r["status"], r["note"], r["ts"]])

    def export_report(self, summary_rows, filepath):
        headers = ["ID","Name","Present","Absent","Late","Total","Attendance%"]
        with open(filepath, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(headers)
            for d in summary_rows:
                w.writerow([d["id"], d["name"], d["present"],
                             d["absent"], d["late"], d["total"], d["pct"]])

    def backup(self):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(BACKUP_DIR, f"attendance_backup_{ts}.csv")
        shutil.copy2(self.path, dst)
        return dst

    def backup_db(self, db_path=DB_FILE):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        ts  = datetime.now().strftime("%Y%m%d_%H%M%S")
        dst = os.path.join(BACKUP_DIR, f"attendance_backup_{ts}.db")
        shutil.copy2(db_path, dst)
        return dst


# ══════════════════════════════════════════════
#  HELPERS / VALIDATORS
# ══════════════════════════════════════════════
def valid_date(s):
    try:
        datetime.strptime(s, "%Y-%m-%d")
        return True
    except ValueError:
        return False

def valid_id(s):
    return bool(re.match(r'^[A-Za-z0-9_\-]{1,20}$', s.strip()))

def today_str():
    return date.today().strftime("%Y-%m-%d")

def this_month():
    return date.today().strftime("%Y-%m")


# ══════════════════════════════════════════════
#  STYLED WIDGET HELPERS
# ══════════════════════════════════════════════
def styled_btn(parent, text, cmd, color=ACCENT, fg=BG_DARK, width=18):
    btn = tk.Button(
        parent, text=text, command=cmd,
        bg=color, fg=fg, activebackground=color, activeforeground=fg,
        font=FONT_SUB, bd=0, padx=12, pady=7,
        cursor="hand2", width=width, relief="flat"
    )
    def on_enter(e): btn.config(bg=_lighten(color))
    def on_leave(e): btn.config(bg=color)
    btn.bind("<Enter>", on_enter)
    btn.bind("<Leave>", on_leave)
    return btn

def _lighten(hex_color):
    """Slightly lighten a hex color."""
    h = hex_color.lstrip("#")
    rgb = tuple(int(h[i:i+2], 16) for i in (0,2,4))
    lighter = tuple(min(255, int(c * 1.18)) for c in rgb)
    return "#{:02X}{:02X}{:02X}".format(*lighter)

def lbl(parent, text, font=FONT_BODY, fg=TEXT_PRI, bg=None, **kw):
    return tk.Label(parent, text=text, font=font,
                    fg=fg, bg=bg or BG_PANEL, **kw)

def entry_field(parent, textvariable=None, width=24, **kw):
    e = tk.Entry(parent, textvariable=textvariable,
                 bg=BG_INPUT, fg=TEXT_PRI,
                 insertbackground=ACCENT, relief="flat",
                 font=FONT_BODY, bd=0, highlightthickness=1,
                 highlightcolor=ACCENT, highlightbackground=BORDER,
                 width=width, **kw)
    return e

def combo_field(parent, values, textvariable=None, width=22):
    style = ttk.Style()
    style.theme_use("clam")
    style.configure("Dark.TCombobox",
                     fieldbackground=BG_INPUT, background=BG_INPUT,
                     foreground=TEXT_PRI, selectbackground=SEL_BG,
                     selectforeground=TEXT_PRI, arrowcolor=ACCENT,
                     bordercolor=BORDER, lightcolor=BG_INPUT,
                     darkcolor=BG_INPUT)
    c = ttk.Combobox(parent, values=values,
                     textvariable=textvariable,
                     style="Dark.TCombobox",
                     font=FONT_BODY, width=width, state="readonly")
    return c

def separator(parent, color=BORDER):
    return tk.Frame(parent, height=1, bg=color)

def section_title(parent, text):
    f = tk.Frame(parent, bg=BG_PANEL)
    tk.Label(f, text=text, font=("Segoe UI", 13, "bold"),
             fg=ACCENT, bg=BG_PANEL).pack(side="left")
    tk.Frame(f, height=2, bg=ACCENT).pack(
        side="left", fill="x", expand=True, padx=(10,0), pady=8)
    return f

def card(parent, **kw):
    return tk.Frame(parent, bg=BG_CARD,
                    highlightthickness=1, highlightbackground=BORDER, **kw)


# ══════════════════════════════════════════════
#  TREEVIEW HELPER
# ══════════════════════════════════════════════
def build_tree(parent, columns, col_widths=None, height=14):
    style = ttk.Style()
    style.configure("Dark.Treeview",
                     background=BG_CARD, foreground=TEXT_PRI,
                     fieldbackground=BG_CARD, rowheight=28,
                     font=FONT_BODY, borderwidth=0)
    style.configure("Dark.Treeview.Heading",
                     background=BG_INPUT, foreground=ACCENT,
                     font=FONT_SUB, relief="flat")
    style.map("Dark.Treeview",
              background=[("selected", SEL_BG)],
              foreground=[("selected", TEXT_PRI)])

    frame = tk.Frame(parent, bg=BG_DARK)
    tree  = ttk.Treeview(frame, columns=columns, show="headings",
                          style="Dark.Treeview", height=height)
    vsb = ttk.Scrollbar(frame, orient="vertical",   command=tree.yview)
    hsb = ttk.Scrollbar(frame, orient="horizontal", command=tree.xview)
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

    style.configure("Vertical.TScrollbar",
                     background=BG_INPUT, troughcolor=BG_DARK,
                     arrowcolor=ACCENT)
    style.configure("Horizontal.TScrollbar",
                     background=BG_INPUT, troughcolor=BG_DARK,
                     arrowcolor=ACCENT)

    for i, col in enumerate(columns):
        w = col_widths[i] if col_widths else 120
        tree.heading(col, text=col, anchor="w")
        tree.column(col, width=w, anchor="w", stretch=False)

    tree.grid(row=0, column=0, sticky="nsew")
    vsb.grid(row=0, column=1, sticky="ns")
    hsb.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return frame, tree


# ══════════════════════════════════════════════
#  POPUP DIALOGS
# ══════════════════════════════════════════════
class UpdateDialog(tk.Toplevel):
    def __init__(self, parent, rec, db, csv_mgr, refresh_cb):
        super().__init__(parent)
        self.db, self.csv_mgr, self.refresh_cb = db, csv_mgr, refresh_cb
        self.rec_id = rec["rec_id"]
        self.title("Update Record")
        self.configure(bg=BG_PANEL)
        self.resizable(False, False)
        self._build(rec)
        self.grab_set()

    def _build(self, rec):
        pad = dict(padx=18, pady=8)
        lbl(self, "Update Attendance Record",
            font=FONT_HEAD, fg=ACCENT).pack(**pad)
        separator(self).pack(fill="x", padx=18)

        info_f = tk.Frame(self, bg=BG_PANEL)
        info_f.pack(fill="x", **pad)
        for label, val in [("ID", rec["person_id"]),
                            ("Name", rec["name"]),
                            ("Date", rec["date"])]:
            row = tk.Frame(info_f, bg=BG_PANEL)
            row.pack(fill="x", pady=2)
            lbl(row, f"{label}:", fg=TEXT_SEC, width=8, anchor="w").pack(side="left")
            lbl(row, val, fg=TEXT_PRI, font=FONT_SUB).pack(side="left")

        separator(self).pack(fill="x", padx=18)

        form = tk.Frame(self, bg=BG_PANEL)
        form.pack(fill="x", **pad)

        lbl(form, "Status:", fg=TEXT_SEC).grid(row=0, column=0, sticky="w", pady=6)
        self.status_var = tk.StringVar(value=rec["status"])
        combo_field(form, ["Present","Absent","Late"],
                    self.status_var, width=18).grid(row=0, column=1, padx=8)

        lbl(form, "Note:", fg=TEXT_SEC).grid(row=1, column=0, sticky="w", pady=6)
        self.note_var = tk.StringVar(value=rec["note"] or "")
        entry_field(form, self.note_var, width=24).grid(row=1, column=1, padx=8)

        btn_f = tk.Frame(self, bg=BG_PANEL)
        btn_f.pack(pady=14)
        styled_btn(btn_f, "Save Changes", self._save, ACCENT, width=14).pack(side="left", padx=6)
        styled_btn(btn_f, "Cancel", self.destroy, BG_INPUT, TEXT_PRI, width=10).pack(side="left")

    def _save(self):
        self.db.update(self.rec_id, self.status_var.get(), self.note_var.get())
        self.csv_mgr.sync_from_db(self.db)
        self.refresh_cb()
        self.destroy()
        messagebox.showinfo("Success", "Record updated successfully.", parent=self.master)


class EmailDialog(tk.Toplevel):
    def __init__(self, parent, attachment_path):
        super().__init__(parent)
        self.attachment = attachment_path
        self.title("Send Report via Email")
        self.configure(bg=BG_PANEL)
        self.resizable(False, False)
        self._build()
        self.grab_set()

    def _build(self):
        pad = dict(padx=20, pady=8)
        lbl(self, "Email Report", font=FONT_HEAD, fg=ACCENT).pack(**pad)
        separator(self).pack(fill="x", padx=20)

        form = tk.Frame(self, bg=BG_PANEL)
        form.pack(fill="x", padx=20, pady=10)

        fields = [
            ("SMTP Server",  "smtp_server",  "smtp.gmail.com"),
            ("SMTP Port",    "smtp_port",    "587"),
            ("Sender Email", "sender_email", ""),
            ("App Password", "sender_pass",  ""),
            ("Recipient",    "to_email",     ""),
            ("Subject",      "subject",      "Attendance Report"),
        ]
        self.vars = {}
        for i, (label, key, default) in enumerate(fields):
            lbl(form, f"{label}:", fg=TEXT_SEC, width=14,
                anchor="w").grid(row=i, column=0, sticky="w", pady=4)
            var = tk.StringVar(value=default)
            self.vars[key] = var
            show = "*" if key == "sender_pass" else None
            kw = {"show": show} if show else {}
            entry_field(form, var, width=28, **kw).grid(row=i, column=1, padx=8)

        lbl(form, "Body:", fg=TEXT_SEC, width=14,
            anchor="w").grid(row=len(fields), column=0, sticky="nw", pady=4)
        self.body_text = tk.Text(form, bg=BG_INPUT, fg=TEXT_PRI,
                                  insertbackground=ACCENT, font=FONT_BODY,
                                  width=32, height=4, bd=0,
                                  highlightthickness=1,
                                  highlightbackground=BORDER,
                                  highlightcolor=ACCENT)
        self.body_text.insert("1.0", "Please find the attached attendance report.")
        self.body_text.grid(row=len(fields), column=1, padx=8, pady=4)

        self.status_lbl = lbl(self, "", fg=TEXT_SEC, font=FONT_SMALL)
        self.status_lbl.pack()

        btn_f = tk.Frame(self, bg=BG_PANEL)
        btn_f.pack(pady=14)
        styled_btn(btn_f, "Send Email", self._send, ACCENT, width=14).pack(side="left", padx=6)
        styled_btn(btn_f, "Cancel", self.destroy, BG_INPUT, TEXT_PRI, width=10).pack(side="left")

    def _send(self):
        v = {k: var.get().strip() for k, var in self.vars.items()}
        body = self.body_text.get("1.0", "end").strip()
        if not all([v["smtp_server"], v["smtp_port"], v["sender_email"],
                    v["sender_pass"], v["to_email"]]):
            messagebox.showwarning("Missing Fields", "Please fill all required fields.")
            return
        self.status_lbl.config(text="Sending…", fg=WARN)
        self.update()
        threading.Thread(target=self._do_send, args=(v, body), daemon=True).start()

    def _do_send(self, v, body):
        try:
            msg = MIMEMultipart()
            msg["From"]    = v["sender_email"]
            msg["To"]      = v["to_email"]
            msg["Subject"] = v["subject"]
            msg.attach(MIMEText(body, "plain"))

            with open(self.attachment, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            part.add_header("Content-Disposition",
                             f"attachment; filename={os.path.basename(self.attachment)}")
            msg.attach(part)

            with smtplib.SMTP(v["smtp_server"], int(v["smtp_port"])) as s:
                s.starttls()
                s.login(v["sender_email"], v["sender_pass"])
                s.send_message(msg)

            self.after(0, lambda: self._done(True))
        except Exception as ex:
            self.after(0, lambda: self._done(False, str(ex)))

    def _done(self, ok, err=""):
        if ok:
            self.status_lbl.config(text="Email sent successfully!", fg=SUCCESS)
            self.after(1500, self.destroy)
        else:
            self.status_lbl.config(text=f"Error: {err}", fg=DANGER)


# ══════════════════════════════════════════════
#  MAIN APPLICATION
# ══════════════════════════════════════════════
class AttendanceApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Attendance Management System")
        self.geometry("1200x760")
        self.minsize(1000, 650)
        self.configure(bg=BG_DARK)
        self.db      = Database()
        self.csv_mgr = CSVManager()
        self._setup_style()
        self._build_layout()
        self._show_page("mark")

    # ── Style ─────────────────────────────────
    def _setup_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TNotebook", background=BG_DARK, borderwidth=0)
        style.configure("TNotebook.Tab", background=BG_PANEL,
                         foreground=TEXT_SEC, font=FONT_BODY, padding=(14,6))
        style.map("TNotebook.Tab",
                   background=[("selected", BG_CARD)],
                   foreground=[("selected", ACCENT)])

    # ── Layout ────────────────────────────────
    def _build_layout(self):
        # ── Header ───────────────────────────
        header = tk.Frame(self, bg=BG_PANEL, height=56)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="ATTENDANCE MANAGEMENT SYSTEM",
                 font=("Segoe UI", 15, "bold"), fg=ACCENT,
                 bg=BG_PANEL).pack(side="left", padx=24, pady=12)
        self.clock_lbl = tk.Label(header, text="", font=FONT_BODY,
                                   fg=TEXT_SEC, bg=BG_PANEL)
        self.clock_lbl.pack(side="right", padx=20)
        self._tick()

        # ── Sidebar + Content ─────────────────
        body = tk.Frame(self, bg=BG_DARK)
        body.pack(fill="both", expand=True)

        # sidebar
        self.sidebar = tk.Frame(body, bg=BG_PANEL, width=200)
        self.sidebar.pack(side="left", fill="y")
        self.sidebar.pack_propagate(False)
        self._build_sidebar()

        # content area
        self.content = tk.Frame(body, bg=BG_DARK)
        self.content.pack(side="left", fill="both", expand=True)

        # Pages dict
        self.pages = {}
        for name, cls in [
            ("mark",    MarkPage),
            ("view",    ViewPage),
            ("update",  UpdatePage),
            ("report",  ReportPage),
            ("backup",  BackupPage),
        ]:
            p = cls(self.content, self.db, self.csv_mgr, self)
            p.place(relwidth=1, relheight=1)
            self.pages[name] = p

    def _build_sidebar(self):
        tk.Label(self.sidebar, text="MENU",
                 font=("Segoe UI", 9, "bold"), fg=TEXT_DIM,
                 bg=BG_PANEL).pack(pady=(20,6), padx=16, anchor="w")

        items = [
            ("mark",   "Mark Attendance"),
            ("view",   "View Records"),
            ("update", "Update Records"),
            ("report", "Generate Report"),
            ("backup", "Backup & Tools"),
        ]
        self.nav_btns = {}
        for key, label in items:
            btn = tk.Button(
                self.sidebar, text=label,
                bg=BG_PANEL, fg=TEXT_SEC,
                activebackground=BG_CARD, activeforeground=ACCENT,
                font=FONT_BODY, bd=0, pady=10, padx=20,
                anchor="w", cursor="hand2", relief="flat",
                command=lambda k=key: self._show_page(k)
            )
            btn.pack(fill="x")
            self.nav_btns[key] = btn

        separator(self.sidebar).pack(fill="x", pady=12, padx=16)
        lbl(self.sidebar, "v1.0  |  SQLite + CSV",
            font=FONT_SMALL, fg=TEXT_DIM).pack(side="bottom", pady=10)

    def _show_page(self, name):
        for k, b in self.nav_btns.items():
            if k == name:
                b.config(bg=BG_CARD, fg=ACCENT,
                         font=("Segoe UI", 10, "bold"))
            else:
                b.config(bg=BG_PANEL, fg=TEXT_SEC,
                         font=FONT_BODY)
        self.pages[name].lift()
        if hasattr(self.pages[name], "on_show"):
            self.pages[name].on_show()

    def _tick(self):
        now = datetime.now().strftime("%A, %d %b %Y   %H:%M:%S")
        self.clock_lbl.config(text=now)
        self.after(1000, self._tick)

    def on_closing(self):
        self.db.close()
        self.destroy()


# ══════════════════════════════════════════════
#  PAGE: MARK ATTENDANCE
# ══════════════════════════════════════════════
class MarkPage(tk.Frame):
    def __init__(self, parent, db, csv_mgr, app):
        super().__init__(parent, bg=BG_DARK)
        self.db, self.csv_mgr, self.app = db, csv_mgr, app
        self._build()

    def _build(self):
        wrap = tk.Frame(self, bg=BG_DARK, padx=32, pady=24)
        wrap.pack(fill="both", expand=True)

        section_title(wrap, "Mark Attendance").pack(fill="x", pady=(0,18))

        # Two-column layout
        cols = tk.Frame(wrap, bg=BG_DARK)
        cols.pack(fill="both", expand=True)

        # ── Left: Form ────────────────────────
        form_card = card(cols)
        form_card.pack(side="left", fill="both", expand=True, padx=(0,12))
        tk.Label(form_card, text="New Entry",
                 font=FONT_SUB, fg=TEXT_SEC, bg=BG_CARD).pack(
                 anchor="w", padx=16, pady=(14,4))
        separator(form_card, BORDER).pack(fill="x", padx=16)

        form = tk.Frame(form_card, bg=BG_CARD)
        form.pack(padx=20, pady=16, anchor="w")

        self.id_var     = tk.StringVar()
        self.name_var   = tk.StringVar()
        self.date_var   = tk.StringVar(value=today_str())
        self.status_var = tk.StringVar(value="Present")
        self.note_var   = tk.StringVar()

        fields = [
            ("Student / Employee ID *", self.id_var,     None),
            ("Full Name *",             self.name_var,   None),
            ("Date (YYYY-MM-DD) *",     self.date_var,   None),
            ("Note (optional)",         self.note_var,   None),
        ]
        for i, (lbl_txt, var, _) in enumerate(fields):
            tk.Label(form, text=lbl_txt, font=FONT_SMALL,
                     fg=TEXT_SEC, bg=BG_CARD).grid(
                     row=i*2, column=0, sticky="w", pady=(8,2))
            entry_field(form, var, width=30).grid(
                row=i*2+1, column=0, sticky="w")

        tk.Label(form, text="Status *", font=FONT_SMALL,
                 fg=TEXT_SEC, bg=BG_CARD).grid(
                 row=len(fields)*2, column=0, sticky="w", pady=(8,2))
        status_frame = tk.Frame(form, bg=BG_CARD)
        status_frame.grid(row=len(fields)*2+1, column=0, sticky="w")
        for s, col in [("Present", SUCCESS), ("Absent", DANGER), ("Late", WARN)]:
            rb = tk.Radiobutton(
                status_frame, text=s, variable=self.status_var, value=s,
                bg=BG_CARD, fg=col, selectcolor=BG_INPUT,
                activebackground=BG_CARD, font=FONT_BODY,
                cursor="hand2")
            rb.pack(side="left", padx=(0,12))

        btn_row = tk.Frame(form_card, bg=BG_CARD)
        btn_row.pack(padx=20, pady=(0,16), anchor="w")
        styled_btn(btn_row, "Mark Attendance", self._submit,
                   ACCENT, width=18).pack(side="left", padx=(0,8))
        styled_btn(btn_row, "Clear Form", self._clear,
                   BG_INPUT, TEXT_PRI, width=12).pack(side="left")

        self.msg_lbl = tk.Label(form_card, text="", font=FONT_BODY,
                                 fg=SUCCESS, bg=BG_CARD)
        self.msg_lbl.pack(padx=20, pady=(0,10))

        # ── Right: Today's log ────────────────
        log_card = card(cols)
        log_card.pack(side="left", fill="both", expand=True)
        hdr = tk.Frame(log_card, bg=BG_CARD)
        hdr.pack(fill="x", padx=16, pady=(14,4))
        tk.Label(hdr, text="Today's Log", font=FONT_SUB,
                 fg=TEXT_SEC, bg=BG_CARD).pack(side="left")
        styled_btn(hdr, "Refresh", self._load_today,
                   BG_INPUT, TEXT_PRI, width=8).pack(side="right")

        separator(log_card, BORDER).pack(fill="x", padx=16)

        cols_spec = ["ID","Name","Status","Note"]
        widths     = [90, 160, 80, 140]
        tree_frame, self.today_tree = build_tree(
            log_card, cols_spec, widths, height=16)
        tree_frame.pack(fill="both", expand=True, padx=8, pady=8)
        self._load_today()

    def _load_today(self):
        self.today_tree.delete(*self.today_tree.get_children())
        rows = self.db.search(att_date=today_str())
        for r in rows:
            tag = r["status"].lower()
            self.today_tree.insert("", "end",
                values=(r["person_id"], r["name"], r["status"], r["note"]),
                tags=(tag,))
        self.today_tree.tag_configure("present", foreground=SUCCESS)
        self.today_tree.tag_configure("absent",  foreground=DANGER)
        self.today_tree.tag_configure("late",    foreground=WARN)

    def _submit(self):
        pid    = self.id_var.get().strip().upper()
        name   = self.name_var.get().strip()
        dt     = self.date_var.get().strip()
        status = self.status_var.get()
        note   = self.note_var.get().strip()

        if not pid or not name:
            self._show_msg("ID and Name are required.", DANGER); return
        if not valid_id(pid):
            self._show_msg("ID: letters, digits, _ or - only (max 20).", DANGER); return
        if not valid_date(dt):
            self._show_msg("Date must be YYYY-MM-DD format.", DANGER); return

        ok, msg = self.db.mark(pid, name, dt, status, note)
        if ok:
            self.csv_mgr.sync_from_db(self.db)
            self._show_msg(msg, SUCCESS)
            self._load_today()
        else:
            self._show_msg(msg, WARN)

    def _show_msg(self, msg, color):
        self.msg_lbl.config(text=msg, fg=color)
        self.after(4000, lambda: self.msg_lbl.config(text=""))

    def _clear(self):
        self.id_var.set("")
        self.name_var.set("")
        self.date_var.set(today_str())
        self.note_var.set("")
        self.status_var.set("Present")


# ══════════════════════════════════════════════
#  PAGE: VIEW RECORDS
# ══════════════════════════════════════════════
class ViewPage(tk.Frame):
    def __init__(self, parent, db, csv_mgr, app):
        super().__init__(parent, bg=BG_DARK)
        self.db, self.csv_mgr, self.app = db, csv_mgr, app
        self._build()

    def _build(self):
        wrap = tk.Frame(self, bg=BG_DARK, padx=32, pady=24)
        wrap.pack(fill="both", expand=True)
        section_title(wrap, "View Records").pack(fill="x", pady=(0,14))

        # ── Filter bar ────────────────────────
        flt = card(wrap)
        flt.pack(fill="x", pady=(0,12))
        f = tk.Frame(flt, bg=BG_CARD)
        f.pack(fill="x", padx=16, pady=12)

        self.f_id    = tk.StringVar()
        self.f_name  = tk.StringVar()
        self.f_date  = tk.StringVar()
        self.f_month = tk.StringVar()
        self.f_stat  = tk.StringVar()

        filters = [
            ("ID",          self.f_id,    None),
            ("Name",        self.f_name,  None),
            ("Date",        self.f_date,  None),
            ("Month YYYY-MM", self.f_month, None),
        ]
        for i, (lbl_txt, var, _) in enumerate(filters):
            tk.Label(f, text=lbl_txt, font=FONT_SMALL,
                     fg=TEXT_SEC, bg=BG_CARD).grid(
                     row=0, column=i*2, sticky="w", padx=(8,2))
            entry_field(f, var, width=14).grid(
                row=1, column=i*2, padx=(8,2))

        tk.Label(f, text="Status", font=FONT_SMALL,
                 fg=TEXT_SEC, bg=BG_CARD).grid(row=0, column=8, sticky="w", padx=8)
        combo_field(f, ["","Present","Absent","Late"],
                    self.f_stat, width=10).grid(row=1, column=8, padx=8)

        styled_btn(f, "Search", self._search, ACCENT, width=10).grid(
            row=1, column=10, padx=8)
        styled_btn(f, "All Records", self._load_all, ACCENT2, TEXT_PRI, width=10).grid(
            row=1, column=11, padx=4)
        styled_btn(f, "Clear", self._clear_filters, BG_INPUT, TEXT_PRI, width=8).grid(
            row=1, column=12, padx=4)

        # ── Tree ──────────────────────────────
        cols   = ["Rec ID","Person ID","Name","Date","Status","Note","Timestamp"]
        widths = [65, 90, 150, 100, 80, 160, 150]
        tree_frame, self.tree = build_tree(
            wrap, cols, widths, height=20)
        tree_frame.pack(fill="both", expand=True)

        self.count_lbl = lbl(wrap, "", font=FONT_SMALL, fg=TEXT_SEC, bg=BG_DARK)
        self.count_lbl.pack(anchor="e", pady=(4,0))
        self._load_all()

    def on_show(self):
        self._load_all()

    def _populate(self, rows):
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            tag = r["status"].lower()
            self.tree.insert("", "end",
                values=(r["rec_id"], r["person_id"], r["name"],
                        r["date"], r["status"], r["note"], r["ts"]),
                tags=(tag,))
        self.tree.tag_configure("present", foreground=SUCCESS)
        self.tree.tag_configure("absent",  foreground=DANGER)
        self.tree.tag_configure("late",    foreground=WARN)
        self.count_lbl.config(text=f"{len(rows)} record(s) found")

    def _search(self):
        rows = self.db.search(
            pid=self.f_id.get() or None,
            att_date=self.f_date.get() or None,
            name_like=self.f_name.get() or None,
            status=self.f_stat.get() or None,
            month=self.f_month.get() or None,
        )
        self._populate(rows)

    def _load_all(self):
        self._populate(self.db.search())

    def _clear_filters(self):
        for v in [self.f_id, self.f_name, self.f_date,
                  self.f_month, self.f_stat]:
            v.set("")
        self._load_all()


# ══════════════════════════════════════════════
#  PAGE: UPDATE RECORDS
# ══════════════════════════════════════════════
class UpdatePage(tk.Frame):
    def __init__(self, parent, db, csv_mgr, app):
        super().__init__(parent, bg=BG_DARK)
        self.db, self.csv_mgr, self.app = db, csv_mgr, app
        self._build()

    def _build(self):
        wrap = tk.Frame(self, bg=BG_DARK, padx=32, pady=24)
        wrap.pack(fill="both", expand=True)
        section_title(wrap, "Update / Delete Records").pack(fill="x", pady=(0,14))

        # search
        sf = card(wrap)
        sf.pack(fill="x", pady=(0,12))
        row = tk.Frame(sf, bg=BG_CARD)
        row.pack(padx=16, pady=12, fill="x")

        self.s_id   = tk.StringVar()
        self.s_date = tk.StringVar()
        tk.Label(row, text="Person ID:", font=FONT_SMALL,
                 fg=TEXT_SEC, bg=BG_CARD).pack(side="left", padx=(0,4))
        entry_field(row, self.s_id, width=14).pack(side="left", padx=(0,10))
        tk.Label(row, text="Date:", font=FONT_SMALL,
                 fg=TEXT_SEC, bg=BG_CARD).pack(side="left", padx=(0,4))
        entry_field(row, self.s_date, width=14).pack(side="left", padx=(0,10))
        styled_btn(row, "Search", self._search, ACCENT, width=10).pack(side="left", padx=4)
        styled_btn(row, "Load All", self._load_all, ACCENT2, TEXT_PRI, width=10).pack(side="left", padx=4)

        # tree
        cols   = ["Rec ID","Person ID","Name","Date","Status","Note"]
        widths = [65, 100, 160, 100, 80, 200]
        tf, self.tree = build_tree(wrap, cols, widths, height=16)
        tf.pack(fill="both", expand=True)

        # action buttons
        ab = tk.Frame(wrap, bg=BG_DARK)
        ab.pack(fill="x", pady=(10,0))
        styled_btn(ab, "Edit Selected", self._edit,
                   ACCENT2, TEXT_PRI, width=14).pack(side="left", padx=(0,8))
        styled_btn(ab, "Delete Selected", self._delete,
                   DANGER, TEXT_PRI, width=14).pack(side="left")
        self.info_lbl = lbl(ab, "", font=FONT_SMALL, fg=TEXT_SEC, bg=BG_DARK)
        self.info_lbl.pack(side="right")
        self._load_all()

    def on_show(self):
        self._load_all()

    def _populate(self, rows):
        self.tree.delete(*self.tree.get_children())
        for r in rows:
            self.tree.insert("", "end",
                iid=str(r["rec_id"]),
                values=(r["rec_id"], r["person_id"], r["name"],
                        r["date"], r["status"], r["note"]))
        self.info_lbl.config(text=f"{len(rows)} record(s)")

    def _search(self):
        rows = self.db.search(pid=self.s_id.get() or None,
                               att_date=self.s_date.get() or None)
        self._populate(rows)

    def _load_all(self):
        self._populate(self.db.search())

    def _selected_id(self):
        sel = self.tree.selection()
        return int(sel[0]) if sel else None

    def _edit(self):
        rid = self._selected_id()
        if rid is None:
            messagebox.showwarning("No Selection", "Please select a record to edit.")
            return
        rec = self.db.get_record(rid)
        UpdateDialog(self, rec, self.db, self.csv_mgr, self._load_all)

    def _delete(self):
        rid = self._selected_id()
        if rid is None:
            messagebox.showwarning("No Selection", "Please select a record to delete.")
            return
        if messagebox.askyesno("Confirm Delete",
                                f"Delete record ID {rid}? This cannot be undone."):
            self.db.delete(rid)
            self.csv_mgr.sync_from_db(self.db)
            self._load_all()
            messagebox.showinfo("Deleted", "Record deleted successfully.")


# ══════════════════════════════════════════════
#  PAGE: GENERATE REPORT
# ══════════════════════════════════════════════
class ReportPage(tk.Frame):
    def __init__(self, parent, db, csv_mgr, app):
        super().__init__(parent, bg=BG_DARK)
        self.db, self.csv_mgr, self.app = db, csv_mgr, app
        self._last_summary = []
        self._build()

    def _build(self):
        wrap = tk.Frame(self, bg=BG_DARK, padx=32, pady=24)
        wrap.pack(fill="both", expand=True)
        section_title(wrap, "Attendance Report").pack(fill="x", pady=(0,14))

        # filter row
        ff = card(wrap)
        ff.pack(fill="x", pady=(0,12))
        row = tk.Frame(ff, bg=BG_CARD)
        row.pack(padx=16, pady=12, fill="x")

        self.r_id    = tk.StringVar()
        self.r_month = tk.StringVar(value=this_month())

        tk.Label(row, text="Person ID (blank=all):", font=FONT_SMALL,
                 fg=TEXT_SEC, bg=BG_CARD).pack(side="left", padx=(0,4))
        entry_field(row, self.r_id, width=14).pack(side="left", padx=(0,10))
        tk.Label(row, text="Month (YYYY-MM):", font=FONT_SMALL,
                 fg=TEXT_SEC, bg=BG_CARD).pack(side="left", padx=(0,4))
        entry_field(row, self.r_month, width=10).pack(side="left", padx=(0,10))
        styled_btn(row, "Generate", self._generate, ACCENT, width=10).pack(side="left", padx=4)
        styled_btn(row, "All Time", self._all_time, ACCENT2, TEXT_PRI, width=10).pack(side="left", padx=4)
        styled_btn(row, "Export CSV", self._export, SUCCESS, BG_DARK, width=12).pack(side="left", padx=4)
        styled_btn(row, "Send Email", self._send_email, WARN, BG_DARK, width=12).pack(side="left", padx=4)

        # tree
        cols   = ["ID","Name","Present","Absent","Late","Total","Attendance %"]
        widths = [90, 180, 75, 75, 60, 60, 110]
        tf, self.tree = build_tree(wrap, cols, widths, height=14)
        tf.pack(fill="both", expand=True)

        # chart placeholder
        if MATPLOTLIB_AVAILABLE:
            self.chart_frame = tk.Frame(wrap, bg=BG_DARK, height=200)
            self.chart_frame.pack(fill="x", pady=(10,0))

        self._generate()

    def on_show(self):
        self._generate()

    def _generate(self):
        pid   = self.r_id.get().strip() or None
        month = self.r_month.get().strip() or None
        data  = self.db.summary(pid, month)
        self._populate(data)

    def _all_time(self):
        self.r_month.set("")
        data = self.db.summary(self.r_id.get().strip() or None, None)
        self._populate(data)

    def _populate(self, data):
        self._last_summary = data
        self.tree.delete(*self.tree.get_children())
        for d in data:
            tag = "good" if d["pct"] >= 75 else "warn" if d["pct"] >= 50 else "bad"
            self.tree.insert("", "end",
                values=(d["id"], d["name"], d["present"],
                        d["absent"], d["late"], d["total"],
                        f"{d['pct']}%"),
                tags=(tag,))
        self.tree.tag_configure("good", foreground=SUCCESS)
        self.tree.tag_configure("warn", foreground=WARN)
        self.tree.tag_configure("bad",  foreground=DANGER)

        if MATPLOTLIB_AVAILABLE and data:
            self._draw_chart(data)

    def _draw_chart(self, data):
        for w in self.chart_frame.winfo_children():
            w.destroy()

        fig = Figure(figsize=(12, 2.6), facecolor=BG_CARD)
        ax  = fig.add_subplot(111)
        ax.set_facecolor(BG_CARD)
        names = [d["name"][:12] for d in data]
        pcts  = [d["pct"] for d in data]
        colors = [SUCCESS if p >= 75 else WARN if p >= 50 else DANGER for p in pcts]
        bars = ax.bar(names, pcts, color=colors, width=0.55, zorder=3)
        ax.axhline(75, color=ACCENT, linewidth=1, linestyle="--", alpha=0.7)
        ax.set_ylim(0, 110)
        ax.set_ylabel("Attendance %", color=TEXT_SEC, fontsize=9)
        ax.tick_params(colors=TEXT_SEC, labelsize=8)
        ax.spines[['top','right','left','bottom']].set_color(BORDER)
        ax.yaxis.label.set_color(TEXT_SEC)
        for spine in ax.spines.values():
            spine.set_edgecolor(BORDER)
        for bar, pct in zip(bars, pcts):
            ax.text(bar.get_x() + bar.get_width()/2, bar.get_height() + 1,
                    f"{pct}%", ha="center", va="bottom",
                    fontsize=7, color=TEXT_PRI)
        fig.tight_layout(pad=0.5)

        canvas = FigureCanvasTkAgg(fig, master=self.chart_frame)
        canvas.draw()
        canvas.get_tk_widget().pack(fill="both", expand=True)

    def _export(self):
        if not self._last_summary:
            messagebox.showinfo("No Data", "Generate a report first.")
            return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv",
            filetypes=[("CSV files","*.csv")],
            initialfile=f"report_{this_month()}.csv")
        if path:
            self.csv_mgr.export_report(self._last_summary, path)
            messagebox.showinfo("Exported", f"Report saved to:\n{path}")

    def _send_email(self):
        if not self._last_summary:
            messagebox.showinfo("No Data", "Generate a report first.")
            return
        tmp = os.path.join(BACKUP_DIR, f"report_tmp_{today_str()}.csv")
        os.makedirs(BACKUP_DIR, exist_ok=True)
        self.csv_mgr.export_report(self._last_summary, tmp)
        EmailDialog(self, tmp)


# ══════════════════════════════════════════════
#  PAGE: BACKUP & TOOLS
# ══════════════════════════════════════════════
class BackupPage(tk.Frame):
    def __init__(self, parent, db, csv_mgr, app):
        super().__init__(parent, bg=BG_DARK)
        self.db, self.csv_mgr, self.app = db, csv_mgr, app
        self._build()

    def _build(self):
        wrap = tk.Frame(self, bg=BG_DARK, padx=32, pady=24)
        wrap.pack(fill="both", expand=True)
        section_title(wrap, "Backup & Tools").pack(fill="x", pady=(0,18))

        # Backup card
        bc = card(wrap)
        bc.pack(fill="x", pady=(0,14))
        tk.Label(bc, text="Auto / Manual Backup",
                 font=FONT_SUB, fg=TEXT_SEC, bg=BG_CARD).pack(
                 anchor="w", padx=16, pady=(12,4))
        separator(bc, BORDER).pack(fill="x", padx=16)
        bb = tk.Frame(bc, bg=BG_CARD)
        bb.pack(padx=16, pady=12, anchor="w")
        styled_btn(bb, "Backup CSV", self._bk_csv, ACCENT, width=14).pack(side="left", padx=(0,8))
        styled_btn(bb, "Backup Database", self._bk_db, ACCENT2, TEXT_PRI, width=16).pack(side="left", padx=(0,8))
        styled_btn(bb, "Backup Both", self._bk_both, SUCCESS, BG_DARK, width=14).pack(side="left")
        self.bk_msg = tk.Label(bc, text="", font=FONT_SMALL, fg=TEXT_SEC, bg=BG_CARD)
        self.bk_msg.pack(padx=16, pady=(0,10))

        # Database tools
        dc = card(wrap)
        dc.pack(fill="x", pady=(0,14))
        tk.Label(dc, text="Database Tools",
                 font=FONT_SUB, fg=TEXT_SEC, bg=BG_CARD).pack(
                 anchor="w", padx=16, pady=(12,4))
        separator(dc, BORDER).pack(fill="x", padx=16)
        db_row = tk.Frame(dc, bg=BG_CARD)
        db_row.pack(padx=16, pady=12, anchor="w")
        styled_btn(db_row, "Sync CSV from DB", self._sync,
                   ACCENT, width=16).pack(side="left", padx=(0,8))
        styled_btn(db_row, "Import CSV to DB", self._import_csv,
                   WARN, BG_DARK, width=16).pack(side="left")

        # Backup log
        lc = card(wrap)
        lc.pack(fill="both", expand=True)
        tk.Label(lc, text="Backup History",
                 font=FONT_SUB, fg=TEXT_SEC, bg=BG_CARD).pack(
                 anchor="w", padx=16, pady=(12,4))
        separator(lc, BORDER).pack(fill="x", padx=16)
        cols   = ["Filename","Size","Modified"]
        widths = [280, 100, 200]
        tf, self.bk_tree = build_tree(lc, cols, widths, height=10)
        tf.pack(fill="both", expand=True, padx=8, pady=8)
        styled_btn(lc, "Refresh List", self._load_backups,
                   BG_INPUT, TEXT_PRI, width=14).pack(
                   anchor="w", padx=16, pady=(0,12))
        self._load_backups()

    def on_show(self):
        self._load_backups()

    def _show_bk(self, msg):
        self.bk_msg.config(text=msg, fg=SUCCESS)
        self.after(4000, lambda: self.bk_msg.config(text=""))
        self._load_backups()

    def _bk_csv(self):
        p = self.csv_mgr.backup()
        self._show_bk(f"CSV backed up: {p}")

    def _bk_db(self):
        p = self.csv_mgr.backup_db()
        self._show_bk(f"DB backed up: {p}")

    def _bk_both(self):
        p1 = self.csv_mgr.backup()
        p2 = self.csv_mgr.backup_db()
        self._show_bk(f"Backed up: {os.path.basename(p1)}, {os.path.basename(p2)}")

    def _sync(self):
        self.csv_mgr.sync_from_db(self.db)
        messagebox.showinfo("Sync", "CSV synchronized from database.")

    def _import_csv(self):
        path = filedialog.askopenfilename(
            filetypes=[("CSV files","*.csv")])
        if not path:
            return
        count, errors = 0, 0
        with open(path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                try:
                    ok, _ = self.db.mark(
                        row.get("person_id",""),
                        row.get("name",""),
                        row.get("date",""),
                        row.get("status","Present"),
                        row.get("note",""))
                    if ok: count += 1
                    else:  errors += 1
                except Exception:
                    errors += 1
        self.csv_mgr.sync_from_db(self.db)
        messagebox.showinfo("Import Complete",
                             f"Imported: {count} records\nSkipped: {errors}")

    def _load_backups(self):
        self.bk_tree.delete(*self.bk_tree.get_children())
        if not os.path.isdir(BACKUP_DIR):
            return
        files = sorted(os.listdir(BACKUP_DIR), reverse=True)
        for fn in files:
            fp = os.path.join(BACKUP_DIR, fn)
            sz = f"{os.path.getsize(fp):,} B"
            mt = datetime.fromtimestamp(
                os.path.getmtime(fp)).strftime("%Y-%m-%d %H:%M:%S")
            self.bk_tree.insert("", "end", values=(fn, sz, mt))


# ══════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════
if __name__ == "__main__":
    app = AttendanceApp()
    app.protocol("WM_DELETE_WINDOW", app.on_closing)
    app.mainloop()