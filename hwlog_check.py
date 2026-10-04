#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hwlog_check.py - Auswertung von HWiNFO- und LibreHardwareMonitor-Sensorlogs (CSV)

Prueft HWiNFO- und LHM-CSV-Logs auf Stabilitaets- und Hardwareauffaelligkeiten (WHEA,
PCIe-Fehlerzaehler, Netzteilschienen, CPU-/RAM-Spannungen, Temperaturen,
RAM-Module/Kanaele, PMIC-Flags, SMART, Drosselung, Luefter, Log-Luecken,
Frametime-Spitzen, Logende ohne Abschluss) und erzeugt einen interaktiven
HTML-Bericht. Unter Windows werden passende Eintraege aus dem
Ereignisprotokoll zugeordnet (Kernel-Power 41, BugCheck, WHEA-Logger, TDR ...).
Jede Auswertung landet im Verlauf (verlauf.jsonl + verlauf.html), damit sich
Sitzungen vergleichen lassen und Konfigurationsaenderungen (EXPO, SoC-Spannung
usw.) automatisch auffallen.

Nur Python-Standardbibliothek (ab 3.8), keine Zusatzpakete.

Beispiele:
  py hwlog_check.py gaming.CSV --oeffnen
  py hwlog_check.py D:\\HWiNFO-Logs --neu
  py hwlog_check.py "C:\\Program Files\\LibreHardwareMonitor" --neu
  py hwlog_check.py --config-schreiben hwlog_config.json
"""
from __future__ import annotations

import argparse
import base64
import bisect
import csv
import datetime as dt
import hashlib
import html
import json
import math
import os
import re
import socket
import subprocess
import sys
import time
import webbrowser
from array import array
from dataclasses import dataclass, field

VERSION = "1.8.1"
NAN = float("nan")
LEVELS = {0: "Info", 1: "Hinweis", 2: "Warnung", 3: "Kritisch"}
LEVEL_ICON = {0: "i", 1: "!", 2: "\u25B2", 3: "\u2716"}
LEVEL_CSS = {0: "info", 1: "warning", 2: "serious", 3: "critical"}
DASH = "\u2013"
SEP = " \u00b7 "

# --------------------------------------------------------------------------
# Konfiguration (per --config als JSON ueberschreibbar)
# --------------------------------------------------------------------------
DEFAULT_CONFIG = {
    "erwartete_riegel": 2,
    # erwarteter Arbeitsspeicher in GB (0 = automatisch: gr\u00f6\u00dfter bisher von Windows gemeldeter Wert)
    "ram_gb_erwartet": 0,
    # Abweichung zwischen den RAM-Modulen im selben Log: [Hinweis, Warnung, Kritisch]
    # (VIN/VDD in V, Temperatur in \u00b0C) \u2013 ein Modul, das vom anderen wegdriftet, ist das Fr\u00fchwarnzeichen
    "ram_abweichung": {"vin": [0.08, 0.15, 0.30], "vdd": [0.03, 0.06, 0.10], "temp": [8, 15, 25]},
    # Temperatur-Grenzwerte in Grad C: [Hinweis, Warnung, Kritisch]
    "temperaturen": {
        "cpu": [85, 89, 95],
        # X3D-Modelle (z. B. 7800X3D) drosseln schon bei 89 \u00b0C -> Kritisch = am Limit
        "cpu_x3d": [80, 85, 89],
        "gpu_kern": [80, 87, 90],
        "gpu_hotspot": [95, 105, 110],
        "gpu_speicher": [90, 100, 105],
        "ram": [65, 75, 85],
        "nvme": [70, 80, 85],
        "sata": [55, 65, 70],
        "vrm": [90, 105, 115],
        "chipsatz": [75, 85, 95],
    },
    # Abweichung der Netzteilschienen vom Sollwert in %: [Hinweis, Warnung, Kritisch]
    "schienen_toleranz_prozent": [3, 4, 5],
    "spannungen": {
        # EXPO-Profile setzen VDDCR_SOC oft auf genau 1,25 V; AMD-Obergrenze 1,30 V
        "soc_max": [1.28, 1.30, 1.35],
        "vcore_max": [1.45, 1.50, 1.55],
        "vcore_max_x3d": [1.30, 1.35, 1.40],
        # EXPO DDR5-6000 CL30 l\u00e4uft mit 1,40 V, PMIC-Messwerte liegen oft 0,01\u20130,02 V dar\u00fcber
        "ram_max": [1.43, 1.45, 1.50],
        "ram_vin_min": [4.75, 4.60, 4.40],
        "cmos_batterie_min": [2.90, 2.70, 2.50],
    },
    "ssd_restlebensdauer_min": [20, 10, 3],
    "frametime_spitze_ms": 100,
    # LHM-Tagesdateien aelter als N Tage beim Aufruf mit einem Ordner loeschen (0 = nie)
    "lhm_aufbewahrung_tage": 0,
    # fester Ordner fuer Berichte und Verlauf (leer = neben dem Log bzw. Dokumente\hwlog_berichte fuer LHM)
    "ausgabeordner": "",
    "log_luecke_faktor": 3.0,
    "log_luecke_warnung_s": 10,
    "lastgrenzen": {"gpu_prozent": 80, "cpu_prozent": 60},
    # chronik_tage: wie weit die Ereignis-Chronik beim ersten Lauf zur\u00fcckschaut
    "ereignisse": {"aktiv": True, "minuten_vor_start": 2, "minuten_nach_ende": 60, "chronik_tage": 14},
    # BIOS-Zeit beim Start (Memory Training) ab hier als Hinweis melden, in s (0 = aus)
    "post_zeit_hinweis_s": 60,
    # smartctl.exe (smartmontools) f\u00fcr SMART-Fehlerz\u00e4hler; leer = Standardpfad unter Programme
    "smartctl_pfad": "",
}


def cpu_temp_key(cfg, is_x3d):
    """Grenzwert-Schl\u00fcssel f\u00fcr die CPU-Temperatur (X3D-Modelle haben ein niedrigeres Limit)."""
    return "cpu_x3d" if is_x3d and "cpu_x3d" in cfg["temperaturen"] else "cpu"


def deep_merge(base, over):
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


# --------------------------------------------------------------------------
# Hilfsfunktionen Formatierung
# --------------------------------------------------------------------------
def isnum(x):
    return x is not None and x == x


def fnum(x, d=1):
    if not isnum(x):
        return "\u2013"
    s = f"{x:,.{d}f}"
    return s.replace(",", "\u2009").replace(".", ",").replace("\u2009", ".")


def fdur(sec):
    sec = int(round(sec))
    h, r = divmod(sec, 3600)
    m, s = divmod(r, 60)
    if h:
        return f"{h} h {m:02d} min"
    if m:
        return f"{m} min {s:02d} s"
    return f"{s} s"


UNIT_DIGITS = {"V": 3, "\u00b0C": 1, "W": 1, "MHz": 0, "%": 1, "RPM": 0, "GT/s": 1,
               "FPS": 1, "ms": 1, "MB": 0, "A": 1, "x": 1, "T": 0, "GB/s": 2, "GB": 0}


def digits_for(unit):
    return UNIT_DIGITS.get(unit, 2)


def pct(sorted_vals, p):
    if not sorted_vals:
        return NAN
    k = (len(sorted_vals) - 1) * p / 100.0
    f = math.floor(k)
    c = min(f + 1, len(sorted_vals) - 1)
    return sorted_vals[f] + (sorted_vals[c] - sorted_vals[f]) * (k - f)


def e(s):
    return html.escape(str(s), quote=True)


# --------------------------------------------------------------------------
# Einlesen
# --------------------------------------------------------------------------
YES = {"yes", "ja", "oui", "si", "s\u00ed", "sim", "tak", "ano", "igen", "da"}
NO = {"no", "nein", "non", "nao", "n\u00e3o", "nie", "ne", "nem", "nu"}
UNIT_RX = re.compile(r"\[([^\[\]]*)\]\s*$")


@dataclass
class Column:
    idx: int
    name: str
    group: str
    unit: str
    vals: array
    kind: str = "num"
    n_valid: int = 0
    vmin: float = NAN
    vmax: float = NAN
    vavg: float = NAN
    vfirst: float = NAN
    vlast: float = NAN

    @property
    def short(self):
        return UNIT_RX.sub("", self.name).strip()

    def finalize(self):
        v = [x for x in self.vals if x == x]
        self.n_valid = len(v)
        if v:
            self.vmin, self.vmax = min(v), max(v)
            self.vavg = sum(v) / len(v)
            self.vfirst, self.vlast = v[0], v[-1]


def _low_pct(c, q=0.01):
    """Unteres 1-%-Quantil: robust gegen einzelne Fehlmessungen, zeigt aber wiederholte Einbr\u00fcche."""
    v = sorted(x for x in c.vals if x == x)
    return v[min(len(v) - 1, int(len(v) * q))] if v else NAN


@dataclass
class Log:
    path: str
    names: list
    groups: list
    cols: list
    t: array
    t0: dt.datetime
    interval: float
    footer: bool
    group_source: str
    nul_bytes: int
    bad_lines: int
    encoding: str
    delimiter: str
    header_hash: str
    file_id: str
    size: int
    source: str = "HWiNFO"
    meta: dict = field(default_factory=dict)

    @property
    def n(self):
        return len(self.t)

    @property
    def duration(self):
        return self.t[-1] - self.t[0] if self.n else 0.0

    def wall_delta(self, sec):
        """Versatz der Wanduhr gegen\u00fcber der fortlaufenden Messzeit (nur nach einer Zeitumstellung != 0)."""
        d = 0
        for rel, delta in self.meta.get("dst", []):
            if sec >= rel - 1e-6:
                d = delta
        return d

    def clock(self, sec, with_date=None):
        ts = self.at(sec)
        if with_date is None:
            with_date = self.at(self.duration).date() != self.t0.date()
        return ts.strftime("%d.%m. %H:%M:%S" if with_date else "%H:%M:%S")

    def at(self, sec):
        return self.t0 + dt.timedelta(seconds=sec + self.wall_delta(sec))

    def rel_of(self, ts_local, ts_utc=None):
        """Lokale Ereigniszeit -> Messzeit. Bei Zeitumstellung ueber UTC eindeutig, sonst direkt."""
        if self.meta.get("dst") and ts_utc is not None:
            try:
                t0u = self.t0.astimezone(dt.timezone.utc).replace(tzinfo=None)
                return (ts_utc - t0u).total_seconds()
            except (OverflowError, OSError, ValueError):
                pass
        rel = (ts_local - self.t0).total_seconds()
        for r, delta in self.meta.get("dst", []):
            if rel - delta >= r:
                rel -= delta
        return rel


_DATE_CACHE = {}


def parse_date(s):
    d = _DATE_CACHE.get(s)
    if d is not None:
        return d
    m = re.match(r"^\s*(\d{1,4})[./-](\d{1,2})[./-](\d{1,4})\s*$", s)
    if not m:
        return None
    a, b, c = m.groups()
    try:
        if len(a) == 4:
            d = dt.date(int(a), int(b), int(c))
        elif "/" in s:
            y = int(c) + (2000 if len(c) <= 2 else 0)
            d = dt.date(y, int(a), int(b))
        else:
            y = int(c) + (2000 if len(c) <= 2 else 0)
            d = dt.date(y, int(b), int(a))
    except ValueError:
        return None
    _DATE_CACHE[s] = d
    return d


def parse_time(s):
    try:
        p = s.strip().split(":")
        if len(p) != 3:
            return None
        return int(p[0]) * 3600 + int(p[1]) * 60 + float(p[2].replace(",", "."))
    except ValueError:
        return None


def _last_sunday(year, month):
    d = dt.date(year, month, 31)
    while d.weekday() != 6:
        d -= dt.timedelta(days=1)
    return d


def dst_fix(t_abs):
    """EU-Zeitumstellung in lokalen Zeitstempeln ausgleichen (letzter Sonntag im M\u00e4rz/Oktober, gegen 2-3 Uhr).

    Gibt (korrigierte Zeiten, [(Index ab, Anzeige-Versatz in s)]) zur\u00fcck. Die korrigierte Zeitachse l\u00e4uft
    durch, die Anzeige rechnet den Versatz wieder drauf, damit Uhrzeiten der Wanduhr entsprechen.
    """
    out = array("d")
    switches = []
    shift = 0.0
    prev_raw = None
    for i, x in enumerate(t_abs):
        if prev_raw is not None:
            d = x - prev_raw
            day = dt.date.fromordinal(int(x // 86400))
            tod = (x % 86400) / 3600.0
            if -3720 <= d <= -3480 and day == _last_sunday(day.year, 10) and 1.9 <= tod <= 3.1:
                shift += 3600
                switches.append((i, -int(shift)))
            elif 3480 <= d <= 3720 and day == _last_sunday(day.year, 3) and 2.9 <= tod <= 3.2:
                shift -= 3600
                switches.append((i, -int(shift)))
        out.append(x + shift)
        prev_raw = x
    return out, switches


def header_key(names):
    return hashlib.sha1("\x1f".join(names).encode("utf-8")).hexdigest()


def file_fingerprint(path):
    h = hashlib.sha1()
    size = os.path.getsize(path)
    h.update(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(65536))
        if size > 131072:
            f.seek(-65536, os.SEEK_END)
            h.update(f.read())
    return h.hexdigest()[:16], size


def read_tail_rows(path, nbytes=400000):
    """Liest die letzten Zeilen einer (anderen) CSV, um deren Logabschluss zu finden."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > nbytes:
                f.seek(-nbytes, os.SEEK_END)
            raw = f.read().replace(b"\x00", b"")
        for enc in ("utf-8-sig", "cp1252"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if len(lines) < 2:
            return None
        delim = ";" if lines[-1].count(";") > lines[-1].count(",") else ","
        return list(csv.reader(lines[-2:], delimiter=delim))
    except OSError:
        return None


def synth_groups(names):
    """Notbehelf ohne Logabschluss: RAM- und SMART-Bloecke anhand der Spaltennamen erkennen."""
    groups = [""] * len(names)
    dimm_start = re.compile(r"^SPD Hub Temperat", re.I)
    dimm_rx = re.compile(r"^(SPD Hub|VDD \(SWA\)|VDDQ \(SWB\)|VPP \(SWC\)|1[ .,]8V VOUT|1[ .,]0V VOUT|VIN\b|"
                         r"Gesamtleistung|Total Power|PMIC)", re.I)
    smart_start = re.compile(r"^(Laufwerkstemperatur|Drive Temperature) \[", re.I)
    smart_rx = re.compile(r"^(Laufwerkstemperatur|Drive Temperature|Verbleibende Lebensdauer|Drive Remaining|"
                          r"Verf\u00fcgbarer Laufwerksreserve|Available Spare|Festplattenfehler|Drive Failure|"
                          r"Festplattenwarnung|Drive Warning|Host-|Total Host|NAND-|Total NAND)", re.I)
    cur, kind, k, d = None, None, 0, 0
    for i, n in enumerate(names):
        if dimm_start.search(n):
            k += 1
            cur, kind = f"DDR5 DIMM [#{k}]: (Gruppe gesch\u00e4tzt)", "dimm"
        elif smart_start.search(n):
            d += 1
            cur, kind = f"S.M.A.R.T.: Laufwerk {d} (Gruppe gesch\u00e4tzt)", "smart"
        elif cur and ((kind == "dimm" and dimm_rx.search(n)) or (kind == "smart" and smart_rx.search(n))):
            pass
        else:
            cur, kind = None, None
        if cur:
            groups[i] = cur
    return groups


def conv_factory(decimal_comma):
    special = {"": NAN}
    for y in YES:
        special[y] = 1.0
        special[y.capitalize()] = 1.0
        special[y.upper()] = 1.0
    for n in NO:
        special[n] = 0.0
        special[n.capitalize()] = 0.0
        special[n.upper()] = 0.0

    def conv(s):
        v = special.get(s)
        if v is not None:
            return v
        try:
            return float(s.replace(",", ".") if decimal_comma else s)
        except ValueError:
            s2 = s.strip()
            v = special.get(s2)
            if v is not None:
                return v
            try:
                return float(s2.replace(",", "."))
            except ValueError:
                return NAN
    return conv


def load_log(path, schema_cache):
    with open(path, "rb") as f:
        raw = f.read()
    nul = raw.count(b"\x00")
    if nul:
        raw = raw.replace(b"\x00", b"")
    text = None
    enc = "utf-8"
    for enc in ("utf-8-sig", "cp1252"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    del raw
    head2 = text[:200000].splitlines()[:2]
    if len(head2) == 2 and is_lhm_header(head2[0], head2[1]):
        return load_lhm(path, text, nul)
    lines = text.splitlines()
    del text
    if not lines:
        raise ValueError("Datei ist leer")
    first = lines[0]
    delim = ";" if first.count(";") > first.count(",") else ","
    conv = conv_factory(delim == ";")
    reader = csv.reader(lines, delimiter=delim)
    header = next(reader)
    if len(header) < 3 or not header[0].strip():
        raise ValueError("Keine HWiNFO-Kopfzeile gefunden")
    ncol = len(header)
    colvals = [array("d") for _ in range(ncol)]
    t_abs = array("d")
    footer_header, group_row = None, None
    bad = 0
    nrows = 0
    last_len = 0
    for row in reader:
        if not row:
            continue
        d = parse_date(row[0]) if row[0] else None
        tm = parse_time(row[1]) if d is not None and len(row) > 1 else None
        if d is None or tm is None:
            if row[0].strip() == header[0].strip() and len(row) > 2:
                footer_header = row
            elif not row[0].strip() and len(row) > 2 and not (row[1].strip() if len(row) > 1 else ""):
                group_row = row
            else:
                bad += 1
            continue
        if len(row) > ncol:
            for _ in range(len(row) - ncol):
                a = array("d", [NAN]) * nrows
                colvals.append(a)
            ncol = len(row)
        t_abs.append(d.toordinal() * 86400.0 + tm)
        for j in range(2, ncol):
            colvals[j].append(conv(row[j]) if j < len(row) else NAN)
        nrows += 1
        last_len = len(row)
    del lines
    partial = 0
    if nrows > 1 and footer_header is None and last_len < len(header) * 0.9:
        # beim Absturz halb geschriebene letzte Zeile verwerfen
        t_abs.pop()
        for j in range(2, ncol):
            colvals[j].pop()
        nrows -= 1
        partial = 1
    if nrows == 0:
        raise ValueError("Keine Messzeilen gefunden")

    names = list(header)
    if footer_header and len(footer_header) >= len(names):
        names = list(footer_header)
    while len(names) < ncol:
        names.append(f"Unbekannte Spalte {len(names) + 1}")
    hkey = header_key(header)

    footer = footer_header is not None and group_row is not None
    if group_row:
        groups = list(group_row) + [""] * max(0, ncol - len(group_row))
        gsrc = "Logabschluss"
        schema_cache[hkey] = groups
    elif hkey in schema_cache:
        groups = list(schema_cache[hkey]) + [""] * max(0, ncol - len(schema_cache[hkey]))
        gsrc = "Zwischenspeicher (fr\u00fcheres Log mit gleichen Sensoren)"
    else:
        groups, gsrc = None, ""
        folder = os.path.dirname(os.path.abspath(path))
        try:
            siblings = sorted(os.listdir(folder))
        except OSError:
            siblings = []
        for fn in siblings:
            p = os.path.join(folder, fn)
            if not fn.lower().endswith(".csv") or os.path.abspath(p) == os.path.abspath(path):
                continue
            tail = read_tail_rows(p)
            if tail and len(tail) == 2 and tail[0][:len(header)] == header and not tail[1][0].strip():
                groups = list(tail[1]) + [""] * max(0, ncol - len(tail[1]))
                gsrc = f"Nachbarlog {fn}"
                schema_cache[hkey] = list(tail[1])
                break
        if groups is None:
            groups = synth_groups(names)
            gsrc = "gesch\u00e4tzt (kein Logabschluss, kein passendes Vergleichslog)"
    groups = groups[:ncol]

    base = t_abs[0]
    t_abs, dst_sw = dst_fix(t_abs)
    t = array("d", (x - base for x in t_abs))
    od = int(base // 86400)
    t0 = dt.datetime.combine(dt.date.fromordinal(od), dt.time()) + dt.timedelta(seconds=base - od * 86400)
    if len(t) > 1:
        diffs = sorted(t[i] - t[i - 1] for i in range(1, len(t)))
        interval = diffs[len(diffs) // 2]
    else:
        interval = 0.0

    cols = [None, None]
    for j in range(2, ncol):
        nm = names[j].strip()
        if not nm and j == ncol - 1:
            cols.append(None)
            continue
        m = UNIT_RX.search(nm)
        unit = m.group(1) if m else ""
        c = Column(idx=j, name=nm, group=(groups[j] if j < len(groups) else "").strip(), unit=unit,
                   vals=colvals[j], kind="bool" if unit.lower() in ("yes/no", "ja/nein") else "num")
        c.finalize()
        cols.append(c)
    fid, size = file_fingerprint(path)
    return Log(path=path, names=names, groups=groups, cols=cols, t=t, t0=t0, interval=interval,
               footer=footer, group_source=gsrc, nul_bytes=nul, bad_lines=bad + partial, encoding=enc,
               delimiter=delim, header_hash=hkey, file_id=fid, size=size,
               meta={"dst": [(t[i], d) for i, d in dst_sw]})


# --------------------------------------------------------------------------
# LibreHardwareMonitor (LHM)
# --------------------------------------------------------------------------
# Format (LHM Logger.cs): Zeile 1 = ",<Sensorkennung>,..." (z. B. /amdcpu/0/temperature/2),
# Zeile 2 = "Time,<Sensorname>,...", danach "MM/dd/yyyy HH:mm:ss,<Wert>,..." (Punkt als Dezimaltrenner,
# leer = kein Wert). Kein Logabschluss; im Modus "Daily" werden Neustarts in dieselbe Datei angehaengt.
LHM_UNITS = {"temperature": "°C", "voltage": "V", "current": "A", "power": "W", "clock": "MHz", "load": "%",
             "fan": "RPM", "flow": "L/h", "control": "%", "level": "%", "factor": "x", "data": "GB", "smalldata": "MB",
             "throughput": "B/s", "timing": "ns", "energy": "mWh", "noise": "dBA", "humidity": "%", "frequency": "Hz",
             "conductivity": "µS/cm", "timespan": "s"}

# Gigabyte-Boards mit ITE IT8696E, die LHM (noch) nicht kennt, liefern nur "Voltage #1..#7" als Rohwerte.
# Belegung wie X870 AORUS ELITE WIFI7 in LHM; am 03.10.2026 auf einem B850 AORUS ELITE WIFI7 gegen HWiNFO
# abgeglichen (Abweichung < 0,005 V bzw. 0,1 °C).
IT8696E_GIGABYTE = {
    "voltage": {0: ("Vcore", 1.0), 1: ("+3.3V", 1.649), 2: ("+12V", 6.0), 3: ("+5V", 2.5), 4: ("CPU VCORE SoC", 1.0),
                5: ("CPU VCORE MISC", 1.0), 6: ("CPU VDDIO_MEM", 1.0)},
    "temperature": {0: "System1", 1: "PCH", 2: "CPU", 3: "PCIEX16", 4: "VRM MOS", 5: "EC"},
    "fan": {0: "CPU", 1: "System 1", 2: "System 2", 3: "System 3", 4: "CPU OPT", 5: "System 4"},
}
# Board-Sensornamen, die LHM bei bekannten Boards vergibt -> HWiNFO-aehnliche Namen
LHM_BOARD_NAMES = {"+3V Standby": "3VSB", "CMOS Battery": "VBAT", "+3.3V": "+3.3V", "+12V": "+12V", "+5V": "+5V",
                   "Vcore": "Vcore", "CPU NB/SoC": "CPU VCORE SoC", "CPU VDDIO": "CPU VDDIO_MEM",
                   "CPU Misc": "CPU VCORE MISC", "System #1": "System1", "PCIe x16": "PCIEX16", "CPU Fan": "CPU"}
LHM_CPU = {
    "temperature": {"Core (Tctl/Tdie)": "CPU (Tctl/Tdie)", "Core (Tctl)": "CPU (Tctl)", "Core (Tdie)": "CPU (Tdie)"},
    "power": {"Package": "CPU Package Power"},
    "clock": {"Cores (Average)": "Core Clocks (avg)", "Cores (Average Effective)": "Average Effective Clock",
              "Bus Speed": "Bus Clock"},
    "load": {"CPU Total": "Total CPU Usage", "CPU Core Max": "Max CPU/Thread Usage"},
    "voltage": {"Core (SVI2 TFN)": "CPU Core Voltage (SVI2 TFN)", "SoC (SVI2 TFN)": "SoC Voltage (SVI2 TFN)"},
}
LHM_NVIDIA = {
    "temperature": {"GPU Core": "GPU Temperature", "GPU Hot Spot": "GPU Hot Spot Temperature",
                    "GPU Memory Junction": "GPU Memory Junction Temperature"},
    "power": {"GPU Package": "GPU Power"},
    "load": {"GPU Core": "GPU Core Load", "GPU Power": "GPU Power Limit Usage", "GPU Board Power": "GPU Board Power Limit Usage",
             "GPU Memory": "GPU Memory Usage"},
    "clock": {"GPU Core": "GPU Clock", "GPU Memory": "GPU Memory Clock"},
}


def is_lhm_header(line0, line1):
    return line0.startswith(",/") and line1.startswith("Time,")


def log_identity(path):
    """(Kennung, Groesse, ist_LHM). LHM-Tagesdateien wachsen weiter -> Kennung aus Name + erstem Zeitstempel."""
    size = os.path.getsize(path)
    with open(path, "rb") as f:
        head = f.read(262144)
    lines = head.replace(b"\x00", b"").decode("utf-8-sig", errors="replace").splitlines()
    if len(lines) >= 3 and is_lhm_header(lines[0], lines[1]):
        first = lines[2].split(",", 1)[0]
        key = f"{os.path.basename(path).lower()}|{first}"
        return "lhm-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:12], size, True
    fid, size = file_fingerprint(path)
    return fid, size, False


def _lhm_size_label(gb):
    if not isnum(gb) or gb <= 0:
        return ""
    return f"{fnum(gb / 1000, 0)} TB" if gb >= 950 else f"{int(round(gb))} GB"


def _median(vals):
    v = sorted(x for x in vals if x == x)
    return v[len(v) // 2] if v else NAN


def load_lhm(path, text, nul=0):
    lines = text.splitlines()
    reader = csv.reader(lines)
    ids = next(reader)
    names = next(reader)
    ncol = len(ids)
    if ncol < 2 or len(names) != ncol:
        raise ValueError("LHM-Kopfzeilen passen nicht zusammen")
    conv = conv_factory(False)
    colvals = [array("d") for _ in range(ncol)]
    t_abs = array("d")
    bad, nrows, last_len = 0, 0, 0
    first_ts = ""
    for row in reader:
        if not row or not row[0]:
            continue
        stamp = row[0].strip()
        d = parse_date(stamp[:10]) if len(stamp) >= 19 else None
        tm = parse_time(stamp[11:]) if d is not None else None
        if d is None or tm is None:
            bad += 1
            continue
        if not first_ts:
            first_ts = stamp
        t_abs.append(d.toordinal() * 86400.0 + tm)
        for j in range(1, ncol):
            colvals[j].append(conv(row[j]) if j < len(row) else NAN)
        nrows += 1
        last_len = len(row)
    partial = 0
    if nrows > 1 and last_len < ncol * 0.9:
        t_abs.pop()
        for j in range(1, ncol):
            colvals[j].pop()
        nrows -= 1
        partial = 1
    if nrows == 0:
        raise ValueError("Keine Messzeilen gefunden")

    # Hilfswerte: Rohmediane fuer das Board-Profil, Laufwerksgroessen, GPU-Speicher
    parsed = []
    for j in range(1, ncol):
        ident = ids[j].strip()
        parts = ident.strip("/").split("/")
        if len(parts) < 3:
            parsed.append(None)
            continue
        parsed.append(("/".join(parts[:-2]), parts[-2], parts[-1], names[j].strip(), j))
    by_hw = {}
    for p_ in parsed:
        if p_:
            by_hw.setdefault(p_[0], []).append(p_)

    def raw_median(hw, stype, idx):
        for h, st, ix, nm, j in by_hw.get(hw, []):
            if st == stype and ix == str(idx):
                return _median(colvals[j])
        return NAN

    meta = {"notes": []}
    profiles = {}
    for hw in by_hw:
        m = re.match(r"^lpc/(it8696e)/\d+$", hw)
        if not m:
            continue
        generic = all(nm.startswith(("Voltage #", "Temperature #", "Fan #")) or nm in ("+3V Standby", "CMOS Battery")
                      for h, st, ix, nm, j in by_hw[hw] if st in ("voltage", "temperature", "fan"))
        v12, v5, v33 = raw_median(hw, "voltage", 2) * 6, raw_median(hw, "voltage", 3) * 2.5, raw_median(hw, "voltage", 1) * 1.649
        plausible = 11.0 <= v12 <= 13.0 and 4.5 <= v5 <= 5.5 and 3.0 <= v33 <= 3.6
        if generic and plausible:
            profiles[hw] = IT8696E_GIGABYTE
            meta["board"] = "ITE IT8696E (Profil Gigabyte)"
            meta["notes"].append("board_profil")
        elif generic:
            meta["notes"].append("board_roh")
            meta["board"] = "ITE IT8696E"

    gpu_amd_igpu = {}
    for hw in by_hw:
        if hw.startswith("gpu-amd/"):
            tot = NAN
            for h, st, ix, nm, j in by_hw[hw]:
                if nm == "GPU Memory Total":
                    tot = _median(colvals[j])
            gpu_amd_igpu[hw] = not isnum(tot) or tot <= 4096
    drive_size = {}
    for hw in by_hw:
        if hw.split("/")[0] in ("nvme", "ssd", "hdd", "storage"):
            for h, st, ix, nm, j in by_hw[hw]:
                if nm == "Total Space":
                    drive_size[hw] = _median(colvals[j])

    out_names, out_groups, out_cols = ["Date", "Time"], ["", ""], [None, None]
    k = 2
    for p_ in parsed:
        if p_ is None:
            continue
        hw, st, ix, nm, j = p_
        top = hw.split("/")[0]
        unit = LHM_UNITS.get(st, "")
        vals = colvals[j]
        new, group = nm, ""
        if top == "nic" or st == "control":
            continue
        if top == "lpc":
            chip = hw.split("/")[1].upper()
            maker = "ITE " if chip.startswith("IT") else "Nuvoton " if chip.startswith("NCT") else ""
            group = f"Mainboard ({maker}{chip})"
            prof = profiles.get(hw)
            if prof and st in prof and int(ix) in prof[st]:
                entry = prof[st][int(ix)]
                if st == "voltage":
                    new, factor = entry
                    if factor != 1.0:
                        vals = array("d", (x * factor if x == x else NAN for x in vals))
                else:
                    new = entry
            else:
                new = LHM_BOARD_NAMES.get(nm, nm)
        elif top in ("amdcpu", "intelcpu"):
            group = f"CPU [#{hw.split('/')[1]}]: " + ("AMD-Prozessor" if top == "amdcpu" else "Intel-Prozessor")
            new = LHM_CPU.get(st, {}).get(nm, "")
            if not new:
                mc = re.match(r"^CCD(\d+) \(Tdie\)$", nm)
                mcore = re.match(r"^(?:CPU )?Core #(\d+)(.*)$", nm)
                if mc:
                    new = f"CPU CCD{mc.group(1)} (Tdie)"
                elif nm.startswith("CCDs "):
                    new = "CPU " + nm
                elif mcore and st == "load":
                    new = f"CPU Thread #{mcore.group(1)} Usage"
                elif mcore and st == "clock":
                    new = f"Core #{mcore.group(1)} " + ("Effective Clock" if "Effective" in mcore.group(2) else "Clock")
                elif mcore and st == "factor":
                    new = f"Core #{mcore.group(1)} Ratio"
                elif mcore and st == "power":
                    new = f"Core #{mcore.group(1)} Power"
                else:
                    new = nm
        elif top == "gpu-nvidia":
            group = f"dGPU [#{hw.split('/')[1]}]: NVIDIA-Grafikkarte"
            new = LHM_NVIDIA.get(st, {}).get(nm, nm)
            if st == "fan":
                new = re.sub(r"^GPU Fan (\d)$", r"GPU Fan\1", nm)
        elif top in ("gpu-amd", "gpu-intel"):
            igpu = gpu_amd_igpu.get(hw, True) if top == "gpu-amd" else True
            vendor = "AMD Radeon" if top == "gpu-amd" else "Intel"
            group = (f"iGPU [#{hw.split('/')[1]}]: {vendor} (integriert)" if igpu
                     else f"dGPU [#{hw.split('/')[1]}]: {vendor}-Grafikkarte")
            if nm == "Fullscreen FPS":
                continue
            if not igpu:
                new = LHM_NVIDIA.get(st, {}).get(nm, nm)
        elif top == "memory" and hw.startswith("memory/dimm/"):
            n = int(hw.split("/")[2])
            if st == "temperature" and ix == "0":
                new = "SPD Hub Temperature"
            elif st == "data" and nm == "Capacity":
                new = "Capacity"
            else:
                continue
            # SPD-Adresse 0x50+n: Kanal = n // 2, Steckplatz im Kanal = n % 2 (deckt sich mit HWiNFO)
            group = f"DIMM [#{n}]: (P0 CHANNEL {chr(65 + n // 2)}/DIMM {n % 2}, abgeleitet)"
            meta["dimm_abgeleitet"] = True
        elif top == "ram":
            group = "Arbeitsspeicher (Windows)"
            if st == "data" and nm == "Memory Used":
                new, unit = "Physical Memory Used", "MB"
                vals = array("d", (x * 1024 if x == x else NAN for x in vals))
            elif st == "data" and nm == "Memory Available":
                new, unit = "Physical Memory Available", "MB"
                vals = array("d", (x * 1024 if x == x else NAN for x in vals))
            elif st == "load":
                new = "Physical Memory Load"
        elif top == "vram":
            group = "Virtueller Speicher (Windows)"
            new = "Virtual " + nm
        elif top in ("nvme", "ssd", "hdd", "storage"):
            kind = {"nvme": "NVMe-SSD", "ssd": "SATA-SSD", "hdd": "Festplatte"}.get(top, "Laufwerk")
            size = _lhm_size_label(drive_size.get(hw, NAN))
            group = f"S.M.A.R.T.: {kind} #{hw.split('/')[1]}" + (f" · {size}" if size else "")
            if st == "temperature":
                if ix in ("10", "11") or "Warning" in nm or "Critical" in nm:
                    # vom Laufwerk selbst gemeldete Grenzen (NVMe WCTEMP/CCTEMP)
                    v = _median(vals)
                    if isnum(v) and 40 <= v <= 120:
                        lim = meta.setdefault("drive_limits", {}).setdefault(group, {})
                        lim["krit" if ix == "11" or "Critical" in nm else "warn"] = v
                    continue
                new = "Drive Temperature" if ix == "0" else f"Drive Temperature {int(ix) + 1}"
            elif st == "level" and nm == "Life":
                new = "Drive Remaining Life"
            elif st == "level" and nm == "Available Spare":
                new = "Available Spare"
            elif st == "level" and "Threshold" in nm:
                continue
        else:
            group = f"LHM: {hw}"
        col = Column(idx=k, name=f"{new} [{unit}]" if unit else new, group=group, unit=unit, vals=vals,
                     kind="num")
        col.finalize()
        out_names.append(col.name)
        out_groups.append(group)
        out_cols.append(col)
        k += 1

    base = t_abs[0]
    t_abs, dst_sw = dst_fix(t_abs)
    t = array("d", (x - base for x in t_abs))
    od = int(base // 86400)
    t0 = dt.datetime.combine(dt.date.fromordinal(od), dt.time()) + dt.timedelta(seconds=base - od * 86400)
    diffs = sorted(t[i] - t[i - 1] for i in range(1, len(t)) if t[i] > t[i - 1])
    interval = diffs[len(diffs) // 2] if diffs else 0.0
    meta["dst"] = [(t[i], d) for i, d in dst_sw]
    fid, size, _ = log_identity(path)
    return Log(path=path, names=out_names, groups=out_groups, cols=out_cols, t=t, t0=t0, interval=interval,
               footer=False, group_source="LHM-Sensorkennungen", nul_bytes=nul, bad_lines=bad + partial,
               encoding="utf-8", delimiter=",", header_hash="", file_id=fid, size=size, source="LHM", meta=meta)


# --------------------------------------------------------------------------
# Sensor-Zuordnung (deutsche und englische HWiNFO-Bezeichnungen)
# --------------------------------------------------------------------------
G_CPU = r"^CPU \[#\d+\]"
G_DGPU = r"^d?GPU \[#\d+\]"
G_BOARD = r"\((ITE|Nuvoton|NCT\d|Fintek|IT8\d|AMD Chip|Intel Chip|Chipsatz|Chipset|EC\))"
GPU_ANCHOR = r"^GPU[- ]Hot[- ]?Spot|^GPU (Core Load|Kernlast)|12VHPWR|^GPU[- ](Memory Junction|Speicher Sperrschicht)"


class Sensors:
    def __init__(self, log: Log):
        self.log = log
        self.cols = [c for c in log.cols if c is not None and c.n_valid > 0]
        self.grouped = any(c.group for c in self.cols) and not log.group_source.startswith("gesch")
        grp = [g.strip() for g in log.groups if g and g.strip()]
        self.cpu_name = self._name_from(grp, G_CPU)
        self.gpu_name = self._gpu_name(grp)
        self.board_name = ""
        for g in grp:
            m = re.match(r"^System:\s*(.+)$", g)
            if m:
                self.board_name = m.group(1).strip()
                break
        if not self.board_name:
            for g in grp:
                if re.search(G_BOARD, g, re.I):
                    self.board_name = re.sub(r"\s*\(.*$", "", g).strip()
                    break
        if log.source == "LHM":
            self.board_name = log.meta.get("board", "")
        self.is_x3d = "X3D" in self.cpu_name.upper()
        self.dgpu_range = None if self.grouped else self._dgpu_range()

        f = self.find
        # CPU
        self.tctl = f(r"^CPU \(Tctl/Tdie\)") or f(r"^(CPU Package|Core Max|CPU-Paket) \[\u00b0C\]", G_CPU)
        self.ccd = f(r"^CPU CCD\d+ \(Tdie\)", pick="all")
        self.iod = f(r"^CPU IOD Hotspot")
        self.ppt = f(r"^CPU PPT \[W\]") or f(r"^(CPU Package Power|CPU-Gesamt-Leistungsaufnahme|CPU-Package-Leistung) \[W\]")
        self.core_power = f(r"^(CPU Core Power|CPU Kern-Leistung) \(SVI")
        self.soc_power = f(r"^(CPU SoC Power|CPU-SoC-Leistung) \(SVI")
        self.clock = f(r"^(Core Clocks \(avg\)|Kern-Takte \(avg\))")
        self.eff_clock = f(r"^(Average Effective Clock|Durchschnittlicher effektiver Takt)")
        self.usage = f(r"^(Total CPU Usage|Gesamte CPU-Auslastung)")
        self.vcore = f(r"^(CPU VDDCR_VDD (Voltage|Spannung)|CPU Core Voltage|CPU-Kernspannung) \(SVI")
        self.vsoc = f(r"^(CPU VDDCR_SOC (Voltage|Spannung)|SoC Voltage|SoC-Spannung) \(SVI")
        self.vcore_board = f(r"^(Vcore|CPU Vcore|CPU Core) \[V\]", G_BOARD)
        self.vsoc_board = f(r"^(CPU VCORE SoC|VSOC|CPU SoC|SoC) \[V\]", G_BOARD)
        self.vddio = f(r"^(CPU VDDIO_MEM|CPU VDDIO|VDDIO|DRAM|VDIMM|DIMM) \[V\]", G_BOARD)
        self.cpu_throttle = [c for c in self.cols if c.kind == "bool" and _gm(c, G_CPU)
                             and re.search(r"Throttling|Drosselung|PROCHOT", c.name, re.I)]
        # Board
        self.rails = []
        for rx, nom, lab in ((r"^\+12V \[V\]", 12.0, "+12 V (Board)"), (r"^\+5V \[V\]", 5.0, "+5 V (Board)"),
                             (r"^\+3[ .,]3 ?V \[V\]", 3.3, "+3,3 V (Board)"), (r"^3VSB \[V\]", 3.3, "3VSB"),
                             (r"^5VSB \[V\]", 5.0, "5VSB")):
            c = f(rx)
            if c:
                self.rails.append((c, nom, lab))
        self.vbat = f(r"^(VBAT|CMOS Battery|Batterie|Battery)\b.*\[V\]")
        self.board_temps = [c for c in self.cols if c.unit == "\u00b0C" and _gm(c, G_BOARD, strict=True)]
        self.vrm = [c for c in self.board_temps if re.search(r"VRM|MOS|VR\b", c.name, re.I)]
        self.chipset = [c for c in self.board_temps if re.search(r"Chipset|Chipsatz|PCH", c.name, re.I)]
        self.fans = [c for c in self.cols if c.unit == "RPM" and not _gm(c, G_DGPU, strict=True)
                     and not re.search(r"^GPU", c.name)]
        # WHEA
        self.whea = f(r"^(Gesamtfehler|Total Errors)", r"WHEA")
        # GPU
        gf = lambda rx, **kw: f(rx, G_DGPU, anchor=GPU_ANCHOR, **kw)
        self.gpu_temp = gf(r"^GPU[- ]Temperat\w* \[")
        self.gpu_hot = gf(r"^GPU[- ]Hot[- ]?Spot")
        self.gpu_mem = gf(r"^GPU[- ](Memory Junction|Speicher Sperrschicht)")
        self.gpu_power = gf(r"^(GPU Power|GPU Leistung|GPU ASIC Power|Total Board Power[^\[]*|GPU PPT) \[W\]")
        self.gpu_limit_nom = gf(r"(Leistungsgrenze|Power Limit).*\((Nennleistung|Nominal|Default)\)")
        self.gpu_load = gf(r"^(GPU Core Load|GPU Kernlast|GPU Utilization) \[%\]") or gf(r"^(GPU-Auslastung|GPU Usage) \[%\]")
        self.gpu_clock = gf(r"^(GPU Clock|GPU-Takt) \[MHz\]")
        self.gpu_link = gf(r"^(PCIe Link Speed|PCIe-Link-Geschwindigkeit) \[")
        self.gpu_fans = [c for c in self.cols if c.unit == "RPM" and (_gm(c, G_DGPU, strict=True) or re.search(r"^GPU", c.name))]
        self.gpu_rails = []
        for rx, lab in ((r"^GPU 12VHPWR (Voltage|Spannung)", "GPU 12VHPWR"),
                        (r"^GPU 16-pin.*(Voltage|Spannung)", "GPU 16-Pin"),
                        (r"^GPU PCIe \+12V (Input Voltage|Eingangsspannung)", "GPU PCIe-Slot 12 V")):
            c = gf(rx)
            if c:
                self.gpu_rails.append((c, 12.0, lab))
        for c in [c for c in self.cols if re.search(r"^GPU 8-pin #?\d.*(Voltage|Spannung)", c.name, re.I)]:
            self.gpu_rails.append((c, 12.0, "GPU " + c.short))
        self.gpu_thermal_limit = gf(r"^(Performance Limit|Leistungsgrenzwert) - (Thermal|Thermisch)")
        self.gpu_power_limit = gf(r"^(Performance Limit|Leistungsgrenzwert) - (Power|Leistung) \[")
        self.pcie = {}
        for key in ("Receiver Errors", "Replay Count", "Replay Rollover Count", "Bad DLLP Count", "Bad TLP Count",
                    "LCRC Error Count", "NAKs Sent Count", "NAKs Received Count", "Recovery Count",
                    "Correctable Error Count", "Non-Fatal Error Count", "Fatal Error Count", "Unsupported Request Count"):
            c = f("^" + re.escape(key) + r" \[")
            if c:
                self.pcie[key] = c
        self.pcie_lanes = f(r"^PCIe Lane \d+ Errors", pick="all")
        # Speicher
        self.mem_clock = f(r"^(Memory Clock|Speichertakt) \[MHz\]")
        self.bus = f(r"^(Bus Clock|Bustakt) \[MHz\]")
        self.timings = {k: f(rf"^{k} \[") for k in ("Tcas", "Trcd", "Trp", "Tras", "Trc", "Trfc")}
        self.cr = f(r"^Command Rate \[")
        self.fclk = f(r"\(FCLK\)")
        self.uclk = f(r"\(UCLK\)")
        self.mem_used = f(r"^(Physical Memory Used|Physikalischer Speicher verwendet) \[MB\]")
        self.mem_avail = f(r"^(Physical Memory Available|Physikalischer Speicher verf\u00fcgbar) \[MB\]")
        self.dimms = self._dimms()
        self.drives = self._drives()
        # Einschaltz\u00e4hler der Laufwerke: steigt nur, wenn die SSD stromlos war
        self.power_counts = [c for c in self.cols if c.group.startswith("S.M.A.R.T.")
                             and re.search(r"^(Power On Count|Power Cycles?|Einschaltvorg|Ein-/Ausschaltzyklen)", c.name, re.I)]
        # PresentMon
        pm_groups = [g for g in dict.fromkeys(grp) if re.match(r"^PresentMon", g)]
        self.pm_proc = ""
        if pm_groups:
            m = re.search(r"\[(.+?)\]", pm_groups[0])
            self.pm_proc = m.group(1) if m else ""
        self.pm_desktop = self.pm_proc.lower() in ("dwm.exe", "explorer.exe")
        self.fps = f(r"^(Framerate|Bildrate|Frame Rate) Presented \(avg\)") or f(r"^(Framerate|Bildrate|Frame Rate)\b.*\(avg\)")
        self.fps_low = f(r"^(Framerate|Bildrate|Frame Rate) Presented \(1% low\)")
        self.ft_avg = f(r"^Frame ?Time Presented \(avg\)") or f(r"^Frame ?Time\b.*\(avg\)")
        self.ft_hi = f(r"^Frame ?Time\b.*\((0[.,]1|1)% high\)", pick="all")

    # -- Suche --------------------------------------------------------------
    def find(self, name_rx, group_rx=None, unit=None, pick="first", anchor=None):
        rx = re.compile(name_rx, re.I)
        gx = re.compile(group_rx, re.I) if group_rx else None
        out = []
        for c in self.cols:
            if not rx.search(c.name):
                continue
            if unit and c.unit != unit:
                continue
            if gx and c.group and not gx.search(c.group):
                continue
            out.append(c)
        if pick == "all":
            return out
        if not out:
            return None
        if len(out) > 1 and gx:
            grouped = [c for c in out if c.group]
            if grouped:
                return grouped[0]
            if anchor and self.dgpu_range:
                a0, a1 = self.dgpu_range
                inside = [c for c in out if a0 <= c.idx < a1]
                if inside:
                    return inside[0]
            if anchor:
                ax = re.compile(anchor, re.I)
                anchors = [c.idx for c in self.cols if ax.search(c.name)]
                if anchors:
                    return min(out, key=lambda c: min(abs(c.idx - a) for a in anchors))
        return out[0]

    def _dgpu_range(self):
        """Ohne Sensorgruppen: Spaltenbereich der dedizierten GPU anhand typischer dGPU-Spalten bestimmen."""
        names = self.log.names
        ax = re.compile(GPU_ANCHOR, re.I)
        anchors = [i for i, n in enumerate(names) if ax.search(n)]
        if not anchors:
            return None
        start = None
        for a in sorted(anchors):
            for i in range(a, -1, -1):
                if re.match(r"^GPU[- ]Temperat\w* \[", names[i], re.I):
                    start = i
                    break
            if start is not None:
                break
        if start is None:
            return None
        end_rx = re.compile(r"^(Bildrate|Framerate|Frame Rate|Frame ?Time|Summe Download|Total DL|Aktuelle Download|"
                            r"Current DL|Gesamtfehler|Total Errors)", re.I)
        end = len(names)
        for i in range(start + 1, len(names)):
            if end_rx.search(names[i]) or names[i] == names[start]:
                end = i
                break
        return (start, end)

    @staticmethod
    def _name_from(groups, rx):
        for g in groups:
            if re.match(rx, g):
                m = re.match(r"^[^:]+:\s*([^:]+)", g)
                if m:
                    return m.group(1).strip()
        return ""

    @staticmethod
    def _gpu_name(groups):
        for g in groups:
            if re.match(G_DGPU, g):
                parts = [p.strip() for p in g.split(":")]
                return parts[-1] if len(parts) > 2 else (parts[1] if len(parts) > 1 else g)
        return ""

    def _dimms(self):
        gl = [g for g in dict.fromkeys(c.group for c in self.cols) if g and re.search(r"\bDIMM\b", g)
              and not re.search(r"^Speicher|^Memory", g)]
        out = []
        for g in gl:
            cs = [c for c in self.cols if c.group == g]
            pick = lambda rx: next((c for c in cs if re.search(rx, c.name, re.I)), None)
            m = re.search(r"CHANNEL\s*([A-H])\s*/\s*DIMM\s*(\d)", g, re.I)
            label = f"Kanal {m.group(1).upper()} \u00b7 DIMM {m.group(2)}" if m else re.sub(r":.*$", "", g)
            mod = re.search(r"\]:\s*([^()]+)", g)
            out.append({
                "group": g, "label": label, "channel": m.group(1).upper() if m else "",
                "module": mod.group(1).strip() if mod else "",
                "temp": pick(r"Temperat"), "vdd": pick(r"^VDD \(SWA\)"), "vddq": pick(r"^VDDQ \(SWB\)"),
                "vpp": pick(r"^VPP \(SWC\)"), "vin": pick(r"^VIN\b"),
                "flags": [c for c in cs if c.kind == "bool" and re.search(r"PMIC", c.name, re.I)],
            })
        return out

    def _drives(self):
        gl = [g for g in dict.fromkeys(c.group for c in self.cols) if g and re.match(r"^S\.M\.A\.R\.T\.", g)]
        out = []
        for g in gl:
            cs = [c for c in self.cols if c.group == g]
            pick = lambda rx: next((c for c in cs if re.search(rx, c.name, re.I)), None)
            temp2 = pick(r"(Laufwerkstemperatur|Drive Temperature) 2")
            extra = [c for c in cs if re.search(r"(Laufwerkstemperatur|Drive Temperature) [3-9]\b", c.name, re.I)]
            spare = pick(r"(Reserveplatz|Available Spare)")
            label = re.sub(r"^S\.M\.A\.R\.T\.:\s*", "", g)
            label = re.sub(r"\s*\([0-9A-Z_]{6,}\)", "", label)
            out.append({
                "group": g, "label": label, "type": "nvme" if (temp2 or spare) else "sata",
                "temp": pick(r"^(Laufwerkstemperatur|Drive Temperature) \["), "temp2": temp2, "extra": extra,
                "limits": self.log.meta.get("drive_limits", {}).get(g, {}),
                "life": pick(r"(Verbleibende Lebensdauer|Remaining Life)"), "spare": spare,
                "fail": pick(r"(Festplattenfehler|Drive Failure)"), "warn": pick(r"(Festplattenwarnung|Drive Warning)"),
            })
        return out


def _gm(c, rx, strict=False):
    if not c.group:
        return not strict
    return re.search(rx, c.group, re.I) is not None


# --------------------------------------------------------------------------
# Befunde
# --------------------------------------------------------------------------
@dataclass
class Finding:
    level: int
    cat: str
    title: str
    detail: str = ""
    t: float = None
    marks: list = field(default_factory=list)


class Analysis:
    def __init__(self, log: Log, cfg: dict, prev_entry=None, events_enabled=True, markers=None):
        self.log = log
        self.markers = markers or []
        self.cfg = cfg
        self.s = Sensors(log)
        self.F = []
        self.gaps = []
        self.events = []
        self.dumps = []
        self.events_note = ""
        self.prev = prev_entry
        self.load = self._load_mask()
        self.trans = self._transition_mask()
        self.fp = {}
        self.fp_changes = []
        self.kpi = {}
        self.end_rows = []
        self.end_cols = []
        self.end_roles = []
        self.breaks = []
        self.crash_tables = []
        self.events_ok = False
        self.profile = ""
        self.events_enabled = events_enabled

    def add(self, level, cat, title, detail="", t=None, marks=None):
        self.F.append(Finding(level, cat, title, detail, t, marks or []))

    # -- Masken -------------------------------------------------------------
    def _load_mask(self):
        n = self.log.n
        lim = self.cfg["lastgrenzen"]
        g = self.s.gpu_load.vals if self.s.gpu_load else None
        u = self.s.usage.vals if self.s.usage else None
        out = [False] * n
        for i in range(n):
            if g is not None and g[i] == g[i] and g[i] >= lim["gpu_prozent"]:
                out[i] = True
            elif u is not None and u[i] == u[i] and u[i] >= lim["cpu_prozent"]:
                out[i] = True
        return out

    def _transition_mask(self):
        """True rund um Wechsel der PCIe-Link-Geschwindigkeit (+-3 Messpunkte) und bei GPU-Leerlauf."""
        n = self.log.n
        c = self.s.gpu_link
        if not c:
            return None
        out = [False] * n
        v = c.vals
        for i in range(1, n):
            if v[i] == v[i] and v[i - 1] == v[i - 1] and v[i] != v[i - 1]:
                for k in range(max(0, i - 3), min(n, i + 4)):
                    out[k] = True
        g = self.s.gpu_load.vals if self.s.gpu_load else None
        if g is not None:
            for i in range(n):
                if g[i] == g[i] and g[i] < 10:
                    out[i] = True
        return out

    def _steady_load(self, i, radius=1):
        """GPU durchgehend ueber der Lastgrenze und kein Linkwechsel im Umfeld."""
        g = self.s.gpu_load.vals if self.s.gpu_load else None
        if g is None:
            return False
        thr = self.cfg["lastgrenzen"]["gpu_prozent"]
        n = self.log.n
        for k in range(max(0, i - radius), min(n, i + radius + 1)):
            if not (g[k] == g[k] and g[k] >= thr):
                return False
            if self.trans is not None and self.trans[k]:
                return False
        return True

    # -- Hilfen -------------------------------------------------------------
    def clk(self, t):
        return self.log.clock(t)

    def _exceed(self, c, levels, above=True):
        """Liefert (Stufe, Extremwert, Zeitpunkt Extrem, erste Ueberschreitung, Anzahl) oder None."""
        v, t = c.vals, self.log.t
        best_i, best = None, None
        for i, x in enumerate(v):
            if x != x:
                continue
            if best is None or (x > best if above else x < best):
                best, best_i = x, i
        if best is None:
            return None
        for lvl in (3, 2, 1):
            thr = levels[lvl - 1]
            hit = [i for i, x in enumerate(v) if x == x and (x >= thr if above else x <= thr)]
            if hit:
                return lvl, best, t[best_i], t[hit[0]], len(hit), thr
        return 0, best, t[best_i], None, 0, None

    def _samples_dur(self, k):
        return fdur(k * (self.log.interval or 1))

    # -- Pruefungen ---------------------------------------------------------
    def run(self):
        self.check_integrity()
        self.check_gaps()
        self.check_whea()
        self.check_pcie()
        self.check_throttle()
        self.check_temps()
        self.check_voltages()
        self.check_dimms()
        self.check_drives()
        self.check_fans()
        self.check_frametimes()
        self.check_dropouts()
        self.check_end()
        self.build_fingerprint()
        self.check_ram_profile()
        self.check_config_change()
        if self.events_enabled and self.cfg["ereignisse"].get("aktiv", True):
            self.check_events()
        else:
            self.events_note = "Ereignisprotokoll nicht ausgewertet (abgeschaltet)."
        self.classify_breaks()
        self.check_markers()
        self.build_kpi()
        self.F.sort(key=lambda f: (-f.level, f.t if f.t is not None else -1))
        return self

    @property
    def worst(self):
        return max([f.level for f in self.F if f.level > 0], default=0)

    def check_integrity(self):
        L = self.log
        for rel, delta in L.meta.get("dst", []):
            self.add(0, "Log", f"Zeitumstellung um {self.clk(rel)} ({'Winterzeit' if delta < 0 else 'Sommerzeit'})",
                     "Die Uhr springt um eine Stunde. Das Tool rechnet intern mit durchlaufender Zeit, damit keine falsche "
                     "L\u00fccke oder R\u00fcckw\u00e4rtsspr\u00fcnge entstehen, und zeigt trotzdem die Uhrzeit der Wanduhr.", t=rel)
        if L.source == "LHM":
            notes = L.meta.get("notes", [])
            self.add(0, "Log", "LibreHardwareMonitor-Dauerlog",
                     "LHM schreibt keinen Logabschluss. Abst\u00fcrze erkennt das Tool stattdessen an Unterbrechungen der "
                     "Aufzeichnung in Verbindung mit dem Windows-Ereignisprotokoll."
                     + (" Die Kanalzuordnung der RAM-Module ist aus der Steckplatznummer abgeleitet (DIMM #1 = Kanal A, "
                        "#3 = Kanal B) \u2013 deckt sich mit HWiNFO." if L.meta.get("dimm_abgeleitet") else ""))
            if "board_profil" in notes:
                self.add(0, "Board", "Board-Profil Gigabyte IT8696E angewendet",
                         "LHM kennt dieses Mainboard noch nicht und liefert nur Rohwerte (\u201eVoltage #1\u2026#7\u201c). "
                         "Das Tool rechnet sie um: 3,3 V \u00d71,649, 12 V \u00d76, 5 V \u00d72,5; Temperaturen und L\u00fcfter "
                         "werden benannt. Abgeglichen mit HWiNFO am 03.10.2026.")
            elif "board_roh" in notes:
                self.add(1, "Board", "Boardspannungen nur als Rohwerte",
                         "Der Sensorchip wurde erkannt, die Umrechnung passte aber nicht zu den Messwerten. 12 V/5 V/3,3 V "
                         "werden deshalb nicht bewertet.")
            if L.bad_lines:
                self.add(0, "Log", f"{L.bad_lines} unvollst\u00e4ndige/unlesbare Zeile(n) \u00fcbersprungen",
                         "Bei einem laufenden LHM-Log ist die letzte Zeile oft gerade erst halb geschrieben.")
            return
        if L.footer:
            self.add(0, "Log", "Log regul\u00e4r beendet",
                     "HWiNFO hat den Logabschluss geschrieben \u2013 das Logging wurde normal gestoppt.")
        else:
            extra = []
            if L.nul_bytes:
                extra.append(f"Die Datei enthielt {L.nul_bytes} NUL-Bytes (typisch f\u00fcr eine beim Absturz abgeschnittene letzte Zeile).")
            if self.log_still_running():
                self.add(0, "Log", "Log l\u00e4uft noch",
                         f"Die Datei wurde gerade noch geschrieben (letzter Messpunkt {self.clk(L.t[-1])}) \u2013 HWiNFO "
                         "zeichnet weiter auf. Den Logabschluss schreibt HWiNFO erst beim Stoppen.", t=L.t[-1])
                return
            self.add(2, "Log", "Log endet ohne regul\u00e4ren Abschluss",
                     "Der Logabschluss (zweite Kopfzeile + Sensorgruppen) fehlt. Ursachen: Absturz, Hardreset, "
                     "Stromausfall oder HWiNFO wurde hart beendet. Letzter Messpunkt: "
                     f"{self.clk(L.t[-1])}. " + " ".join(extra), t=L.t[-1])
        if L.bad_lines:
            self.add(0 if not L.footer else 1, "Log", f"{L.bad_lines} unvollst\u00e4ndige/unlesbare Zeile(n) \u00fcbersprungen",
                     "Besch\u00e4digte oder halb geschriebene Zeilen, typischerweise die letzte Zeile nach einem Absturz.")
        if L.group_source != "Logabschluss":
            self.add(0, "Log", "Sensorgruppen nicht aus diesem Log",
                     f"Zuordnung der Spalten zu Ger\u00e4ten: {L.group_source}.")

    def log_still_running(self):
        """Ausgewertet w\u00e4hrend HWiNFO noch schreibt: Datei und letzter Messpunkt liegen nur Sekunden zur\u00fcck."""
        L = self.log
        if L.nul_bytes or not L.n:
            return False
        try:
            age = time.time() - os.path.getmtime(L.path)
        except OSError:
            return False
        last = (dt.datetime.now() - L.at(L.duration)).total_seconds()
        lim = max(120.0, 5 * (L.interval or 2.0))
        return 0 <= age <= lim and -60 <= last <= lim

    def check_gaps(self):
        L = self.log
        if L.n < 3 or not L.interval:
            return
        fac = self.cfg["log_luecke_faktor"]
        lim = max(L.interval * fac, L.interval + 1.0)
        warn_s = self.cfg["log_luecke_warnung_s"]
        gaps = []
        for i in range(1, L.n):
            d = L.t[i] - L.t[i - 1]
            if d > lim:
                gaps.append((L.t[i - 1], L.t[i], d))
            elif d < 0:
                self.add(1, "Log", "Zeitstempel springt zur\u00fcck",
                         f"Bei {self.clk(L.t[i])} springt die Zeit um {fnum(-d, 1)} s zur\u00fcck (Uhrzeitumstellung/Synchronisation?).",
                         t=L.t[i])
        self.gaps = [(a, b) for a, b, _ in gaps]
        if L.source == "LHM":
            # Dauerlog: jede Luecke wird spaeter mit dem Ereignisprotokoll eingeordnet (classify_breaks)
            self.breaks = [(a, b) for a, b, _ in gaps]
            return
        if not gaps:
            return
        big = [g for g in gaps if g[2] >= warn_s]
        lvl = 2 if big else 1
        lst = ", ".join(f"{self.clk(a)} ({fnum(d, 1)} s)" for a, b, d in gaps[:12])
        more = f" und {len(gaps) - 12} weitere" if len(gaps) > 12 else ""
        tool = "LHM" if L.source == "LHM" else "HWiNFO"
        self.add(lvl, "Log", f"{len(gaps)} L\u00fccke(n) im Log, l\u00e4ngste {fnum(max(g[2] for g in gaps), 1)} s",
                 f"Normal sind {fnum(L.interval, 1)} s zwischen zwei Messpunkten. L\u00fccken entstehen, wenn das System "
                 f"oder {tool} kurz h\u00e4ngt (Freeze, Treiber-Reset, Standby). Stellen: {lst}{more}.",
                 t=gaps[0][0], marks=[g[0] for g in gaps])

    def check_whea(self):
        c = self.s.whea
        if not c:
            if self.log.source == "LHM":
                self.add(0, "WHEA", "WHEA kommt aus dem Ereignisprotokoll",
                         "LHM hat keinen WHEA-Z\u00e4hler. Gemeldete Hardwarefehler stehen aber im Windows-Ereignisprotokoll "
                         "(WHEA-Logger) und werden dort gepr\u00fcft \u2013 siehe Abschnitt Ereignisse.")
            else:
                self.add(0, "WHEA", "Kein WHEA-Z\u00e4hler im Log", "In HWiNFO den Sensor \u201eWindows Hardware Errors (WHEA)\u201c aktivieren.")
            return
        v, t = c.vals, self.log.t
        valid = [(t[i], x) for i, x in enumerate(v) if x == x]
        start, end = valid[0][1], valid[-1][1]
        incs = [valid[k][0] for k in range(1, len(valid)) if valid[k][1] > valid[k - 1][1]]
        if end > start:
            self.add(3, "WHEA", f"{int(end - start)} WHEA-Hardwarefehler w\u00e4hrend des Logs",
                     "Windows hat korrigierte oder unkorrigierte Hardwarefehler gemeldet (CPU, Speicher, PCIe). "
                     f"Zeitpunkte: {', '.join(self.clk(x) for x in incs[:10])}. Details im Ereignisprotokoll (WHEA-Logger).",
                     t=incs[0] if incs else None, marks=incs)
        elif start > 0:
            self.add(1, "WHEA", f"WHEA-Z\u00e4hler steht bei {int(start)} (vor Logbeginn)",
                     "W\u00e4hrend des Logs kamen keine neuen hinzu. Die vorhandenen stammen aus der Zeit vor dem Logstart.")
        else:
            self.add(0, "WHEA", "Keine WHEA-Fehler", "WHEA-Z\u00e4hler blieb w\u00e4hrend des gesamten Logs bei 0.")

    def check_pcie(self):
        s, L = self.s, self.log
        if not s.pcie:
            return
        trans = self.trans
        self.pcie_err_load, self.pcie_err_trans = 0, 0
        sev = {"Fatal Error Count": 3, "Non-Fatal Error Count": 2, "Unsupported Request Count": 1, "Recovery Count": 2}
        total_err = 0
        for key, c in list(s.pcie.items()) + [(c.short, c) for c in s.pcie_lanes]:
            v = c.vals
            inc_t, inc_load = [], []
            amount, amount_load = 0, 0
            prev = None
            for i, x in enumerate(v):
                if x != x:
                    continue
                if prev is not None and x > prev:
                    d = x - prev
                    amount += d
                    inc_t.append(L.t[i])
                    if self._steady_load(i, radius=1):
                        amount_load += d
                        inc_load.append(L.t[i])
                prev = x
            if not amount:
                continue
            base = sev.get(key, 2)
            if key != "Recovery Count":
                total_err += amount
                self.pcie_err_load += amount_load if trans is not None else amount
                self.pcie_err_trans += (amount - amount_load) if trans is not None else 0
            if trans is None:
                lvl = min(base, 1) if key == "Recovery Count" else base
                self.add(lvl, "PCIe", f"GPU-PCIe: {key} +{int(amount)}",
                         f"Zuordnung zu Linkwechseln nicht m\u00f6glich (keine Link-Geschwindigkeit im Log). "
                         f"Erste Erh\u00f6hung {self.clk(inc_t[0])}.", t=inc_t[0], marks=inc_t)
            elif amount_load == 0:
                self.add(3 if base == 3 else 0, "PCIe", f"GPU-PCIe: {key} +{int(amount)} nur bei Lastwechseln",
                         "Alle Erh\u00f6hungen fielen in Phasen mit Wechsel der Link-Geschwindigkeit oder ohne volle GPU-Last "
                         "(Energiesparen, Men\u00fcs, Ladebildschirme). Bei Recovery/NAK/Bad TLP ist das normal.", t=inc_t[0])
            else:
                lvl = 3 if base == 3 else 2 if base >= 2 else 1
                self.add(lvl, "PCIe", f"GPU-PCIe: {key} +{int(amount_load)} unter Dauerlast",
                         f"{int(amount_load)} von {int(amount)} Erh\u00f6hungen passierten bei durchgehender GPU-Last ohne "
                         "Linkwechsel. Das deutet auf Signalprobleme (Riser, Slot, Kontakt, Board) oder Link-Retraining im Betrieb. "
                         f"Zeitpunkte: {', '.join(self.clk(x) for x in inc_load[:10])}.",
                         t=inc_load[0], marks=inc_load)
        if total_err == 0 and s.pcie:
            self.add(0, "PCIe", "GPU-PCIe: keine \u00dcbertragungsfehler", "Receiver-, LCRC-, Replay-, NAK- und Correctable-Z\u00e4hler blieben unver\u00e4ndert.")
        # Link unter Last herabgestuft
        if s.gpu_link and s.gpu_load:
            mx = s.gpu_link.vmax
            lv, gv = s.gpu_link.vals, s.gpu_load.vals
            low = [L.t[i] for i in range(L.n) if gv[i] == gv[i] and gv[i] >= 80 and lv[i] == lv[i] and lv[i] < mx]
            if len(low) > 2:
                self.add(2, "PCIe", f"PCIe-Link unter Last herabgestuft ({len(low)} Messpunkte)",
                         f"Bei GPU-Last \u2265 80 % lief der Link zeitweise unter {fnum(mx, 1)} GT/s.", t=low[0], marks=low[:20])

    def check_throttle(self):
        s, L = self.s, self.log
        for c in s.cpu_throttle:
            hits = [L.t[i] for i, x in enumerate(c.vals) if x == 1.0]
            if hits:
                ext = "PROCHOT EXT" in c.name.upper()
                self.add(2, "CPU", f"CPU-Drosselung: {c.short} ({len(hits)} Messpunkte)",
                         ("Externes PROCHOT kommt vom Mainboard (z. B. \u00fcberhitzte Spannungswandler). " if ext else
                          "Die CPU hat sich wegen Temperatur/Strom selbst gebremst. ") +
                         f"Erstmals {self.clk(hits[0])}.", t=hits[0], marks=hits[:30])
        if s.gpu_thermal_limit:
            hits = [L.t[i] for i, x in enumerate(s.gpu_thermal_limit.vals) if x == 1.0]
            if hits:
                self.add(2, "GPU", f"GPU thermisch begrenzt ({len(hits)} Messpunkte)",
                         f"Erstmals {self.clk(hits[0])}.", t=hits[0], marks=hits[:30])
        if s.gpu_power_limit:
            k = sum(1 for x in s.gpu_power_limit.vals if x == 1.0)
            if k:
                self.add(0, "GPU", f"GPU am Leistungslimit ({k * 100 // max(1, L.n)} % der Zeit)",
                         "Normales Verhalten unter Volllast, kein Fehler.")

    def _temp(self, c, key, label, cat, levels=None, note=""):
        if not c:
            return
        r = self._exceed(c, levels or self.cfg["temperaturen"][key])
        if not r:
            return
        lvl, best, tb, tf, k, thr = r
        if lvl:
            self.add(lvl, cat, f"{label}: max {fnum(best, 1)} \u00b0C",
                     f"Grenzwert {thr} \u00b0C ({LEVELS[lvl]}) erstmals {self.clk(tf)} \u00fcberschritten, "
                     f"insgesamt {self._samples_dur(k)} dar\u00fcber.{note}", t=tf, marks=[tb])

    def drive_levels(self, d):
        """Grenzwerte eines Laufwerks: Konfiguration, gedeckelt durch die vom Laufwerk selbst gemeldeten Grenzen."""
        lv = list(self.cfg["temperaturen"][d["type"]])
        lim = d.get("limits") or {}
        if isnum(lim.get("warn", NAN)):
            lv[1] = min(lv[1], lim["warn"])
        if isnum(lim.get("krit", NAN)):
            lv[2] = min(lv[2], lim["krit"])
        lv[1] = min(lv[1], lv[2])
        lv[0] = min(lv[0], lv[1])
        return lv

    @staticmethod
    def drive_sensors(d):
        return [c for c in [d["temp"], d["temp2"]] + list(d.get("extra") or []) if c]

    def check_temps(self):
        s = self.s
        self._temp(s.tctl, cpu_temp_key(self.cfg, s.is_x3d), "CPU-Temperatur", "CPU")
        self._temp(s.gpu_temp, "gpu_kern", "GPU-Temperatur", "GPU")
        self._temp(s.gpu_hot, "gpu_hotspot", "GPU-Hotspot", "GPU")
        self._temp(s.gpu_mem, "gpu_speicher", "GPU-Speicher (Junction)", "GPU")
        for d in s.dimms:
            self._temp(d["temp"], "ram", f"RAM {d['label']}", "RAM")
        for d in s.drives:
            lv, lim = self.drive_levels(d), d.get("limits") or {}
            note = (f" Das Laufwerk selbst meldet Warnung ab {fnum(lim['warn'], 0)} \u00b0C"
                    + (f" und kritisch ab {fnum(lim['krit'], 0)} \u00b0C." if "krit" in lim else ".")) if "warn" in lim else ""
            for k, c in enumerate(self.drive_sensors(d)):
                self._temp(c, d["type"], f"SSD {d['label']}" + (f" (Sensor {k + 1})" if k else ""), "Laufwerk",
                           levels=lv, note=note)
        for c in s.vrm:
            self._temp(c, "vrm", f"Board {c.short}", "Board")
        for c in s.chipset:
            self._temp(c, "chipsatz", f"Board {c.short}", "Board")

    def _rail(self, c, nom, label, cat):
        tol = self.cfg["schienen_toleranz_prozent"]
        v, t = c.vals, self.log.t
        worst, wi = 0.0, None
        k = [0, 0, 0]
        first = [None, None, None]
        for i, x in enumerate(v):
            if x != x or x <= 0:
                continue
            dev = (x / nom - 1) * 100
            if abs(dev) > abs(worst):
                worst, wi = dev, i
            for j in range(3):
                if abs(dev) >= tol[j]:
                    k[j] += 1
                    if first[j] is None:
                        first[j] = t[i]
        lvl = 3 if k[2] else 2 if k[1] else 1 if k[0] else 0
        if lvl:
            j = lvl - 1
            self.add(lvl, cat, f"{label}: {fnum(c.vmin, 3)}\u2013{fnum(c.vmax, 3)} V ({worst:+.1f} %)".replace(".", ","),
                     f"Abweichung vom Sollwert {fnum(nom, 1)} V \u00fcber {tol[j]} % ({self._samples_dur(k[j])}), "
                     f"erstmals {self.clk(first[j])}. Board-Sensoren messen oft 1\u20132 % ungenau; aussagekr\u00e4ftig sind "
                     "Einbr\u00fcche unter Last oder Abweichungen, die auch die Grafikkarte misst.", t=first[j], marks=[t[wi]])

    def _vmax(self, c, levels, label, cat, note=""):
        if not c:
            return
        r = self._exceed(c, levels)
        if r and r[0]:
            lvl, best, tb, tf, k, thr = r
            self.add(lvl, cat, f"{label}: max {fnum(best, 3)} V",
                     f"\u00dcber {fnum(thr, 2)} V ab {self.clk(tf)} ({self._samples_dur(k)}). {note}", t=tf, marks=[tb])

    def _vmin(self, c, levels, label, cat, note=""):
        if not c:
            return
        r = self._exceed(c, levels, above=False)
        if r and r[0]:
            lvl, best, tb, tf, k, thr = r
            self.add(lvl, cat, f"{label}: min {fnum(best, 3)} V",
                     f"Unter {fnum(thr, 2)} V ab {self.clk(tf)} ({self._samples_dur(k)}). {note}", t=tf, marks=[tb])

    def check_voltages(self):
        s, sp = self.s, self.cfg["spannungen"]
        for c, nom, lab in s.rails:
            self._rail(c, nom, lab, "Netzteil")
        for c, nom, lab in s.gpu_rails:
            self._rail(c, nom, lab, "Netzteil")
        self._vmax(s.vsoc or s.vsoc_board, sp["soc_max"], "CPU-SoC-Spannung", "CPU",
                   "AMD empfiehlt f\u00fcr VDDCR_SOC h\u00f6chstens 1,30 V.")
        vc = s.vcore or s.vcore_board
        self._vmax(vc, sp["vcore_max_x3d"] if s.is_x3d else sp["vcore_max"], "CPU-Kernspannung", "CPU")
        self._vmax(s.vddio, sp["ram_max"], "VDDIO_MEM", "RAM")
        for d in s.dimms:
            self._vmax(d["vdd"], sp["ram_max"], f"RAM {d['label']} VDD", "RAM")
            self._vmax(d["vddq"], sp["ram_max"], f"RAM {d['label']} VDDQ", "RAM")
            self._vmin(d["vin"], sp["ram_vin_min"], f"RAM {d['label']} Eingang (VIN)", "RAM",
                       "VIN ist die 5-V-Versorgung des Moduls vom Mainboard.")
        self._vmin(s.vbat, sp["cmos_batterie_min"], "CMOS-Batterie", "Board",
                   "Eine schwache CMOS-Batterie kann BIOS-Einstellungen verlieren lassen.")

    def check_dimms(self):
        s, L = self.s, self.log
        exp = int(self.cfg.get("erwartete_riegel") or 0)
        n = len(s.dimms)
        total_gb = NAN
        if s.mem_used and s.mem_avail:
            tot = [a + b for a, b in zip(s.mem_used.vals, s.mem_avail.vals) if a == a and b == b]
            if tot:
                total_gb = max(tot) / 1024
        if n:
            chans = sorted({d["channel"] for d in s.dimms if d["channel"]})
            desc = ", ".join(d["label"] for d in s.dimms)
            ref, _ = self.ram_reference()
            if exp and n < exp and isnum(total_gb) and isnum(ref) and total_gb >= ref * 0.85:
                # Speicher vollst\u00e4ndig, nur ein Sensorchip antwortet nicht (z. B. zwei Programme am SMBus)
                miss = sorted({"A", "B"} - set(chans)) if exp == 2 and chans else []
                self.add(1, "RAM", ("Sensor von Kanal " + ", ".join(miss) if miss else f"Nur {n} von {exp} RAM-Sensoren")
                         + f" fehlt \u2013 Arbeitsspeicher aber vollst\u00e4ndig ({fnum(total_gb, 1)} GB)",
                         f"Erkannt: {desc}. Der volle Speicher ist nutzbar, beide Kan\u00e4le laufen also; nur der Sensorchip des "
                         "anderen Moduls hat nicht geantwortet. H\u00e4ufige Ursache: LHM und HWiNFO lesen gleichzeitig den "
                         "gemeinsamen Sensor-Bus (SMBus), HWiNFO blendet den Sensor nach Lesefehlern aus. HWiNFO allein neu "
                         "starten; kommt der Sensor dauerhaft nicht wieder, Modul/Steckplatz im Auge behalten.")
            elif exp and n < exp:
                self.add(3, "RAM", f"Nur {n} von {exp} RAM-Modulen erkannt",
                         f"Erkannt: {desc}. Fehlt ein Modul pl\u00f6tzlich, ist ein Speicherkanal ausgefallen oder nicht trainiert.")
            else:
                self.add(0, "RAM", f"{n} RAM-Module erkannt" + (f" (Kan\u00e4le {', '.join(chans)})" if chans else ""),
                         f"{desc}" + (f" \u00b7 {fnum(total_gb, 1)} GB nutzbar" if isnum(total_gb) else ""))
        elif exp:
            self.add(1, "RAM", "Keine RAM-Modul-Sensoren im Log",
                     "In HWiNFO die DIMM-Sensoren (SPD Hub/PMIC) aktivieren, sonst l\u00e4sst sich ein Kanalausfall im Log nicht erkennen.")
        self._check_ram_total(total_gb, n)
        self._check_dimm_asym()
        for d in s.dimms:
            for c in d["flags"]:
                hits = [L.t[i] for i, x in enumerate(c.vals) if x == 1.0]
                if hits:
                    self.add(3, "RAM", f"RAM {d['label']}: {c.short}",
                             f"Der Spannungsregler (PMIC) des Moduls hat einen Fehler gemeldet, erstmals {self.clk(hits[0])}.",
                             t=hits[0], marks=hits[:20])
            if d["temp"] is None:
                self.add(1, "RAM", f"RAM {d['label']}: kein Temperatursensor", "")

    def ram_reference(self):
        """Sollwert f\u00fcr den nutzbaren RAM: Konfiguration/Windows-Bestand, sonst das vorige Log."""
        exp = self.expected_ram_gb()
        if exp:
            return exp, "erwartet"
        if self.prev and self.prev.get("fp", {}).get("RAM nutzbar"):
            try:
                return float(self.prev["fp"]["RAM nutzbar"].split()[0].replace(",", ".")), "im vorigen Log"
            except ValueError:
                pass
        return NAN, ""

    def expected_ram_gb(self):
        return float(self.cfg.get("ram_gb_erwartet") or self.cfg.get("_ram_gb_auto") or 0)

    def _check_ram_total(self, total_gb, n):
        """Nutzbarer RAM gegen den Sollwert: Ein nicht eingemessener Kanal halbiert den Speicher, die Sensorchips beider
        Module k\u00f6nnen trotzdem weiter antworten \u2013 die Modulanzahl allein reicht deshalb nicht."""
        if not isnum(total_gb):
            return
        ref, src = self.ram_reference()
        # Windows meldet etwas weniger als verbaut (Firmware, iGPU); erst ab 15 % fehlt wirklich ein Modul
        if isnum(ref) and total_gb < ref * 0.85:
            sens = f" Die Sensoren zeigen trotzdem {n} Module: Der Kanal antwortet noch, wurde aber beim Start nicht " \
                   "eingemessen." if n >= int(self.cfg.get("erwartete_riegel") or 0) > 0 else ""
            self.add(3, "RAM", f"Nur {fnum(total_gb, 1)} GB Arbeitsspeicher nutzbar ({fnum(ref, 1)} GB {src})",
                     "Es fehlt ungef\u00e4hr ein Modul bzw. ein Speicherkanal \u2013 das typische Bild eines ausgefallenen "
                     "Kanals." + sens)

    def _check_dimm_asym(self):
        """Gleiche Module im selben Log verglichen: Weicht eines deutlich ab, ist das das Fr\u00fchwarnzeichen."""
        dims = [d for d in self.s.dimms if d.get("vin") or d.get("vdd") or d.get("temp")]
        if len(dims) < 2:
            return
        lim = self.cfg.get("ram_abweichung") or {}
        for key, unit, dec, what in (("vin", "V", 3, "Eingangsspannung (VIN)"), ("vdd", "V", 3, "Modulspannung (VDD)"),
                                     ("temp", "\u00b0C", 1, "Temperatur")):
            cs = [(d, d[key]) for d in dims if d.get(key) and d[key].n_valid >= 3]
            if len(cs) < 2 or len(lim.get(key) or []) < 3:
                continue
            # Durchschnitt und (bei Spannungen) Minimum vergleichen \u2013 Einbr\u00fcche zeigen sich zuerst im Minimum
            stats = [("\u00d8", lambda c: c.vavg)] + ([("Tiefstwerte", _low_pct)] if key != "temp" else [])
            best = None
            for name, f in stats:
                vals = sorted(((f(c), d) for d, c in cs if isnum(f(c))), key=lambda x: x[0])
                if len(vals) < 2:
                    continue
                diff = vals[-1][0] - vals[0][0]
                if best is None or diff > best[0]:
                    best = (diff, name, vals[0], vals[-1])
            if not best:
                continue
            diff, name, lo, hi = best
            lvl = next((l for l in (3, 2, 1) if diff >= lim[key][l - 1]), 0)
            if not lvl:
                continue
            odd = hi[1] if key == "temp" else lo[1]
            self.add(lvl, "RAM", f"RAM {odd['label']}: {what} weicht um {fnum(diff, dec)} {unit} vom anderen Modul ab",
                     f"{name} {fnum(lo[0], dec)} {unit} ({lo[1]['label']}) gegen\u00fcber {fnum(hi[0], dec)} {unit} "
                     f"({hi[1]['label']}). Gleiche Module am selben Board sollten fast gleiche Werte haben; ein Modul oder "
                     "Steckplatz, der wegdriftet, f\u00e4llt so auf, bevor feste Grenzwerte greifen.")

    def check_drives(self):
        s, L = self.s, self.log
        lim = self.cfg["ssd_restlebensdauer_min"]
        for d in s.drives:
            for key, lvl, txt in (("fail", 3, "meldet SMART-Fehler"), ("warn", 2, "meldet SMART-Warnung")):
                c = d[key]
                if c:
                    hits = [L.t[i] for i, x in enumerate(c.vals) if x == 1.0]
                    if hits:
                        self.add(lvl, "Laufwerk", f"SSD {d['label']} {txt}", "Sofort Datensicherung pr\u00fcfen.", t=hits[0])
            c = d["life"]
            if c and isnum(c.vmin):
                lvl = 3 if c.vmin <= lim[2] else 2 if c.vmin <= lim[1] else 1 if c.vmin <= lim[0] else 0
                if lvl:
                    self.add(lvl, "Laufwerk", f"SSD {d['label']}: Restlebensdauer {fnum(c.vmin, 0)} %", "")

    def check_fans(self):
        s, L = self.s, self.log
        tctl = s.tctl.vals if s.tctl else None
        for c in s.fans:
            v = c.vals
            if not (c.vmax > 300):
                continue
            seen_run, stops = False, []
            for i, x in enumerate(v):
                if x != x:
                    continue
                if x > 300:
                    seen_run = True
                elif x < 50 and seen_run:
                    stops.append(L.t[i])
            if len(stops) >= 2:
                crit = re.search(r"CPU|Pump|AIO|OPT", c.name, re.I)
                hot = tctl is not None and max((x for x in tctl if x == x), default=0) > 60
                self.add(2 if crit and hot else 1, "L\u00fcfter", f"L\u00fcfter {c.short} stand zeitweise ({len(stops)} Messpunkte)",
                         "Bei Geh\u00e4usel\u00fcftern mit Stopp-Modus normal; bei CPU-L\u00fcfter/Pumpe nicht.", t=stops[0], marks=stops[:20])
        if s.gpu_temp:
            gt = s.gpu_temp.vals
            for c in s.gpu_fans:
                hits = [L.t[i] for i, x in enumerate(c.vals) if x == x and x < 50 and gt[i] == gt[i] and gt[i] >= 72]
                if len(hits) >= 2:
                    self.add(2, "GPU", f"GPU-L\u00fcfter {c.short} stand bei \u2265 72 \u00b0C", "", t=hits[0], marks=hits[:20])

    def check_frametimes(self):
        s, L = self.s, self.log
        cols = [c for c in [s.ft_avg] + list(s.ft_hi) if c]
        if not cols:
            return
        thr = self.cfg["frametime_spitze_ms"]
        n = L.n
        peak = [NAN] * n
        for c in cols:
            v = c.vals
            for i in range(n):
                x = v[i]
                if x == x and (peak[i] != peak[i] or x > peak[i]):
                    peak[i] = x
        events, i = [], 0
        while i < n:
            if peak[i] == peak[i] and peak[i] > thr:
                j = i
                while j + 1 < n and peak[j + 1] == peak[j + 1] and peak[j + 1] > thr:
                    j += 1
                events.append((i, j, max(peak[i:j + 1])))
                i = j + 1
            else:
                i += 1
        if not events:
            if s.ft_avg:
                self.add(0, "Spiel", f"Keine Frametime-Spitzen \u00fcber {thr} ms", "")
            return
        # "im Spiel" = GPU-Last >= 50 % und kein Linkwechsel im Umfeld von +-5 Messpunkten (~10 s)
        g = s.gpu_load.vals if s.gpu_load else None
        rad = max(2, int(round(10 / (L.interval or 2))))
        play, idle = [], []
        for a, b, m in events:
            lo_, hi_ = max(0, a - rad), min(n, b + rad + 1)
            gmin = min((g[k] for k in range(lo_, hi_) if g is not None and g[k] == g[k]), default=100)
            tr = self.trans is not None and any(self.trans[k] for k in range(lo_, hi_))
            (idle if (gmin < 50 or tr) else play).append((L.t[a], m))
        if idle:
            self.add(0, "Spiel", f"{len(idle)} Frametime-Spitze(n) bei Lade-/Men\u00fcphasen oder auf dem Desktop",
                     "In der N\u00e4he lag die GPU-Last niedrig oder der PCIe-Link wechselte \u2013 typisch f\u00fcr Ladebildschirme. "
                     + ("PresentMon verfolgte zuletzt den Desktop (dwm.exe); der zeichnet im Leerlauf nur bei \u00c4nderungen neu, "
                        "lange \u201eFrametimes\u201c sind dort normal. " if s.pm_desktop else "")
                     + "; ".join(f"{self.clk(t)}: {fnum(m, 0)} ms" for t, m in idle[:10]), t=idle[0][0])
        if play:
            load_h = max(1e-6, sum(1 for x in self.load if x) * (L.interval or 2) / 3600)
            rate = len(play) / load_h
            worst = max(m for _, m in play)
            lvl = 2 if worst >= 3000 else 1 if (worst >= 1000 or (len(play) >= 3 and rate > 6)) else 0
            self.add(lvl, "Spiel", f"{len(play)} Ruckler \u00fcber {thr} ms im laufenden Spiel (l\u00e4ngster {fnum(worst, 0)} ms)",
                     f"Etwa {fnum(rate, 1)} pro Stunde unter Last. Einzelne Spitzen sind meist Shader-Kompilierung oder Nachladen; "
                     "Spitzen \u00fcber 1 s oder geh\u00e4uftes Auftreten lohnen einen Blick auf L\u00fccken, PCIe-Z\u00e4hler und "
                     "Ereignisse zur selben Zeit. " + "; ".join(f"{self.clk(t)}: {fnum(m, 0)} ms" for t, m in play[:10]),
                     t=play[0][0], marks=[t for t, _ in play[:40]] if lvl else [])

    def check_dropouts(self):
        s, L = self.s, self.log
        n = L.n
        key = [(s.tctl, 1), (s.gpu_temp, 1), (s.vcore, 1), (s.whea, 1)]
        key += [(c, 1) for c, _, _ in s.rails[:3]]
        # RAM-Module: alle Sensoren eines Moduls gemeinsam betrachten
        for d in s.dimms:
            dc = [c for c in (d["temp"], d["vdd"], d["vddq"], d["vin"]) if c]
            if not dc:
                continue
            first_gap = None
            for c in dc:
                v = c.vals
                valid = [i for i in range(n) if v[i] == v[i]]
                if not valid:
                    continue
                lv = valid[-1]
                if n - 1 - lv >= 3:
                    first_gap = lv + 1 if first_gap is None else min(first_gap, lv + 1)
                else:
                    i, fv = valid[0], valid[0]
                    while i <= lv:
                        if v[i] != v[i]:
                            j = i
                            while j <= lv and v[j] != v[j]:
                                j += 1
                            if j - i >= 3:
                                first_gap = i if first_gap is None else min(first_gap, i)
                                break
                            i = j
                        else:
                            i += 1
            if first_gap is not None:
                gone = all(all(x != x for x in c.vals[first_gap:]) for c in dc)
                self.add(2, "RAM", f"RAM {d['label']}: Sensoren liefern " + ("ab " if gone else "zeitweise ab ")
                         + f"{self.clk(L.t[first_gap])} keine Werte",
                         ("Alle Sensoren des Moduls sind bis Logende verstummt. " if gone else "Mehrere Messpunkte ohne Werte. ")
                         + "HWiNFO liest diese Werte \u00fcber den SMBus vom Modul. Ein Ausfall kann auf verlorene Kommunikation "
                         "mit dem Modul bzw. Speicherkanal hindeuten, manchmal aber auch auf Software, die gleichzeitig auf den "
                         "SMBus zugreift (RGB-Tools).", t=L.t[first_gap])
        seen = set()
        for c, lvl in key:
            if not c or c.idx in seen:
                continue
            seen.add(c.idx)
            v = c.vals
            valid = [i for i in range(n) if v[i] == v[i]]
            if not valid:
                continue
            fv, lv = valid[0], valid[-1]
            runs, i = [], fv
            while i <= lv:
                if v[i] != v[i]:
                    j = i
                    while j <= lv and v[j] != v[j]:
                        j += 1
                    if j - i >= 3:
                        runs.append((i, j - 1))
                    i = j
                else:
                    i += 1
            tail = n - 1 - lv
            if tail >= 3:
                runs.append((lv + 1, n - 1))
            if runs:
                a = runs[0][0]
                self.add(lvl, "Sensor", f"Sensor ohne Werte: {c.short}" + (f" ({c.group[:40]})" if c.group else ""),
                         f"{len(runs)} Aussetzer, erster ab {self.clk(L.t[a])}. Bei RAM-Sensoren kann das auf einen "
                         "Kommunikationsverlust mit dem Modul hindeuten.", t=L.t[a], marks=[L.t[r[0]] for r in runs[:20]])

    def check_end(self):
        """Zustand in den letzten Sekunden vor Logende (wichtig nach Abst\u00fcrzen)."""
        s, L = self.s, self.log
        n = L.n
        roles = [("CPU \u00b0C", s.tctl, 1), ("PPT W", s.ppt, 1), ("CPU %", s.usage, 0), ("Vcore", s.vcore or s.vcore_board, 3),
                 ("SoC", s.vsoc or s.vsoc_board, 3), ("GPU %", s.gpu_load, 0), ("GPU W", s.gpu_power, 0),
                 ("GPU \u00b0C", s.gpu_temp, 1)]
        roles += [(lab.replace(" (Board)", ""), c, 3) for c, _, lab in s.rails[:1]]
        roles += [(lab, c, 2) for c, _, lab in s.gpu_rails[:1]]
        roles += [(f"RAM {d['channel'] or k + 1} \u00b0C", d["temp"], 1) for k, d in enumerate(s.dimms)]
        roles += [("FPS", s.fps, 0)]
        roles = [r for r in roles if r[1] is not None]
        self.end_roles = roles
        self.end_cols = [(lab, d) for lab, _, d in roles]
        self.end_rows = self.rows_before(n - 1)
        if L.footer or L.source == "LHM":
            return
        last = n - 1
        st = []
        if s.gpu_load and isnum(s.gpu_load.vals[last]):
            st.append(f"GPU-Last {fnum(s.gpu_load.vals[last], 0)} %")
        if s.usage and isnum(s.usage.vals[last]):
            st.append(f"CPU-Last {fnum(s.usage.vals[last], 0)} %")
        if s.tctl and isnum(s.tctl.vals[last]):
            st.append(f"CPU {fnum(s.tctl.vals[last], 1)} \u00b0C")
        if self.s.fps and isnum(s.fps.vals[last]):
            st.append(f"{fnum(s.fps.vals[last], 0)} FPS")
        under = self.load[last] if self.load else False
        self.add(0, "Log", "Zustand beim letzten Messpunkt" + (" (unter Last)" if under else " (Leerlauf/geringe Last)"),
                 ", ".join(st) + ".", t=L.t[last])
        self.check_rail_dips(last, "Logende")

    def check_rail_dips(self, i_end, what):
        """Spannungseinbruch in den 30 s vor i_end (Netzteil-/Versorgungs-Indiz vor Absturz oder Logende)."""
        s, L = self.s, self.log
        a = i_end
        while a > 0 and L.t[i_end] - L.t[a - 1] <= 30:
            a -= 1
        rails = s.rails[:3] + s.gpu_rails[:1] + [(d["vin"], 5.0, f"RAM {d['label']} VIN") for d in s.dimms if d["vin"]]
        for c, nom, lab in rails:
            vals = sorted(x for x in c.vals if x == x)
            if len(vals) < 20:
                continue
            p10 = pct(vals, 10)
            tail_min = min((x for x in c.vals[a:i_end + 1] if x == x), default=NAN)
            if isnum(tail_min) and tail_min < p10 * 0.985:
                self.add(1, "Netzteil", f"{lab}: Einbruch kurz vor {what} ({fnum(tail_min, 3)} V)",
                         f"\u00dcblich waren mindestens {fnum(p10, 3)} V (10. Perzentil). Ein Einbruch direkt vor einem "
                         "Absturz spricht f\u00fcr Netzteil oder Versorgung.", t=L.t[a], marks=[L.t[a]])

    def power_cycles_across(self, a, b):
        """Anstieg der Einschaltz\u00e4hler der Laufwerke zwischen Messzeit a und b (None = keine Daten)."""
        L = self.log
        i_a = max(0, bisect.bisect_right(L.t, a + 1e-6) - 1)
        i_b = bisect.bisect_left(L.t, b - 1e-6)
        best = None
        for c in self.s.power_counts:
            v = c.vals
            before = next((v[i] for i in range(i_a, -1, -1) if v[i] == v[i]), None)
            after = next((v[i] for i in range(i_b, L.n) if v[i] == v[i]), None)
            if before is None or after is None:
                continue
            d = int(round(after - before))
            best = d if best is None else max(best, d)
        return best

    @staticmethod
    def power_text(d):
        if d is None:
            return ""
        if d > 0:
            return (f" Einschaltz\u00e4hler der SSD +{d}: Der PC war dazwischen stromlos (Ausschalten, Stromausfall "
                    "oder Schutzabschaltung des Netzteils).")
        return (" Einschaltz\u00e4hler der SSD unver\u00e4ndert: Die SSD blieb mit Strom versorgt \u2013 eher H\u00e4nger "
                "mit Reset als Stromverlust.")

    @staticmethod
    def early_crash_text(sec):
        return (f" Achtung: Der Absturz kam nur {fdur(sec)} nach dem Start der Aufzeichnung, also kurz nachdem das "
                "Messprogramm gestartet wurde. Wiederholt sich das, kann das Auslesen der Sensoren (SMBus) selbst der "
                "Ausl\u00f6ser sein.")

    def check_markers(self):
        """Eigene Markierungen (markieren.bat) im Zeitraum des Logs."""
        L = self.log
        end = L.at(L.duration) + dt.timedelta(minutes=10)
        for m in self.markers:
            try:
                ts = dt.datetime.fromisoformat(m["t"])
            except (KeyError, TypeError, ValueError):
                continue
            if L.t0 - dt.timedelta(minutes=1) <= ts <= end:
                rel = L.rel_of(ts)
                self.add(0, "Markierung", f"Eigene Markierung: {m.get('text', '')[:120]}",
                         f"Gesetzt um {ts:%d.%m. %H:%M:%S}" + (" (nach Logende)" if rel > L.duration else "") + ".",
                         t=min(max(rel, 0), L.duration), marks=[min(max(rel, 0), L.duration)])

    def rows_before(self, i_end, seconds=60):
        L = self.log
        t_end = L.t[i_end]
        a = i_end
        while a > 0 and t_end - L.t[a - 1] <= seconds:
            a -= 1
        return [(L.t[i], [r[1].vals[i] for r in self.end_roles]) for i in range(a, i_end + 1)]

    def state_text(self, i):
        s = self.s
        st = []
        for c, fmt in ((s.gpu_load, "GPU-Last {} %"), (s.usage, "CPU-Last {} %")):
            if c and isnum(c.vals[i]):
                st.append(fmt.format(fnum(c.vals[i], 0)))
        if s.tctl and isnum(s.tctl.vals[i]):
            st.append(f"CPU {fnum(s.tctl.vals[i], 1)} \u00b0C")
        if s.gpu_power and isnum(s.gpu_power.vals[i]):
            st.append(f"GPU {fnum(s.gpu_power.vals[i], 0)} W")
        under = self.load[i] if self.load else False
        return ("unter Last: " if under else "Leerlauf/geringe Last: ") + ", ".join(st)

    # -- Unterbrechungen in LHM-Dauerlogs ------------------------------------
    CRASH_EV = {("Microsoft-Windows-Kernel-Power", 41), ("EventLog", 6008), ("Microsoft-Windows-WER-SystemErrorReporting", 1001)}
    CLEAN_EV = {("Microsoft-Windows-Kernel-General", 13), ("EventLog", 6006), ("User32", 1074),
                ("Microsoft-Windows-Kernel-Power", 109)}
    SLEEP_EV = {("Microsoft-Windows-Kernel-Power", 42), ("Microsoft-Windows-Kernel-Power", 107),
                ("Microsoft-Windows-Power-Troubleshooter", 1), ("Microsoft-Windows-Kernel-Power", 506),
                ("Microsoft-Windows-Kernel-Power", 507)}
    BOOT_EV = {("Microsoft-Windows-Kernel-General", 12), ("EventLog", 6005)}
    LOGOFF_EV = {("Microsoft-Windows-Winlogon", 7002), ("Microsoft-Windows-Winlogon", 7001)}
    TIME_EV = {("Microsoft-Windows-Kernel-General", 1)}
    PROC_NAMES = {"startmenuexperiencehost": "Startmenü", "explorer": "Explorer", "shutdown": "shutdown-Befehl",
                  "winlogon": "Anmeldebildschirm", "logonui": "Anmeldebildschirm", "trustedinstaller": "Windows Update",
                  "mousocoreworker": "Windows Update", "usoclient": "Windows Update", "wuauclt": "Windows Update",
                  "musnotification": "Windows Update", "sihclient": "Windows Update"}

    def _shutdown_info(self, evs):
        """Art (Neustart/Herunterfahren) und Ausloeser aus User32 1074."""
        kind, who = "", ""
        for ev in evs:
            if (ev.get("p"), ev.get("id")) != ("User32", 1074):
                continue
            msg = ev.get("msg") or ""
            low = msg.lower()
            if "neu starten" in low or "restart" in low or "neustart" in low:
                kind = "Neustart"
            elif "ausschalten" in low or "power off" in low or "herunterfahren" in low or "shutdown" in low:
                kind = "Herunterfahren"
            m = re.search(r"([\w.-]+)\.exe", msg, re.I)
            if m:
                who = self.PROC_NAMES.get(m.group(1).lower(), m.group(1))
            if "update" in low or "upgrade" in low or "service pack" in low:
                who = who or "Windows Update"
        return kind, who

    def _lhm_config_time(self):
        """LHM speichert LibreHardwareMonitor.config beim Beenden und beim Abmelden -> Zeitpunkt des letzten Endes."""
        cfg = os.path.join(os.path.dirname(os.path.abspath(self.log.path)), "LibreHardwareMonitor.config")
        try:
            return self.log.rel_of(dt.datetime.fromtimestamp(os.path.getmtime(cfg)))
        except (OSError, ValueError, OverflowError):
            return None

    def classify_breaks(self):
        L = self.log
        if L.source != "LHM" or not L.n:
            return
        end_abs = L.at(L.duration)
        now = dt.datetime.now()
        live = -300 <= (now - end_abs).total_seconds() <= 300
        cfg_rel = self._lhm_config_time()
        dst_rel = [r for r, _ in L.meta.get("dst", [])]
        items = list(self.breaks) + [(L.t[-1], None)]
        groups = {}

        def note(key, text):
            groups.setdefault(key, []).append(text)
        seg_start = 0.0
        for a, b in items:
            seg_from, seg_start = seg_start, (b if b is not None else seg_start)
            i_end = max(0, bisect.bisect_right(L.t, a + 1e-6) - 1)
            if b is None and live:
                self.add(0, "Log", "LHM-Aufzeichnung läuft noch",
                         f"Letzter Messpunkt {self.clk(a)} – die Datei wird gerade weiter beschrieben.", t=a)
                continue
            gap = (b - a) if b is not None else None
            hi = (b + 30) if b is not None else a + 3600
            win = [ev for ev in self.events if a - 5 <= ev.get("_rel", -1e18) <= hi]
            key = lambda ev: (ev.get("p"), ev.get("id"))
            crash = [ev for ev in win if key(ev) in self.CRASH_EV]
            clean = [ev for ev in win if key(ev) in self.CLEAN_EV]
            sleep = [ev for ev in win if key(ev) in self.SLEEP_EV]
            boot = [ev for ev in win if key(ev) in self.BOOT_EV]
            logoff = [ev for ev in win if key(ev) in self.LOGOFF_EV]
            timech = [ev for ev in win if key(ev) in self.TIME_EV]
            lhm_err = [ev for ev in win if ev.get("p") in ("Application Error", "Application Hang", ".NET Runtime")
                       and "librehardwaremonitor" in (ev.get("msg") or "").lower()]
            lhm_closed = cfg_rel is not None and a - 5 <= cfg_rel <= (b if b is not None else a + 3600) + 5
            span = f"{self.clk(a)}–{self.clk(b)}" if b is not None else f"ab {self.clk(a)}"
            dur = f" ({fdur(gap)})" if gap is not None else ""
            pw = self.power_cycles_across(a, b) if b is not None else None
            ptxt = self.power_text(pw)
            pnote = "" if pw is None else (", PC war stromlos" if pw > 0 else ", ohne Stromunterbrechung")
            if crash:
                info = list(dict.fromkeys(ev.get("_extra") or ev.get("_label") or "" for ev in crash))
                hang = [ev for ev in clean if ev.get("_rel", 0) <= min(c.get("_rel", 0) for c in crash)]
                if hang:
                    title = f"Hänger beim Herunterfahren um {self.clk(a)}, danach Kaltstart"
                    lvl, expl = 2, ("Windows wollte ordentlich herunterfahren (Herunterfahren eingeleitet), kam aber nicht zum "
                                    "Ende – danach unerwarteter Neustart. Meist ein Treiber oder Dienst, der beim Beenden "
                                    "hängt, oder der Ausschalter wurde gedrückt. Kein Absturz im laufenden Betrieb. ")
                else:
                    title = f"Absturz: Aufzeichnung bricht um {self.clk(a)} ab, danach unerwarteter Neustart"
                    lvl, expl = 3, ""
                early = a - seg_from <= 120 and not hang
                self.add(lvl, "Log", title,
                         expl + f"Zuletzt {self.state_text(i_end)}. Ereignisprotokoll: {'; '.join(i for i in info if i)}. "
                         + (f"Aufzeichnung lief um {self.clk(b)} wieder an." if b is not None else "") + ptxt
                         + (self.early_crash_text(a - seg_from) if early else ""), t=a, marks=[a])
                if early:
                    self.early_crashes = getattr(self, "early_crashes", 0) + 1
                if lvl == 3:
                    self.check_rail_dips(i_end, f"dem Absturz um {self.clk(a)}")
                    self.crash_tables.append((f"Letzte Minute vor dem Absturz um {self.clk(a)}", self.rows_before(i_end)))
            elif b is not None and any(abs(b - r) < 1 for r in dst_rel):
                note("dst", self.clk(b))
            elif sleep:
                note("sleep", span + dur)
            elif clean or (boot and logoff):
                kind, who = self._shutdown_info(clean)
                if not kind:
                    kind = "Neustart" if gap is not None and gap < 600 else "Herunterfahren"
                if who == "Windows Update":
                    kind = "Update-" + kind
                note(kind, span + dur + (f", ausgelöst über {who}" if who and who != "Windows Update" else ""))
            elif logoff:
                note("logoff", span + dur)
            elif lhm_err:
                self.add(1, "Log", f"LibreHardwareMonitor selbst ist abgestürzt ({span})",
                         "Windows meldet einen Programmabsturz bzw. Hänger von LHM. Das betrifft das Messprogramm, "
                         "nicht die Hardware. Häuft es sich, LHM aktualisieren.", t=a)
            elif timech and gap is not None and gap < 600:
                note("time", span + dur)
            elif boot:
                self.add(1, "Log", f"Neustart ohne Abmeldung: {span}{dur}",
                         "Im Ereignisprotokoll steht ein Systemstart, aber weder ein ordentliches Herunterfahren noch ein "
                         "Absturzeintrag. Ungewöhnlich – im Blick behalten." + ptxt, t=a)
            elif lhm_closed:
                note("lhm_closed", span + dur)
            elif b is None:
                note("end", self.clk(a))
            elif gap < 10:
                note("short", f"{self.clk(a)} ({fdur(gap)})")
            elif not self.events_ok:
                note("unknown", span + dur + pnote)
            elif gap < 600:
                self.add(2, "Log", f"Aufzeichnung {fdur(gap)} unterbrochen ohne Neustart",
                         f"{span}: Kein Neustart, kein Abmelden, kein Standby im Ereignisprotokoll. Möglich ist ein "
                         f"Hänger des Systems – oder LHM wurde von Hand beendet und neu gestartet. "
                         f"Zuletzt {self.state_text(i_end)}." + ptxt, t=a, marks=[a])
            else:
                note("unknown", span + dur + pnote)

        texts = {
            "Neustart": (0, "Neustart", "Ordentlich neu gestartet (Herunterfahren und Systemstart stehen im Ereignisprotokoll)."),
            "Update-Neustart": (0, "Update-Neustart", "Neustart durch Windows Update."),
            "Herunterfahren": (0, "Herunterfahren und wieder eingeschaltet", "Ordentlich heruntergefahren."),
            "Update-Herunterfahren": (0, "Herunterfahren durch Windows Update", "Von Windows Update ausgelöst."),
            "logoff": (0, "Ab- und wieder angemeldet", "LHM läuft in deiner Benutzersitzung und pausiert, solange niemand "
                                                      "angemeldet ist."),
            "sleep": (0, "Energiesparmodus", "Währenddessen schläft der PC und LHM zeichnet nichts auf."),
            "dst": (0, "Zeitumstellung", "Sommer-/Winterzeit: Die Zeitachse läuft im Bericht durch, angezeigt wird die Uhrzeit "
                                          "der Wanduhr."),
            "time": (0, "Uhrzeit korrigiert", "Windows hat die Systemzeit angepasst (Zeitsynchronisation) – keine echte Pause."),
            "lhm_closed": (0, "LHM beendet und wieder gestartet", "LHM hat beim Beenden seine Einstellungen gespeichert "
                                                                  "(LibreHardwareMonitor.config) – kein Systemereignis."),
            "short": (0, "kurzer Aussetzer", "LHM hat ein paar Sekunden nichts geschrieben, Windows hat dazu nichts protokolliert. "
                                             "Das passiert gelegentlich in LHM selbst, z. B. wenn ein Sensor langsam antwortet. "
                                             "Erst eine Häufung wäre auffällig."),
            "unknown": (0, "Unterbrechung ohne Ereignis" if self.events_ok else "Unterbrechung (ohne Ereignisprotokoll nicht zuzuordnen)",
                        "Kein Absturzeintrag gefunden – z. B. LHM von Hand beendet oder PC ausgeschaltet, während LHM "
                        "nicht lief." if self.events_ok else
                        "Unter Windows mit Ereignisprotokoll unterscheidet das Tool Neustart, Abmelden, Standby und Absturz."),
            "end": (0, "Aufzeichnung endet" if self.events_ok else "Aufzeichnung endet (ohne Ereignisprotokoll nicht zuzuordnen)",
                    "Nach dem letzten Messpunkt kein Absturzeintrag – z. B. PC ausgeschaltet oder LHM beendet."
                    if self.events_ok else "Unter Windows prüft das Tool hier, ob danach ein Absturz, ein Neustart oder "
                                           "Standby folgte."),
        }
        hours = max(L.duration / 3600.0, 1.0)
        for k_, lst in groups.items():
            lvl, lab, expl = texts[k_]
            if k_ == "short" and (len(lst) > 3 and len(lst) / hours > 1):
                lvl = 1
                expl += f" Hier {len(lst)}× in {fdur(L.duration)} – das ist mehr als üblich."
            if k_ == "end":
                self.add(lvl, "Log", f"{lab} um {lst[0]}", expl)
                continue
            self.add(lvl, "Log", f"{len(lst)}× {lab}", expl + " Zeiträume: " + ", ".join(lst[:12])
                     + (f" und {len(lst) - 12} weitere" if len(lst) > 12 else "") + ".")

    def build_fingerprint(self):
        s = self.s
        fp = {}
        if self.log.source == "LHM":
            fp["Datenquelle"] = "LibreHardwareMonitor"
        if s.cpu_name:
            fp["CPU"] = s.cpu_name
        if s.board_name:
            fp["Mainboard"] = s.board_name
        if s.gpu_name:
            fp["Grafikkarte"] = s.gpu_name
        if s.dimms:
            mods = sorted({d["module"] for d in s.dimms if d["module"]})
            fp["RAM-Module"] = (f"{len(s.dimms)} \u00d7 " + ", ".join(mods) if mods else f"{len(s.dimms)} Module") + \
                (f" (Kan\u00e4le {', '.join(sorted({d['channel'] for d in s.dimms if d['channel']}))})" if any(d["channel"] for d in s.dimms) else "")
        if s.mem_used and s.mem_avail:
            tot = [a + b for a, b in zip(s.mem_used.vals, s.mem_avail.vals) if a == a and b == b]
            if tot:
                fp["RAM nutzbar"] = f"{fnum(max(tot) / 1024, 1)} GB"
        corr = 100.0 / s.bus.vavg if s.bus and isnum(s.bus.vavg) and 90 < s.bus.vavg < 110 else 1.0
        if s.mem_clock and isnum(s.mem_clock.vavg):
            fp["RAM-Takt"] = f"{int(round(s.mem_clock.vavg * 2 * corr / 100.0) * 100)} MT/s"
        tm = [s.timings[k] for k in ("Tcas", "Trcd", "Trp", "Tras")]
        if all(tm):
            fp["Timings"] = "-".join(str(int(round(c.vavg))) for c in tm) + (f" CR{int(round(s.cr.vavg))}" if s.cr else "")
        if s.fclk and isnum(s.fclk.vavg):
            fp["FCLK"] = f"{int(round(s.fclk.vavg * corr))} MHz"
        if s.uclk and s.mem_clock and isnum(s.uclk.vavg) and isnum(s.mem_clock.vavg):
            r = s.mem_clock.vavg / s.uclk.vavg if s.uclk.vavg else 0
            fp["UCLK:MEMCLK"] = "1:1" if abs(r - 1) < 0.05 else "1:2" if abs(r - 2) < 0.1 else f"1:{r:.2f}"

        def med(c):
            v = sorted(x for x in c.vals if x == x)
            return v[len(v) // 2] if v else NAN
        if s.vsoc or s.vsoc_board:
            fp["SoC-Spannung"] = f"{fnum(med(s.vsoc or s.vsoc_board), 3)} V"
        if s.vddio:
            fp["VDDIO_MEM"] = f"{fnum(med(s.vddio), 3)} V"
        if s.dimms and s.dimms[0]["vdd"]:
            fp["RAM-VDD"] = f"{fnum(med(s.dimms[0]['vdd']), 2)} V"
        prof = self.ram_profile(med)
        if prof:
            fp["RAM-Profil"] = prof
        if s.gpu_link and isnum(s.gpu_link.vmax):
            gen = {2.5: 1, 5.0: 2, 8.0: 3, 16.0: 4, 32.0: 5}.get(round(s.gpu_link.vmax, 1))
            fp["GPU-PCIe"] = f"{fnum(s.gpu_link.vmax, 1)} GT/s" + (f" (Gen{gen})" if gen else "")
        if s.gpu_limit_nom and isnum(s.gpu_limit_nom.vavg):
            fp["GPU-Leistungslimit"] = f"{int(round(s.gpu_limit_nom.vavg))} W"
        if s.drives:
            fp["Laufwerke"] = " \u00b7 ".join(d["label"] for d in s.drives)
        self.fp = fp

    def ram_profile(self, med):
        """EXPO/XMP an oder aus, abgeleitet aus den Spannungen (DDR5 auf AM5).

        Ohne Profil (JEDEC) laufen VDDIO_MEM und RAM-VDD bei ~1,1 V und SoC bei ~1,05 V; EXPO hebt VDDIO_MEM/VDD
        auf 1,25-1,45 V und SoC meist auf 1,2-1,3 V. Dazwischen keine Aussage.
        """
        s = self.s
        if not s.dimms:
            return ""
        vio = med(s.vddio) if s.vddio else NAN
        vdd = med(s.dimms[0]["vdd"]) if s.dimms[0]["vdd"] else NAN
        soc = med(s.vsoc or s.vsoc_board) if (s.vsoc or s.vsoc_board) else NAN
        mem = max([x for x in (vio, vdd) if isnum(x)], default=NAN)
        if not isnum(mem):
            return ""
        if mem >= 1.25:
            return "EXPO/XMP vermutlich aktiv"
        if mem <= 1.2 and (not isnum(soc) or soc <= 1.15):
            return "JEDEC (EXPO/XMP vermutlich aus)"
        return ""

    def check_ram_profile(self):
        prof = self.fp.get("RAM-Profil", "")
        if prof.startswith("JEDEC"):
            vals = SEP.join(f"{k} {self.fp[k]}" for k in ("VDDIO_MEM", "RAM-VDD", "SoC-Spannung") if k in self.fp)
            takt = self.fp.get("RAM-Takt")
            self.add(0, "Konfiguration", "RAM l\u00e4uft " + ("ohne" if takt else "vermutlich ohne") + " EXPO/XMP (JEDEC-Standard)",
                     f"Abgeleitet aus den Spannungen ({vals})" + (f", RAM-Takt laut Log {takt}" if takt else "") + ". Mit aktivem EXPO l\u00e4gen VDDIO_MEM/RAM-VDD bei "
                     "1,25\u20131,45 V. Der RAM l\u00e4uft dann mit dem Standardtakt (DDR5 meist 4800 MT/s) statt mit dem "
                     "Profil des Kits. Nach Board-Tausch, BIOS-Update oder CMOS-Reset typisch; als Stabilit\u00e4tstest "
                     "aber auch sinnvoll.")

    def check_config_change(self):
        p = self.prev
        if not p or not p.get("fp"):
            return
        old = p["fp"]
        ch = []
        # Ohne Logabschluss sind die Ger\u00e4tenamen nur gesch\u00e4tzt: Namen-Felder nicht vergleichen, sonst meldet ein
        # laufendes Log scheinbar "CPU fehlt" oder ein anderes RAM-Modul
        skip = {"CPU", "Mainboard", "Grafikkarte", "Laufwerke", "RAM-Module"} \
            if self.log.group_source.startswith("gesch\u00e4tzt") else set()
        for k, v in self.fp.items():
            if k in skip:
                continue
            if k in old and old[k] != v:
                if k in ("SoC-Spannung", "VDDIO_MEM", "RAM-VDD", "RAM nutzbar"):
                    try:
                        a = float(old[k].split()[0].replace(",", "."))
                        b = float(v.split()[0].replace(",", "."))
                        if abs(a - b) < (0.6 if k == "RAM nutzbar" else 0.02):
                            continue
                    except ValueError:
                        pass
                ch.append((k, old[k], v))
        for k in old:
            if k not in self.fp and k not in ("Laufwerke",) and k not in skip:
                ch.append((k, old[k], "fehlt"))
        self.fp_changes = ch
        if ch:
            keys = {k for k, _, _ in ch}
            # weniger Modul-Sensoren bei gleichem nutzbarem RAM = Sensor fehlt, kein Kanalausfall
            ram_lost = "RAM nutzbar" in keys or ("RAM-Module" in keys and "RAM nutzbar" not in self.fp)
            lvl = 2 if ram_lost or "GPU-PCIe" in keys else \
                1 if keys & {"RAM-Module", "RAM-Takt", "RAM-Profil", "Timings", "FCLK", "UCLK:MEMCLK", "SoC-Spannung", "VDDIO_MEM", "RAM-VDD",
                             "CPU", "Mainboard", "Grafikkarte", "GPU-Leistungslimit"} else 0
            self.add(lvl, "Konfiguration", f"Ge\u00e4ndert gegen\u00fcber Log vom {p.get('start', '')[:16].replace('T', ' ')}",
                     "; ".join(f"{k}: {a} \u2192 {b}" for k, a, b in ch))

    # -- Windows-Ereignisprotokoll -------------------------------------------
    def check_events(self):
        L, ec = self.log, self.cfg["ereignisse"]
        start = L.t0 - dt.timedelta(minutes=ec.get("minuten_vor_start", 2))
        end_log = L.at(L.duration)
        end = end_log + dt.timedelta(minutes=ec.get("minuten_nach_ende", 60))
        evs, err = fetch_events(start, min(end, dt.datetime.now() + dt.timedelta(minutes=1)))
        if evs is None:
            self.events_note = f"Ereignisprotokoll nicht ausgewertet: {err}"
            return
        self.events_ok = True
        self.events_note = (f"Ereignisse von {socket.gethostname()} zwischen {start:%d.%m. %H:%M} und {end:%d.%m. %H:%M}. "
                            "Nur aussagekr\u00e4ftig, wenn das Skript auf dem PC l\u00e4uft, der das Log geschrieben hat.")
        kp41_after = []
        groups = {}
        lke = []
        for ev in evs:
            lvl, label = classify_event(ev)
            try:
                ts = dt.datetime.strptime(ev["t"][:19], "%Y-%m-%dT%H:%M:%S")
            except (ValueError, KeyError, TypeError):
                continue
            tsu = None
            if ev.get("u"):
                try:
                    tsu = dt.datetime.strptime(str(ev["u"])[:19], "%Y-%m-%dT%H:%M:%S")
                except ValueError:
                    tsu = None
            rel = L.rel_of(ts, tsu)
            ev["_rel"] = rel
            ev["_lvl"] = lvl
            ev["_label"] = label
            ev["_after"] = ts > end_log
            self.events.append(ev)
            if lvl is None or lvl < 1:
                continue
            if ev.get("_lke"):
                lke.append(ev)
                continue
            if L.source == "LHM" and ((ev.get("p"), ev.get("id")) in self.CRASH_EV
                                      or "librehardwaremonitor" in (ev.get("msg") or "").lower()):
                continue  # wird bei den Unterbrechungen der Aufzeichnung eingeordnet
            groups.setdefault(label, []).append(ev)
            if ev.get("p") == "Microsoft-Windows-Kernel-Power" and ev.get("id") == 41 and ev["_after"]:
                kp41_after.append(ev)
        self.events.sort(key=lambda x: x["_rel"])
        self._check_livekernel(lke, fetch_events.dumps, end_log)
        for label, lst in groups.items():
            lvl = max(x["_lvl"] for x in lst)
            inside = [x for x in lst if not x["_after"]]
            after = [x for x in lst if x["_after"]]
            parts = []
            if inside:
                parts.append(f"{len(inside)}\u00d7 w\u00e4hrend des Logs (erstes {self.clk(inside[0]['_rel'])})")
            if after:
                parts.append(f"{len(after)}\u00d7 nach Logende (erstes {after[0]['t'][11:19]})")
            msg = lst[0].get("_extra") or lst[0].get("msg") or ""
            self.add(lvl, "Ereignis", f"{label}", "; ".join(parts) + (f". {msg[:300]}" if msg else ""),
                     t=inside[0]["_rel"] if inside else None, marks=[x["_rel"] for x in inside[:30]])
        if kp41_after and not L.footer:
            for f in self.F:
                if f.cat == "Log" and f.title.startswith("Log endet ohne"):
                    f.level = 3
                    f.title = "Absturz: Log endet ohne Abschluss, danach unerwarteter Neustart"
                    f.detail += f" Kernel-Power 41 beim n\u00e4chsten Start um {kp41_after[0]['t'][11:19]}."
                    if L.duration <= 120:
                        f.detail += self.early_crash_text(L.duration)
                        self.early_crashes = getattr(self, "early_crashes", 0) + 1
        if not groups and not lke:
            self.add(0, "Ereignis", "Keine relevanten Eintr\u00e4ge im Ereignisprotokoll",
                     "Weder Kernel-Power 41, Bluescreen, WHEA-Logger, Grafiktreiber-Timeout noch Datentr\u00e4gerfehler im Zeitraum.")

    def _check_livekernel(self, lke, dumps, end_log):
        """LiveKernelEvents: echte Ereignisse (passende Dump-Datei) von erneut gemeldeten alten Berichten trennen."""
        L = self.log
        dump_times = []
        for d in dumps or []:
            try:
                dump_times.append((dt.datetime.strptime(str(d.get("t", ""))[:19], "%Y-%m-%dT%H:%M:%S"), d))
            except ValueError:
                pass
        if dumps is not None:
            self.dumps = sorted(dump_times, key=lambda x: x[0])
        log_start = L.t0
        # neue Dump-Dateien im Zeitraum des Logs sind immer echte Ereignisse
        new_dumps = [(t, d) for t, d in self.dumps if log_start <= t <= end_log]
        by_code = {}
        for ev in lke:
            by_code.setdefault(ev["_lke"], []).append(ev)
        for code, lst in by_code.items():
            name = LIVEKERNEL.get(code, "")
            stamps = sorted({x["t"][:19] for x in lst})
            real, repeat = [], []
            for x in lst:
                ts = dt.datetime.strptime(x["t"][:19], "%Y-%m-%dT%H:%M:%S")
                if dumps is None:
                    real.append(x)
                elif any(-60 <= (ts - t).total_seconds() <= 300 for t, _ in self.dumps):
                    real.append(x)
                else:
                    repeat.append(x)
                    x["_lvl"] = 0
                    x["_extra"] = (x.get("_extra") or "") + " \u00b7 erneute Meldung eines \u00e4lteren Berichts"
            short = name.split(" \u2013")[0] if name else ""
            label = f"LiveKernelEvent {code}" + (f" ({short})" if short else "")
            older = [t for t, _ in self.dumps if t < log_start]
            if dumps is None:
                self.add(1, "Ereignis", label,
                         f"{len(lst)} Meldung(en) zu {len(stamps)} Zeitpunkt(en). Ob es neue Ereignisse sind, lie\u00df sich nicht "
                         "pr\u00fcfen (kein Zugriff auf C:\\Windows\\LiveKernelReports \u2013 Skript als Administrator starten).",
                         t=(real[0]["_rel"] if real else None))
            elif real and any(not x["_after"] for x in real):
                inside = [x for x in real if not x["_after"]]
                self.add(2, "Ereignis", label + " w\u00e4hrend des Logs",
                         f"Zu {len(inside)} Meldung(en) gibt es eine passende neue Dump-Datei \u2013 das Ereignis ist "
                         f"tats\u00e4chlich in diesem Zeitraum passiert (erstes {self.clk(inside[0]['_rel'])}). {name}",
                         t=inside[0]["_rel"], marks=[x["_rel"] for x in inside[:30]])
            elif real:
                self.add(1, "Ereignis", label + " nach Logende",
                         f"Neue Dump-Datei nach Logende ({real[0]['t'][11:19]}), also au\u00dferhalb der Messung. {name}")
            else:
                last = f" Letzte Dump-Dateien: {older[-1]:%d.%m. %H:%M}." if older else ""
                self.add(0, "Ereignis", label + ": nur Wiederholung \u00e4lterer Berichte",
                         f"{len(lst)} Meldungen zu {len(stamps)} Zeitpunkten, aber keine neue Dump-Datei dazu. Windows "
                         f"versucht offenbar, bereits vorhandene Berichte erneut zu \u00fcbermitteln.{last}")
        if new_dumps and not lke:
            self.add(2, "Ereignis", f"{len(new_dumps)} neue Kernel-Dump-Datei(en) w\u00e4hrend des Logs",
                     "In C:\\Windows\\LiveKernelReports sind w\u00e4hrend der Messung Dateien entstanden: "
                     + ", ".join(f"{t:%H:%M:%S} {os.path.basename(str(d.get('f', '')))}" for t, d in new_dumps[:6]),
                     t=(new_dumps[0][0] - L.t0).total_seconds())

    # -- Kennzahlen ----------------------------------------------------------
    def stat_rows(self):
        s, L = self.s, self.log
        rows = []

        def add(label, c, unit=None, d=None):
            if not c or not c.n_valid:
                return
            u = unit or c.unit
            dd = digits_for(u) if d is None else d
            v = c.vals
            lv = [v[i] for i in range(L.n) if self.load[i] and v[i] == v[i]]
            imax = max((i for i in range(L.n) if v[i] == v[i]), key=lambda i: v[i])
            rows.append((label, u, dd, c.vmin, c.vavg, c.vmax, (sum(lv) / len(lv)) if lv else NAN, L.t[imax]))
        add("CPU Tctl/Tdie", s.tctl)
        for c in s.ccd:
            add("CPU " + c.short.replace("CPU ", ""), c)
        add("CPU IOD-Hotspot", s.iod)
        add("CPU PPT", s.ppt)
        add("CPU-Takt (\u00d8 Kerne)", s.clock)
        add("CPU-Auslastung", s.usage)
        add("Vcore (SVI3)" if s.vcore and "SVI3" in s.vcore.name else "Vcore", s.vcore or s.vcore_board)
        add("SoC-Spannung", s.vsoc or s.vsoc_board)
        add("VDDIO_MEM", s.vddio)
        for c, nom, lab in s.rails + s.gpu_rails:
            add(lab, c)
        add("CMOS-Batterie", s.vbat)
        for d in s.dimms:
            add(f"RAM {d['label']} \u00b0C", d["temp"])
            add(f"RAM {d['label']} VDD", d["vdd"])
            add(f"RAM {d['label']} VIN", d["vin"])
        add("GPU-Temperatur", s.gpu_temp)
        add("GPU-Hotspot", s.gpu_hot)
        add("GPU-Speicher (Junction)", s.gpu_mem)
        add("GPU-Leistung", s.gpu_power)
        add("GPU-Last", s.gpu_load)
        add("GPU-Takt", s.gpu_clock)
        add("GPU PCIe-Link", s.gpu_link)
        for c in s.vrm + s.chipset:
            add("Board " + c.short, c)
        for d in s.drives:
            add(f"SSD {d['label']}", d["temp"])
            for k, c in enumerate(Analysis.drive_sensors(d)[1:], start=2):
                add(f"SSD {d['label']} (Sensor {k})", c)
        add("Bildrate", s.fps)
        add("Bildrate 1 % low", s.fps_low)
        add("Frametime \u00d8", s.ft_avg)
        return rows

    def build_kpi(self):
        s, L = self.s, self.log
        k = {}

        def mx(c):
            return round(c.vmax, 3) if c and isnum(c.vmax) else None

        def mn(c):
            return round(c.vmin, 3) if c and isnum(c.vmin) else None
        k["cpu_max"] = mx(s.tctl)
        k["gpu_max"] = mx(s.gpu_temp)
        k["gpu_hot_max"] = mx(s.gpu_hot)
        k["gpu_mem_max"] = mx(s.gpu_mem)
        k["ram_max"] = max([d["temp"].vmax for d in s.dimms if d["temp"]], default=None)
        k["dimm"] = {(d["channel"] or d["label"]): {"vin_min": mn(d["vin"]), "vdd": round(d["vdd"].vavg, 3) if d["vdd"] and isnum(d["vdd"].vavg) else None,
                                                    "temp_max": mx(d["temp"])} for d in s.dimms}
        if s.mem_used and s.mem_avail:
            tot = [a + b for a, b in zip(s.mem_used.vals, s.mem_avail.vals) if a == a and b == b]
            k["ram_gb"] = round(max(tot) / 1024, 1) if tot else None
        k["ssd_max"] = max([c.vmax for d in s.drives for c in self.drive_sensors(d) if isnum(c.vmax)], default=None)
        k["v12_min"] = mn(s.rails[0][0]) if s.rails else None
        k["gpu12_min"] = mn(s.gpu_rails[0][0]) if s.gpu_rails else None
        vs = s.vsoc or s.vsoc_board
        k["vsoc"] = round(vs.vavg, 3) if vs else None
        k["whea"] = int(s.whea.vlast - s.whea.vfirst) if s.whea else None
        k["pcie_err"] = int(getattr(self, "pcie_err_load", 0)) if s.pcie else None
        k["pcie_err_trans"] = int(getattr(self, "pcie_err_trans", 0)) if s.pcie else None
        k["dimms"] = len(s.dimms)
        k["fps"] = round(s.fps.vavg, 1) if s.fps else None
        k["gpu_load_share"] = round(100 * sum(1 for i in range(L.n) if self.load[i]) / max(1, L.n))
        k["gaps"] = len(self.gaps)
        k["events_crit"] = sum(1 for f in self.F if f.cat == "Ereignis" and f.level >= 3)
        k["abstuerze"] = sum(1 for f in self.F if f.cat == "Log" and f.level >= 3 and f.title.startswith("Absturz"))
        k["absturz_nach_start"] = getattr(self, "early_crashes", 0)
        k["whea_ev"] = sum(1 for ev in self.events if ev.get("p") == "Microsoft-Windows-WHEA-Logger" and not ev.get("_after")) \
            if self.events_ok else None
        self.kpi = k
        share = k["gpu_load_share"]
        prof = []
        if s.pm_proc:
            prof.append(f"PresentMon: Desktop ({s.pm_proc})" if s.pm_desktop else f"Spiel: {s.pm_proc}")
        prof.append(f"Last-Anteil {share} %")
        self.profile = " \u00b7 ".join(prof)


# --------------------------------------------------------------------------
# Windows-Ereignisprotokoll
# --------------------------------------------------------------------------
EVENT_PROVIDERS = [
    "Microsoft-Windows-Kernel-Power", "EventLog", "Microsoft-Windows-WER-SystemErrorReporting",
    "Microsoft-Windows-WHEA-Logger", "Display", "nvlddmkm", "amdkmdag", "amdwddmg", "volmgr", "disk", "Ntfs",
    "stornvme", "storahci", "Application Error", "Application Hang", "Windows Error Reporting",
    "Microsoft-Windows-Kernel-General", "User32", "Microsoft-Windows-Power-Troubleshooter", "Microsoft-Windows-Winlogon",
    ".NET Runtime", "Microsoft-Windows-Kernel-PnP", "Microsoft-Windows-Kernel-Processor-Power",
    "Microsoft-Windows-StorPort", "Microsoft-Windows-MemoryDiagnostics-Results",
]

PS_SCRIPT = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ci = [Globalization.CultureInfo]::InvariantCulture
$s = [datetime]::ParseExact('__START__', 'yyyy-MM-dd HH:mm:ss', $ci)
$e = [datetime]::ParseExact('__END__', 'yyyy-MM-dd HH:mm:ss', $ci)
$prov = @(__PROV__)
$queries = @(
  @{ LogName = 'System'; Level = 1, 2, 3; StartTime = $s; EndTime = $e },
  @{ LogName = 'System'; Id = 1, 12, 13, 41, 42, 107, 109, 506, 507, 1074, 6005, 6006, 6008, 7001, 7002; StartTime = $s; EndTime = $e },
  @{ LogName = 'Application'; Level = 1, 2, 3; StartTime = $s; EndTime = $e },
  @{ LogName = 'Application'; ProviderName = 'Windows Error Reporting'; Id = 1001; StartTime = $s; EndTime = $e },
  @{ LogName = 'System'; ProviderName = 'Microsoft-Windows-MemoryDiagnostics-Results'; StartTime = $s; EndTime = $e }
)
$out = New-Object System.Collections.ArrayList
foreach ($q in $queries) {
  $ev = Get-WinEvent -FilterHashtable $q -ErrorAction SilentlyContinue
  foreach ($x in $ev) {
    if (($prov -contains $x.ProviderName) -or ($x.Level -ge 1 -and $x.Level -le 2)) {
      $msg = ''
      try { if ($x.Message) { $msg = (($x.Message -split "`r?`n") | Where-Object { $_.Trim() -ne '' } | Select-Object -First 4) -join ' | ' } } catch {}
      $pr = ''
      try { $pr = (($x.Properties | Select-Object -First 8 | ForEach-Object { [string]$_.Value }) -join ';') } catch {}
      [void]$out.Add([pscustomobject]@{ t = $x.TimeCreated.ToString('s', $ci); u = $x.TimeCreated.ToUniversalTime().ToString('s', $ci); id = [int]$x.Id; p = [string]$x.ProviderName; lvl = [int]$x.Level; log = [string]$x.LogName; msg = $msg; props = $pr })
    }
  }
}
$dumps = New-Object System.Collections.ArrayList
$dumpok = $true
try {
  if (Test-Path -LiteralPath '__WINDIR__\LiveKernelReports') {
  Get-ChildItem -LiteralPath '__WINDIR__\LiveKernelReports' -Recurse -File -ErrorAction Stop | ForEach-Object {
    [void]$dumps.Add([pscustomobject]@{ t = $_.LastWriteTime.ToString('s', $ci); f = $_.FullName; mb = [math]::Round($_.Length / 1MB, 1) })
  }
  }
} catch { $dumpok = $false }
ConvertTo-Json -InputObject @{ ev = @($out); dumps = @($dumps); dumpok = $dumpok } -Depth 4 -Compress
"""


def system_dir():
    """Windows-Systemordner direkt von der API holen (nicht aus Umgebungsvariablen, die der Benutzer setzen kann)."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetSystemDirectoryW(buf, 260):
            return buf.value
    except (AttributeError, OSError):
        pass
    return r"C:\Windows\System32"


def powershell_path():
    """Absoluter Pfad, damit keine gleichnamige powershell.exe aus dem Arbeitsordner gestartet wird."""
    return os.path.join(system_dir(), "WindowsPowerShell", "v1.0", "powershell.exe")


def fetch_events(start, end):
    if os.name != "nt":
        return None, "nur unter Windows m\u00f6glich"
    windir = os.path.dirname(system_dir()).replace("'", "''")
    script = (PS_SCRIPT.replace("__WINDIR__", windir).replace("__START__", start.strftime("%Y-%m-%d %H:%M:%S"))
              .replace("__END__", end.strftime("%Y-%m-%d %H:%M:%S"))
              .replace("__PROV__", ",".join(f"'{p}'" for p in EVENT_PROVIDERS)))
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run([powershell_path(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-EncodedCommand", enc], capture_output=True, timeout=180,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as ex:
        return None, f"PowerShell nicht ausf\u00fchrbar ({ex})"
    out = r.stdout.decode("utf-8", errors="replace").strip().lstrip("\ufeff")
    if not out:
        if r.returncode != 0:
            return None, (r.stderr.decode("utf-8", errors="replace").strip()[:200] or f"Exitcode {r.returncode}")
        return [], None
    try:
        data = json.loads(out)
    except ValueError:
        return None, "Ausgabe von PowerShell nicht lesbar"
    dumps = None
    if isinstance(data, dict) and "ev" in data:
        dumps = data.get("dumps") if data.get("dumpok") else None
        if isinstance(dumps, dict):
            dumps = [dumps]
        data = data.get("ev") or []
    if isinstance(data, dict):
        data = [data]
    evs, seen = [], set()
    for x in data:
        if not isinstance(x, dict):
            continue
        k = (x.get("t"), x.get("p"), x.get("id"), x.get("props"), (x.get("msg") or "")[:120])
        if k in seen:
            continue
        seen.add(k)
        evs.append(x)
    fetch_events.dumps = [d for d in (dumps or []) if isinstance(d, dict)] if dumps is not None else None
    return evs, None


fetch_events.dumps = None


BUGCHECKS = {
    0x0A: "IRQL_NOT_LESS_OR_EQUAL", 0x19: "BAD_POOL_HEADER", 0x1A: "MEMORY_MANAGEMENT",
    0x1E: "KMODE_EXCEPTION_NOT_HANDLED", 0x3B: "SYSTEM_SERVICE_EXCEPTION", 0x4E: "PFN_LIST_CORRUPT",
    0x50: "PAGE_FAULT_IN_NONPAGED_AREA", 0x7E: "SYSTEM_THREAD_EXCEPTION_NOT_HANDLED", 0x7F: "UNEXPECTED_KERNEL_MODE_TRAP",
    0x9F: "DRIVER_POWER_STATE_FAILURE", 0xA: "IRQL_NOT_LESS_OR_EQUAL", 0xC2: "BAD_POOL_CALLER",
    0xD1: "DRIVER_IRQL_NOT_LESS_OR_EQUAL", 0xEF: "CRITICAL_PROCESS_DIED", 0x101: "CLOCK_WATCHDOG_TIMEOUT",
    0x109: "CRITICAL_STRUCTURE_CORRUPTION", 0x116: "VIDEO_TDR_FAILURE", 0x117: "VIDEO_TDR_TIMEOUT_DETECTED",
    0x119: "VIDEO_SCHEDULER_INTERNAL_ERROR", 0x124: "WHEA_UNCORRECTABLE_ERROR", 0x133: "DPC_WATCHDOG_VIOLATION",
    0x139: "KERNEL_SECURITY_CHECK_FAILURE", 0x13A: "KERNEL_MODE_HEAP_CORRUPTION", 0x154: "UNEXPECTED_STORE_EXCEPTION",
    0x1000007E: "SYSTEM_THREAD_EXCEPTION_NOT_HANDLED_M", 0x1000008E: "KERNEL_MODE_EXCEPTION_NOT_HANDLED_M",
    0x20001: "HYPERVISOR_ERROR", 0x12B: "FAULTY_HARDWARE_CORRUPTED_PAGE",
}
# Codes, die typischerweise von kaputten oder instabilen Speicherinhalten kommen (RAM, Speichercontroller, Kanal)
MEM_BUGCHECKS = {0x1A, 0x50, 0x4E, 0x19, 0xC2, 0x139, 0x13A, 0x154, 0x109, 0x12B}


LIVEKERNEL = {
    "117": "VIDEO_TDR_TIMEOUT_DETECTED \u2013 Grafiktreiber-Timeout",
    "141": "VIDEO_ENGINE_TIMEOUT_DETECTED \u2013 GPU-Engine hing, Treiber zur\u00fcckgesetzt",
    "144": "BUGCODE_USB3_DRIVER \u2013 USB-3-Treiber",
    "15f": "CONNECTED_STANDBY_WATCHDOG_TIMEOUT_LIVEDUMP \u2013 Energiesparmodus",
    "193": "VIDEO_DXGKRNL_LIVEDUMP \u2013 DirectX-Kernel",
    "1a1": "WIN32K_CALLOUT_WATCHDOG_LIVEDUMP \u2013 Fenstersystem hing",
    "1b0": "VIDEO_MINIPORT_FAILED_LIVEDUMP \u2013 Grafiktreiber",
    "1b8": "VIDEO_MINIPORT_BLACK_SCREEN_LIVEDUMP \u2013 schwarzer Bildschirm",
    "1cc": "EFS_FATAL_ERROR",
    "ab": "SESSION_HAS_VALID_POOL_ON_EXIT",
}


def bugcheck_name(code):
    n = BUGCHECKS.get(code)
    return f"0x{code:X}" + (f" {n}" if n else "")


def classify_event(ev):
    p, i, lvl, msg = ev.get("p", ""), ev.get("id"), ev.get("lvl", 4), ev.get("msg") or ""
    props = (ev.get("props") or "").split(";")
    if p == "Microsoft-Windows-Kernel-Power" and i == 41:
        extra = []
        try:
            code = int(props[0])
            extra.append("BugcheckCode 0 (kein Bluescreen-Code \u00fcbergeben \u2013 typisch f\u00fcr Hardreset, Stromverlust "
                         "oder kompletten H\u00e4nger)" if code == 0 else f"Bugcheck {bugcheck_name(code)}")
            if len(props) > 6 and props[6] not in ("", "0"):
                extra.append("Einschaltknopf wurde gedr\u00fcckt")
        except (ValueError, IndexError):
            pass
        ev["_extra"] = ", ".join(extra)
        return 3, "Unerwarteter Neustart (Kernel-Power 41)"
    if p == "EventLog" and i == 6008:
        return 3, "Unerwartetes Herunterfahren (EventLog 6008)"
    if p == "Microsoft-Windows-WER-SystemErrorReporting" and i == 1001:
        m = re.search(r"0x([0-9a-fA-F]{8})", msg)
        if m:
            ev["_extra"] = f"Bugcheck {bugcheck_name(int(m.group(1), 16))}"
            return 3, f"Bluescreen {bugcheck_name(int(m.group(1), 16))}"
        return 3, "Bluescreen (BugCheck 1001)"
    if p == "Microsoft-Windows-WHEA-Logger":
        return (3 if lvl in (1, 2) else 2), f"WHEA-Logger {i}"
    if p == "Display" and i == 4101:
        return 2, "Grafiktreiber-Timeout (TDR, Display 4101)"
    if p == "nvlddmkm":
        return 2, f"NVIDIA-Treiber nvlddmkm {i}"
    if p in ("amdkmdag", "amdwddmg"):
        return 2, f"AMD-Grafiktreiber {i}"
    if p == "Microsoft-Windows-Winlogon" and i == 7002:
        return None, "Benutzer abgemeldet"
    if p == "Microsoft-Windows-Winlogon" and i == 7001:
        return None, "Benutzer angemeldet"
    if p == "Microsoft-Windows-Kernel-General" and i == 1:
        return None, "Systemzeit ge\u00e4ndert"
    if p == "Microsoft-Windows-Kernel-Power" and i == 109:
        return None, "Herunterfahren eingeleitet"
    if p == "Microsoft-Windows-Kernel-Power" and i == 506:
        return None, "Modern Standby (Bildschirm aus)"
    if p == "Microsoft-Windows-Kernel-Power" and i == 507:
        return None, "Modern Standby beendet"
    if p == "Microsoft-Windows-Kernel-General" and i == 12:
        return None, "Systemstart"
    if p == "Microsoft-Windows-Kernel-General" and i == 13:
        return None, "Herunterfahren"
    if p == "EventLog" and i == 6005:
        return None, "Systemstart (Ereignisprotokoll gestartet)"
    if p == "EventLog" and i == 6006:
        return None, "Sauberes Herunterfahren (Ereignisprotokoll beendet)"
    if p == "User32" and i == 1074:
        return None, "Herunterfahren/Neustart angefordert"
    if p == "Microsoft-Windows-Kernel-Power" and i == 42:
        return None, "Energiesparmodus"
    if p == "Microsoft-Windows-Kernel-Power" and i == 107:
        return None, "Fortsetzen aus Energiesparmodus"
    if p == "Microsoft-Windows-Power-Troubleshooter" and i == 1:
        return None, "Aufgewacht aus Energiesparmodus"
    if p == "Microsoft-Windows-MemoryDiagnostics-Results":
        if re.search(r"keine Fehler|no errors", msg, re.I):
            return 0, "Windows-Speicherdiagnose: keine Fehler"
        if i in (1102, 1202) or re.search(r"Hardwarefehler|hardware errors|Fehler (erkannt|festgestellt)", msg, re.I):
            return 3, "Windows-Speicherdiagnose: Fehler gefunden"
        return 0, f"Windows-Speicherdiagnose: Ergebnis {i}"
    if p == "disk" and i == 157:
        return 2, "Laufwerk unerwartet entfernt (disk 157)"
    if p == "Microsoft-Windows-Kernel-PnP" and i == 411:
        return 1, "Ger\u00e4t konnte nicht starten (Kernel-PnP 411)"
    if p == "Microsoft-Windows-Kernel-PnP" and i == 219:
        return 0, "Treiber nicht geladen (Kernel-PnP 219)"
    if p == "Microsoft-Windows-Kernel-PnP" and i == 225:
        return 0, "Ger\u00e4t blockiert Auswerfen (Kernel-PnP 225)"
    if p == "Microsoft-Windows-Kernel-Processor-Power" and i == 37:
        return 1, "CPU-Takt durch Firmware begrenzt (Kernel-Processor-Power 37)"
    if p == "volmgr" and i in (161, 162):
        return 1, "Speicherabbild nicht erstellt (volmgr)"
    if p in ("disk", "Ntfs", "stornvme", "storahci") and lvl in (1, 2, 3):
        return 2, f"Datentr\u00e4ger: {p} {i}"
    if p == "Application Error" and i == 1000:
        name = msg.split(":")[1].split(",")[0].strip() if ":" in msg else ""
        return 1, "Programmabsturz" + (f" ({name[:40]})" if name else "")
    if p == "Application Hang" and i == 1002:
        return 1, "Programm reagiert nicht"
    if p == "Windows Error Reporting" and i == 1001:
        if "BlueScreen" in msg:
            return 3, "Fehlerbericht: BlueScreen"
        if "LiveKernelEvent" in msg:
            props = (ev.get("props") or "").split(";")
            code = props[5].strip().lower() if len(props) > 5 else ""
            ev["_lke"] = code or "?"
            name = LIVEKERNEL.get(code, "")
            ev["_extra"] = f"LiveKernelEvent {code}" + (f" = {name}" if name else "")
            return 2, f"LiveKernelEvent {code}" + (f" ({name})" if name else "")
        return None, "Fehlerbericht"
    if lvl in (1, 2):
        return 0, f"{p} {i}"
    return None, f"{p} {i}"



# --------------------------------------------------------------------------
# Erklaerungen zum Lernen (aufklappbar im Bericht)
# --------------------------------------------------------------------------
# (Kategorie, Regex auf Titel, Erklaerung). Der erste Treffer gewinnt.
EXPLAIN = [
    ("Log", r"^Absturz: Aufzeichnung", "LibreHardwareMonitor schreibt alle paar Sekunden eine Zeile. Bricht die Aufzeichnung "
     "ab und Windows schreibt beim n\u00e4chsten Start \u201eKernel-Power 41\u201c (unerwarteter Neustart), dann ist der PC an "
     "genau dieser Stelle ausgegangen. Die Tabelle \u201eLetzte Minute vor dem Absturz\u201c zeigt, was davor gemessen wurde. "
     "Steht dort \u201eBugcheckCode 0\u201c, gab es keinen Bluescreen: Hardreset, Stromverlust oder ein totaler H\u00e4nger."),
    ("Log", r"Dauerlog", "LHM l\u00e4uft dauerhaft im Hintergrund und schreibt eine Datei pro Tag. Weil es keinen Logabschluss gibt, "
     "erkennt das Tool Abst\u00fcrze anders als bei HWiNFO: \u00fcber Unterbrechungen der Aufzeichnung plus die Eintr\u00e4ge, die "
     "Windows beim Herunterfahren, Einschlafen oder nach einem Absturz schreibt."),
    ("Log", r"Energiesparmodus|Neustart|Herunterfahren|angemeldet|Zeitumstellung|Uhrzeit|LHM beendet|Aussetzer|unterbrochen|"
            r"Aufzeichnung endet|Unterbrechung|l\u00e4uft noch|H\u00e4nger beim|selbst ist abgest",
     "Jede L\u00fccke in einem Dauerlog hat einen Grund: Der PC war aus, im Energiesparmodus, wurde neu gestartet \u2013 oder ist "
     "abgest\u00fcrzt. Windows protokolliert ordentliches Herunterfahren (Kernel-General 13, EventLog 6006), Einschlafen "
     "(Kernel-Power 42), Abmelden (Winlogon 7002) und Systemstarts (Kernel-General 12). Geplante Vorg\u00e4nge landen deshalb "
     "nur als Info im Bericht. Fehlt bei einer L\u00fccke von mehr als ein paar Sekunden jeder Eintrag, hing vermutlich das "
     "System \u2013 oder LHM wurde von Hand beendet."),
    ("Board", r"Board-Profil|Rohwerte", "Der Sensorchip auf dem Mainboard misst Spannungen \u00fcber Spannungsteiler: 12 V w\u00e4ren "
     "f\u00fcr den Chip zu viel, also misst er nur ein Sechstel davon. Welcher Eingang welche Spannung mit welchem Teiler misst, "
     "steht nirgends im Chip \u2013 das muss das Programm pro Board wissen. Kennt LHM das Board nicht, kommen Rohwerte; das Tool "
     "rechnet sie mit den Teilern um, die zum Abgleich mit HWiNFO passten."),
    ("Log", r"^Log regul", "HWiNFO schreibt beim Stoppen des Loggings eine zweite Kopfzeile und darunter eine Zeile mit den "
     "Ger\u00e4tenamen (z. B. \u201eDDR5 DIMM [#3] \u2026 CHANNEL B\u201c). Ist dieser Abschluss da, wurde das Logging normal beendet "
     "\u2013 der PC lief also bis zum Schluss."),
    ("Log", r"^(Log endet ohne|Absturz)", "Fehlt der Abschluss, wurde HWiNFO mitten im Schreiben unterbrochen. Das passiert bei "
     "Absturz, Hardreset, Stromausfall oder wenn HWiNFO per Taskmanager beendet wurde. Ein typisches Zeichen ist eine halbe "
     "letzte Zeile voller NUL-Bytes: Windows hatte den Platz in der Datei schon reserviert, aber nicht mehr beschrieben. "
     "Findet sich danach im Ereignisprotokoll ein Kernel-Power 41, ist der Absturz praktisch belegt. Schau dann in "
     "\u201eLetzte Minute vor Logende\u201c: Was lief zuletzt, war Last drauf, ist eine Spannung eingebrochen?"),
    ("Log", r"^Zustand beim letzten", "Ob ein Absturz unter Last oder im Leerlauf passiert, grenzt die Ursache ein. Unter Last: "
     "eher Netzteil, Temperatur, zu knappe Spannung bei hohem Takt. Im Leerlauf oder bei wenig Last: eher Energiesparzust\u00e4nde "
     "(C-States), zu niedrige Spannung beim Herunterregeln, Speichercontroller/RAM oder Treiber."),
    ("Log", r"L\u00fccke", "Das Messprogramm schreibt im festen Takt{intervall}. Fehlt pl\u00f6tzlich ein St\u00fcck Zeit, "
     "konnte es in dieser Zeit nicht arbeiten \u2013 meist weil das ganze System kurz eingefroren war (Freeze, Treiber-Reset) "
     "oder im Standby lag. Ein paar Sekunden L\u00fccke ohne Standby sind ein ernstzunehmendes Freeze-Signal."),
    ("Log", r"Sensorgruppen", "Die Ger\u00e4tezuordnung jeder Spalte steht nur im Logabschluss. Fehlt er, leiht sich das Tool "
     "die Zuordnung aus einem fr\u00fcheren sauberen Log mit identischen Sensoren (darum lohnt es sich, Logs normal zu stoppen)."),
    ("Log", r"unvollst\u00e4ndige|unlesbare", "Eine abgeschnittene Zeile entsteht, wenn der PC beim Schreiben ausging. Sie wird "
     "verworfen, damit halbe Werte nicht als Messung z\u00e4hlen."),
    ("WHEA", r"", "WHEA (Windows Hardware Error Architecture) ist die Schnittstelle, \u00fcber die CPU, Speichercontroller und PCIe "
     "Hardwarefehler an Windows melden. \u201eKorrigiert\u201c hei\u00dft: Die Hardware hat den Fehler noch selbst abgefangen \u2013 trotzdem "
     "ist das ein Fr\u00fchwarnzeichen. Ein stabiles System hat hier 0. Schon ein einziger WHEA-Eintrag in einem Stabilit\u00e4tstest "
     "bedeutet: nicht stabil. Details (welche Komponente) stehen im Ereignisprotokoll unter WHEA-Logger."),
    ("PCIe", r"herabgestuft", "Die Grafikkarte handelt mit dem Board die Geschwindigkeit aus (Gen4 = 16 GT/s). Im Leerlauf "
     "schaltet sie zum Stromsparen auf 2,5 GT/s herunter \u2013 normal. F\u00e4llt der Link aber unter Volllast zur\u00fcck, war die "
     "Verbindung nicht sauber (Sitz der Karte, Slot, Riser-Kabel, Staub, BIOS-Einstellung)."),
    ("PCIe", r"", "PCIe ist die Datenleitung zwischen CPU und Grafikkarte. Jedes Datenpaket hat eine Pr\u00fcfsumme; ist sie falsch, "
     "wird es wiederholt (Replay/NAK) und als \u201eCorrectable Error\u201c gez\u00e4hlt. \u201eRecovery\u201c bedeutet, dass die Verbindung neu "
     "eingemessen wurde. Beim Wechsel der Link-Geschwindigkeit (Energiesparen, Ladebildschirme) z\u00e4hlen Recovery, NAK und "
     "Bad TLP fast immer etwas hoch \u2013 das ist normal. Auff\u00e4llig wird es nur, wenn die Z\u00e4hler bei durchgehender Volllast "
     "steigen. Fatal/Non-Fatal Errors sind dagegen immer ernst."),
    ("CPU", r"Drosselung", "Wird die CPU zu hei\u00df oder meldet das Board ein Problem, bremst sie sich selbst (HTC = Temperaturgrenze "
     "der CPU, PROCHOT = Signal \u201ezu hei\u00df\u201c, EXT = vom Mainboard, meist von den Spannungswandlern). Kurz beim Start ist "
     "selten, im Normalbetrieb sollte es nie passieren."),
    ("CPU", r"Temperatur", "Tctl/Tdie ist die Steuertemperatur der CPU. Jede CPU hat ein Temperaturlimit (AMD Ryzen je nach Modell "
     "89\u201395 \u00b0C, Intel meist 100 \u00b0C) und regelt ihren Takt vorher selbst zur\u00fcck. Neuere CPUs nutzen "
     "den Spielraum bis dahin teils bewusst aus; dauerhaft am Limit deutet aber auf K\u00fchler, W\u00e4rmeleitpaste "
     "oder Anpressdruck. Grenzwerte in diesem Bericht: {cpu_grenzen} (anpassbar per Konfiguration)."),
    ("CPU", r"SoC", "VDDCR_SOC versorgt bei AMD den Speichercontroller und den Infinity Fabric. Mit JEDEC-Speicher reichen rund "
     "1,0\u20131,1 V, EXPO hebt sie oft auf 1,2\u20131,3 V. \u00dcber 1,30 V drohen auf AM5 Sch\u00e4den (das f\u00fchrte 2023 zu "
     "durchgebrannten CPUs); zu niedrig kann den Speichercontroller instabil machen."),
    ("CPU", r"Kernspannung", "Vcore ist die Spannung der Rechenkerne. Die CPU fordert sie je nach Takt selbst an (VID). Beim X3D "
     "begrenzt AMD sie deutlich, Spitzen \u00fcber 1,3 V sind dort ungew\u00f6hnlich."),
    ("Netzteil", r"", "Das Netzteil soll 12 V, 5 V und 3,3 V liefern; die ATX-Norm erlaubt \u00b15 %. Die Board-Sensoren sind "
     "einfache Messchips und liegen oft 1\u20132 % daneben, deshalb vergleicht man mit einem zweiten Messpunkt: Die Grafikkarte "
     "misst ihre 12 V am Stecker selbst und ist genauer. Wichtig sind Einbr\u00fcche unter Last \u2013 ein Netzteil, das bei "
     "Lastspitzen einbricht, verursacht spontane Neustarts ohne Bluescreen."),
    ("RAM", r"Module erkannt|von .* RAM-Modulen", "HWiNFO liest jedes DDR5-Modul \u00fcber seinen eigenen Sensorchip (SPD-Hub) und "
     "nennt dazu den Speicherkanal. F\u00e4llt ein Speicherkanal aus oder wird beim Start nicht trainiert, tauchen danach "
     "nur noch die Module der \u00fcbrigen Kan\u00e4le auf. Diese Pr\u00fcfung vergleicht deshalb mit der erwarteten "
     "Anzahl ({riegel}, einstellbar per --riegel oder Konfiguration)."),
    ("RAM", r"weicht um", "Zwei gleiche Module am selben Board sollten fast gleiche Spannungen und Temperaturen haben. Driftet eines "
     "weg, liegt die Ursache bei diesem Modul, seinem Steckplatz oder dessen Zuleitung \u2013 oft lange bevor feste Grenzwerte "
     "anschlagen. Verglichen werden Durchschnitt und die tiefsten 1 % der Messwerte, einzelne Fehlmessungen z\u00e4hlen nicht."),
    ("RAM", r"Arbeitsspeicher nutzbar|Speicherkanal .* fehlt|RAM-Modul fehlt", "Beim Start misst das BIOS jeden Speicherkanal ein "
     "(Memory Training). Scheitert das f\u00fcr einen Kanal, startet der PC je nach Board trotzdem \u2013 nur mit dem halben Speicher. "
     "Die Sensorchips der Module h\u00e4ngen an einem eigenen Bus und k\u00f6nnen weiter antworten, deshalb pr\u00fcft das Tool "
     "zus\u00e4tzlich die nutzbare Speichermenge."),
    ("RAM", r"Sensoren liefern", "Verstummen alle Sensoren eines Moduls mitten im Betrieb, hat der PC die Verbindung zum Sensorchip "
     "des Moduls verloren. Das kann ein Vorbote eines Kanalausfalls sein. Gegenprobe: Lief zur selben Zeit ein Programm, das "
     "ebenfalls auf die Module zugreift (RGB-Software, ein zweites Monitoring-Tool)?"),
    ("RAM", r"PMIC", "Jedes DDR5-Modul hat einen eigenen Spannungsregler (PMIC). Meldet er \u00dcber-/Unterspannung oder \u00dcbertemperatur, "
     "ist das ein Hardwarealarm des Moduls selbst."),
    ("RAM", r"VDD|VDDIO", "VDD/VDDQ ist die Arbeitsspannung des Speichers: JEDEC 1,1 V, EXPO meist 1,35\u20131,40 V. VDDIO_MEM ist "
     "die Gegenseite im Speichercontroller der CPU."),
    ("RAM", r"VIN", "VIN ist die 5-V-Zuleitung vom Mainboard zum Spannungsregler des Moduls. Bricht sie ein, schwankt die "
     "Versorgung des ganzen Moduls."),
    ("RAM", r"\u00b0C|Temperat", "DDR5 arbeitet bis 85 \u00b0C normal, dar\u00fcber verdoppelt sich die Auffrischrate und Fehler "
     "werden wahrscheinlicher. Unter 50 \u00b0C ist entspannt."),
    ("Board", r"CMOS", "Die Knopfzelle h\u00e4lt BIOS-Einstellungen und Uhr. Neu hat sie etwa 3,0\u20133,2 V; unter 2,7 V kann das "
     "BIOS Einstellungen verlieren."),
    ("Board", r"", "VRM sind die Spannungswandler rund um den CPU-Sockel, der Chipsatz ist der Verteiler f\u00fcr USB, SATA und "
     "zus\u00e4tzliche PCIe-Lanes. Beide vertragen deutlich mehr als 80 \u00b0C."),
    ("GPU", r"Leistungslimit", "Die Grafikkarte regelt ihren Takt so, dass sie ihr Leistungsbudget{gpu_limit} nicht "
     "\u00fcberschreitet. Am Limit zu laufen ist Absicht, kein Fehler."),
    ("GPU", r"", "Die GPU hat drei wichtige Temperaturen: Kern (Durchschnitt), Hotspot (hei\u00dfeste Stelle, liegt normal "
     "10\u201320 \u00b0C dar\u00fcber) und Speicher (Junction-Temperatur des Grafikspeichers). Wird der Abstand Kern\u2013Hotspot gro\u00df (> 25 \u00b0C), sitzt der K\u00fchler "
     "oft schlecht oder die W\u00e4rmeleitpaste ist ausgetrocknet."),
    ("Laufwerk", r"", "SSDs melden ihren Zustand per SMART: Temperatur, verbleibende Lebensdauer (abh\u00e4ngig von geschriebenen "
     "Daten) und Warnflags. NVMe-SSDs drosseln ab etwa 70\u201380 \u00b0C."),
    ("L\u00fcfter", r"", "Viele L\u00fcfter haben einen Stopp-Modus bei niedriger Temperatur \u2013 dann ist 0 U/min normal. Beim "
     "CPU-L\u00fcfter oder der Pumpe einer Wasserk\u00fchlung darf das nie passieren."),
    ("Spiel", r"", "Die Frametime ist die Zeit zwischen zwei Bildern (8,3 ms = 120 FPS). HWiNFO liefert pro Messpunkt den "
     "Durchschnitt und die langsamsten Bilder (\u201e1 %/0,1 % high\u201c). Eine einzelne Spitze \u00fcber {ft_grenze} ms sp\u00fcrt man als kurzen "
     "Ruckler. In Ladebildschirmen und Men\u00fcs sind lange Frametimes normal, mitten im Spiel nicht. Achtung: Steht im Profil "
     "\u201edwm.exe\u201c, hat PresentMon den Windows-Desktop gemessen \u2013 der zeichnet nur bei \u00c4nderungen neu, lange Zeiten "
     "sind dort bedeutungslos."),
    ("Sensor", r"", "Ein Sensor, der mitten im Log keine Werte mehr liefert, hat entweder die Verbindung verloren oder HWiNFO "
     "konnte ihn nicht mehr abfragen. Einzelne Aussetzer passieren; dauerhafte deuten auf ein Ger\u00e4teproblem."),
    ("Konfiguration", r"EXPO", "EXPO (AMD) bzw. XMP (Intel) ist ein im RAM-Modul gespeichertes Profil mit h\u00f6herem Takt, "
     "sch\u00e4rferen Timings und h\u00f6herer Spannung. Ohne Profil l\u00e4uft DDR5 mit dem JEDEC-Standard (meist 4800 MT/s, "
     "1,1 V). Das Tool erkennt das an den Spannungen, damit es auch mit LHM funktioniert, das den RAM-Takt nicht "
     "liefert. HWiNFO-Logs enthalten den Takt zus\u00e4tzlich (Abschnitt Konfiguration), sonst zeigt ihn das BIOS."),
    ("Konfiguration", r"", "Das Tool leitet aus den Sensorwerten ab, wie das System eingestellt ist (RAM-Takt, Timings, SoC-Spannung, "
     "PCIe-Generation \u2026) und vergleicht mit dem vorigen Log. So f\u00e4llt auf, wenn ein BIOS-Update oder CMOS-Reset "
     "Einstellungen still zur\u00fcckgesetzt hat, oder wenn du EXPO aktiviert hast."),
    ("Ereignis", r"Kernel-Power 41", "Kernel-Power 41 schreibt Windows beim n\u00e4chsten Start, wenn es vorher nicht ordentlich "
     "heruntergefahren wurde. Der BugcheckCode verr\u00e4t den Grund: 0 hei\u00dft, es gab keinen Bluescreen \u2013 der PC war einfach aus "
     "(Hardreset, Stromverlust, totaler Hardware-H\u00e4nger). Steht dort ein Code, gab es vorher einen Bluescreen."),
    ("Ereignis", r"6008", "EventLog 6008 nennt den Zeitpunkt, zu dem Windows zuletzt noch lief, bevor es unerwartet ausging. "
     "Geh\u00f6rt fast immer zu einem Kernel-Power 41."),
    ("Ereignis", r"Bluescreen", "Ein Bluescreen ist ein kontrollierter Notstopp: Der Windows-Kern hat einen Zustand erkannt, in "
     "dem Weiterlaufen gef\u00e4hrlich w\u00e4re. Der Code (z. B. 0x124 = WHEA_UNCORRECTABLE_ERROR) zeigt die Art des Fehlers. "
     "Wechselnde Codes bei Absturz zu Absturz deuten eher auf Hardware, immer gleiche eher auf einen Treiber."),
    ("Ereignis", r"WHEA-Logger", "Der WHEA-Logger beschreibt genau, welche Komponente einen Hardwarefehler gemeldet hat "
     "(Prozessorkern, Cache, Speicher, PCIe-Ger\u00e4t). ID 19/47 = korrigiert, ID 18/1 = schwerwiegend."),
    ("Ereignis", r"LiveKernelEvent", "Ein LiveKernelEvent ist kein Absturz: Windows hat einen Schnappschuss des Kerns gespeichert, "
     "w\u00e4hrend das System weiterlief \u2013 meist, weil sich ein Treiber neu starten musste. Code 141/117 bedeutet: Die "
     "Grafikkarte hat zu lange nicht reagiert und der Treiber wurde zur\u00fcckgesetzt (TDR). Beim Treiberwechsel oder bei der "
     "Installation ist das normal, im laufenden Spiel ein Warnsignal. Achtung: Windows meldet alte Berichte wiederholt, bis "
     "sie hochgeladen sind. Ma\u00dfgeblich ist, wann die Dump-Datei in C:\\Windows\\LiveKernelReports entstanden ist."),
    ("Ereignis", r"Dump-Datei", "In C:\\Windows\\LiveKernelReports legt Windows bei Treiber-Resets Speicherabbilder ab. Eine neue "
     "Datei hei\u00dft: Genau zu diesem Zeitpunkt ist etwas passiert."),
    ("Ereignis", r"TDR|Grafiktreiber|nvlddmkm", "TDR (Timeout Detection and Recovery): Reagiert die Grafikkarte l\u00e4nger als "
     "2 Sekunden nicht, setzt Windows den Treiber zur\u00fcck \u2013 der Bildschirm wird kurz schwarz. Einzelf\u00e4lle kommen vor, "
     "h\u00e4ufig deutet es auf instabile GPU (Takt, Netzteil, PCIe) oder Treiberprobleme."),
    ("Markierung", r"", "Diese Markierung hast du selbst mit markieren.bat gesetzt (z. B. bei Bildaussetzern, Fehlermeldungen "
     "oder einem Absturz). In den Diagrammen steht sie als graue Linie \u2013 so siehst du, was die Sensoren genau in diesem "
     "Moment gemessen haben."),
    ("Ereignis", r"Programmabsturz|reagiert nicht", "Ein einzelnes abgest\u00fcrztes Programm sagt wenig \u00fcber die Hardware. "
     "Interessant wird es, wenn verschiedene Programme kurz vor einem Systemabsturz ausfallen."),
    ("Ereignis", r"unerwartet entfernt", "Windows hat ein Laufwerk pl\u00f6tzlich verloren, ohne dass es ausgeworfen wurde. Bei "
     "USB-Platten deutet das auf Wackelkontakt, Kabel oder eine einbrechende 5-V-Versorgung am USB-Port, bei internen "
     "Laufwerken auf Kabel, Controller oder Stromversorgung."),
    ("Ereignis", r"Kernel-PnP", "Kernel-PnP meldet Probleme beim Starten von Ger\u00e4ten und Treibern. 411 = Ger\u00e4t konnte nicht "
     "starten (z. B. Grafikkarte mit Ausrufezeichen im Ger\u00e4temanager), 219 = Treiber nicht geladen (oft harmlos), 225 = "
     "Programm blockiert das Auswerfen. H\u00e4ufen sie sich vor einem Absturz, lohnt der Blick auf das genannte Ger\u00e4t."),
    ("Ereignis", r"Firmware begrenzt", "Windows meldet, dass BIOS/Firmware den CPU-Takt begrenzt \u2013 wegen Temperatur, "
     "Strom oder Energiesparvorgaben. Einzelne Meldungen beim Start sind \u00fcblich, h\u00e4ufige im Betrieb nicht."),
    ("Ereignis", r"Datentr\u00e4ger", "Fehler von disk/Ntfs/stornvme bedeuten Probleme beim Lesen oder Schreiben \u2013 Kabel, "
     "Laufwerk oder Controller. Immer ernst nehmen und Backups pr\u00fcfen."),
]

GUIDE = (
    "<p><b>Status oben rechts</b> ist die schwerste Stufe aller Befunde: <i>Info</i> = zur Kenntnis, alles in Ordnung; "
    "<i>Hinweis</i> = beobachten; <i>Warnung</i> = verdient eine Erkl\u00e4rung; <i>Kritisch</i> = deutet auf Fehler oder Absturz.</p>"
    "<p><b>Befunde</b> zuerst lesen. Jeder hat \u201eWas bedeutet das?\u201c zum Aufklappen. Ein Klick auf die Uhrzeit springt in den "
    "Diagrammen genau an diese Stelle.</p>"
    "<p><b>Konfiguration</b> zeigt, wie dein System gerade l\u00e4uft (RAM-Takt, SoC-Spannung \u2026). Ein Punkt \u25CF markiert "
    "\u00c4nderungen gegen\u00fcber dem letzten Log.</p>"
    "<p><b>Diagramme</b>: Zeiger dr\u00fcberfahren zeigt alle Werte zu diesem Zeitpunkt, Ziehen zoomt, Doppelklick zeigt alles. "
    "Gestrichelte senkrechte Linien = Befunde, graue Fl\u00e4chen = L\u00fccken im Log. So liest du sie: Suche nach Stellen, an "
     "denen <i>mehrere</i> Kurven gleichzeitig etwas Ungew\u00f6hnliches tun \u2013 ein einzelner Ausrei\u00dfer ist oft ein Messfehler, "
    "zeitgleiche Ausrei\u00dfer sind ein Ereignis.</p>"
    "<p><b>Kennzahlen</b>: \u201e\u00d8 unter Last\u201c ist aussagekr\u00e4ftiger als der Gesamtdurchschnitt, weil Leerlauf ihn nicht verw\u00e4ssert.</p>"
    "<p><b>LibreHardwareMonitor-Logs</b> laufen dauerhaft mit. Neustarts und Standby erscheinen als L\u00fccken, die das Tool mit dem "
    "Ereignisprotokoll einordnet. Gibt es einen Absturz, steht oben ein kritischer Befund und weiter unten die letzte Minute davor. "
    "F\u00fcr gezielte Tests mit allen Details (PCIe-Fehlerz\u00e4hler, RAM-Spannungen, Frametimes) bleibt HWiNFO das Werkzeug.</p>"
    "<p><b>Nach einem Absturz</b>: (1) Befund \u201eLog endet ohne Abschluss\u201c? (2) Abschnitt \u201eLetzte Minute\u201c \u2013 Last, Temperaturen, "
    "Spannungen kurz vorher. (3) Ereignisse nach Logende \u2013 Kernel-Power 41 und Bluescreen-Code. Mehrere Abst\u00fcrze mit "
    "<i>unterschiedlichen</i> Bluescreen-Codes und ohne Muster in der Last sprechen eher f\u00fcr Hardware.</p>"
)

EVENT_GUIDE = [
    ("Kernel-Power 41", "Unerwarteter Neustart, beim n\u00e4chsten Start geschrieben. BugcheckCode 0 = ohne Bluescreen."),
    ("EventLog 6008", "Wann Windows zuletzt lief, bevor es unerwartet ausging."),
    ("BugCheck 1001", "Bluescreen mit Fehlercode \u2013 der Code zeigt die Fehlerart."),
    ("WHEA-Logger 17/19/47", "Korrigierter Hardwarefehler (PCIe, CPU, Speicher) \u2013 Fr\u00fchwarnzeichen."),
    ("WHEA-Logger 18/1", "Schwerwiegender Hardwarefehler, meist mit Bluescreen 0x124."),
    ("Display 4101", "Grafiktreiber hat nicht reagiert und wurde neu gestartet (TDR)."),
    ("WER 1001 LiveKernelEvent", "Schnappschuss bei Treiber-Reset ohne Absturz; 141/117 = Grafik. Wiederholte Meldungen sind oft alte Berichte."),
    ("volmgr 161/162", "Absturzabbild konnte nicht gespeichert werden \u2013 erkl\u00e4rt fehlende Dumps."),
    ("Service Control Manager 7000/7023", "Ein Dienst startete nicht oder brach ab \u2013 meist harmlos."),
    ("MemoryDiagnostics-Results", "Ergebnis der Windows-Speicherdiagnose (mdsched.exe) \u2013 \u201eHardwarefehler\u201c hei\u00dft RAM defekt oder instabil."),
    ("disk 157", "Laufwerk unerwartet entfernt \u2013 Kabel, USB-Port oder Stromversorgung."),
    ("Kernel-PnP 411 / 219 / 225", "Ger\u00e4t startet nicht / Treiber nicht geladen / Auswerfen blockiert."),
    ("Kernel-General 12 / EventLog 6005", "Systemstart."),
    ("Kernel-General 13 / EventLog 6006 / User32 1074", "Ordentliches Herunterfahren bzw. Neustart \u2013 kein Absturz."),
    ("Kernel-Power 42 / 107", "Energiesparmodus und Aufwachen."),
    ("Winlogon 7002 / 7001", "Abmelden und Anmelden \u2013 LHM l\u00e4uft in deiner Sitzung und pausiert dazwischen."),
    ("Security-SPP 8198", "Windows-Aktivierung fehlgeschlagen \u2013 nach Mainboard-Tausch typisch, nichts mit Stabilit\u00e4t."),
    ("TPM-WMI 1040", "Measured-Boot-Pr\u00fcfung fehlgeschlagen \u2013 nach BIOS-/Boardwechsel oder ohne Secure Boot \u00fcblich."),
    ("DistributedCOM 10016", "Berechtigungswarnung, die Windows st\u00e4ndig schreibt \u2013 ignorieren."),
]


def explain_context(an):
    """Werte aus Log und Konfiguration fuer die Platzhalter in EXPLAIN."""
    L, s, cfg = an.log, an.s, an.cfg
    lim = s.gpu_limit_nom.vavg if s.gpu_limit_nom else NAN
    return {
        "intervall": f" (in diesem Log alle {fnum(L.interval, 1)} s)" if L.interval else "",
        "cpu_grenzen": " / ".join(f"{LEVELS[k + 1]} {v} \u00b0C" for k, v in enumerate(cfg["temperaturen"][cpu_temp_key(cfg, s.is_x3d)])),
        "riegel": int(cfg.get("erwartete_riegel") or 0) or "keine Vorgabe",
        "gpu_limit": f" (in diesem Log {int(round(lim))} W)" if isnum(lim) else "",
        "ft_grenze": cfg["frametime_spitze_ms"],
    }


def explain(f, ctx=None):
    for cat, rx, text in EXPLAIN:
        if cat == f.cat and re.search(rx, f.title):
            return text.format_map(ctx) if ctx else text
    return ""


def event_hint(ev):
    p, i = ev.get("p", ""), ev.get("id")
    table = {("Microsoft-Windows-Kernel-Power", 41): "Unerwarteter Neustart",
             ("EventLog", 6008): "Unerwartetes Herunterfahren",
             ("Microsoft-Windows-WER-SystemErrorReporting", 1001): "Bluescreen",
             ("Display", 4101): "Grafiktreiber-Reset (TDR)",
             ("Service Control Manager", 7023): "Dienst abgebrochen \u2013 meist harmlos",
             ("Service Control Manager", 7000): "Dienst nicht gestartet \u2013 meist harmlos",
             ("Service Control Manager", 7009): "Dienst-Timeout \u2013 meist harmlos",
             ("Microsoft-Windows-Security-SPP", 8198): "Aktivierung fehlgeschlagen \u2013 keine Hardwarefrage",
             ("Microsoft-Windows-TPM-WMI", 1040): "Measured Boot \u2013 nach Boardwechsel \u00fcblich",
             ("Microsoft-Windows-DistributedCOM", 10016): "Berechtigungswarnung \u2013 ignorieren",
             ("Microsoft-Windows-DistributedCOM", 10010): "Dienst-Timeout \u2013 meist harmlos",
             ("Microsoft-Windows-Kernel-General", 12): "Systemstart",
             ("Microsoft-Windows-Winlogon", 7001): "Anmeldung",
             ("Microsoft-Windows-Winlogon", 7002): "Abmeldung",
             ("Microsoft-Windows-Kernel-General", 1): "Uhrzeit ge\u00e4ndert (Synchronisation oder von Hand)",
             ("Microsoft-Windows-Kernel-Power", 109): "Herunterfahren/Neustart eingeleitet",
             ("Microsoft-Windows-Kernel-General", 13): "Ordentliches Herunterfahren",
             ("EventLog", 6005): "Systemstart",
             ("EventLog", 6006): "Ordentliches Herunterfahren",
             ("User32", 1074): "Herunterfahren/Neustart \u2013 von Benutzer oder Update angefordert",
             ("Microsoft-Windows-Kernel-Power", 42): "Energiesparmodus",
             ("Microsoft-Windows-Kernel-Power", 107): "Aufwachen aus dem Energiesparmodus",
             ("Microsoft-Windows-Power-Troubleshooter", 1): "Aufwachen aus dem Energiesparmodus"}
    if (p, i) in table:
        return table[(p, i)]
    if p == "Microsoft-Windows-WHEA-Logger":
        return "Hardwarefehler-Meldung"
    if p == "Windows Error Reporting" and "RADAR_PRE_LEAK" in (ev.get("msg") or ""):
        return "Speicherleck-Verdacht eines Programms \u2013 harmlos"
    return ""


# --------------------------------------------------------------------------
# Diagrammdaten
# --------------------------------------------------------------------------
MAX_ROWS = 6000


def decimate_index(n):
    """Liefert Bucket-Grenzen fuer Min/Max-Verdichtung (oder None, wenn nicht noetig)."""
    if n <= MAX_ROWS:
        return None
    nb = MAX_ROWS // 2
    size = n / nb
    return [(int(k * size), int((k + 1) * size)) for k in range(nb)]


def build_chart_data(an: Analysis):
    s, L, cfg = an.s, an.log, an.cfg
    buckets = decimate_index(L.n)
    t = L.t
    if buckets:
        X = []
        for a, b in buckets:
            X += [round(t[a], 1), round(t[b - 1], 1)]
    else:
        X = [round(x, 1) for x in t]

    def series(vals, nd):
        if buckets:
            out = []
            for a, b in buckets:
                seg = [(i, vals[i]) for i in range(a, b) if vals[i] == vals[i]]
                if not seg:
                    out += [None, None]
                    continue
                imin = min(seg, key=lambda p: p[1])
                imax = max(seg, key=lambda p: p[1])
                pair = [imin, imax] if imin[0] <= imax[0] else [imax, imin]
                out += [round(pair[0][1], nd), round(pair[1][1], nd)]
            return out
        return [round(x, nd) if x == x else None for x in vals]

    charts = []

    def chart(cid, title, unit, items, nd=None, bands=None, inc=None, step=False, logy=False, h=None, floor0=False):
        items = [(name, c) for name, c in items if c is not None and c.n_valid > 0][:8]
        if not items:
            return
        dd = digits_for(unit) if nd is None else nd
        ser = []
        for k, (name, c) in enumerate(items):
            vals = c.vals if not callable(c) else c()
            ser.append({"name": name, "c": k, "y": series(vals, dd + 1)})
        charts.append({"id": cid, "title": title, "unit": unit, "d": dd, "series": ser, "bands": bands or [],
                       "inc": inc or [], "step": step, "logy": logy, "h": h or 190, "floor0": floor0})

    T = cfg["temperaturen"]
    ck = cpu_temp_key(cfg, s.is_x3d)
    chart("cpu_t", "CPU-Temperatur", "\u00b0C",
          [("Tctl/Tdie", s.tctl)] + [(c.short.replace("CPU ", "").replace(" (Tdie)", ""), c) for c in s.ccd] + [("IOD-Hotspot", s.iod)],
          bands=[{"y": T[ck][0], "lvl": 1, "label": f"Hinweis {T[ck][0]} \u00b0C"},
                 {"y": T[ck][1], "lvl": 2, "label": f"Warnung {T[ck][1]} \u00b0C"}])
    chart("cpu_p", "CPU-Leistung", "W", [("PPT", s.ppt), ("Kerne", s.core_power), ("SoC", s.soc_power)], floor0=True)
    chart("load", "Auslastung", "%", [("CPU gesamt", s.usage), ("GPU", s.gpu_load)], inc=[0, 100])
    chart("cpu_c", "CPU-Takt", "MHz", [("\u00d8 Kerntakt", s.clock), ("\u00d8 effektiv", s.eff_clock)])
    chart("cpu_v", "CPU-Spannungen", "V",
          [("Vcore" + (" (SVI3)" if s.vcore and "SVI3" in s.vcore.name else ""), s.vcore),
           ("SoC" + (" (SVI3)" if s.vsoc and "SVI3" in s.vsoc.name else ""), s.vsoc),
           ("Vcore (Board)", s.vcore_board if s.vcore else s.vcore_board)])
    # Netzteil-Schienen als Abweichung in %
    rails = []
    for c, nom, lab in s.rails + s.gpu_rails:
        if lab not in ("3VSB", "5VSB"):
            rails.append((lab, c, nom))
    for d in s.dimms:
        if d["vin"]:
            rails.append((f"RAM {d['channel'] or ''} VIN".replace("  ", " "), d["vin"], 5.0))
    if rails:
        tol = cfg["schienen_toleranz_prozent"]
        items = []
        for lab, c, nom in rails[:8]:
            dev = array("d", ((x / nom - 1) * 100 if x == x and x > 0 else NAN for x in c.vals))
            pc = Column(idx=-1, name=lab, group="", unit="%", vals=dev)
            pc.finalize()
            items.append((lab, pc))
        chart("rails", "Netzteil-Schienen (Abweichung vom Sollwert)", "%", items, nd=2,
              bands=[{"y": tol[0], "lvl": 1, "label": f"+{tol[0]} %"}, {"y": -tol[0], "lvl": 1, "label": f"\u2212{tol[0]} %"},
                     {"y": tol[2], "lvl": 3, "label": f"+{tol[2]} % (ATX)"}, {"y": -tol[2], "lvl": 3, "label": f"\u2212{tol[2]} % (ATX)"}],
              inc=[-tol[0], tol[0]])
    chart("ram_t", "RAM-Module Temperatur", "\u00b0C", [(d["label"], d["temp"]) for d in s.dimms])
    chart("ram_v", "RAM-Spannungen", "V",
          [(f"{d['channel'] or k + 1} VDD", d["vdd"]) for k, d in enumerate(s.dimms)] +
          [(f"{d['channel'] or k + 1} VDDQ", d["vddq"]) for k, d in enumerate(s.dimms)] + [("VDDIO_MEM (CPU)", s.vddio)])
    chart("gpu_t", "GPU-Temperaturen", "\u00b0C", [("Kern", s.gpu_temp), ("Hotspot", s.gpu_hot), ("Speicher", s.gpu_mem)])
    gb = []
    if s.gpu_limit_nom and isnum(s.gpu_limit_nom.vavg):
        gb = [{"y": round(s.gpu_limit_nom.vavg), "lvl": 0, "label": f"Limit {round(s.gpu_limit_nom.vavg)} W"}]
    chart("gpu_p", "GPU-Leistung", "W", [("GPU", s.gpu_power)], bands=gb, floor0=True)
    chart("gpu_l", "GPU PCIe-Link", "GT/s", [("Link", s.gpu_link)], step=True, h=130, floor0=True)
    pc_items = [(k.replace(" Count", "").replace("Errors", "Err."), c) for k, c in s.pcie.items()
                if c.vmax > c.vmin or k == "Recovery Count"]
    chart("pcie", "GPU PCIe-Z\u00e4hler (kumuliert)", "", pc_items, nd=0, step=True, h=150)
    if s.whea and s.whea.vmax > 0:
        chart("whea", "WHEA-Fehler (kumuliert)", "", [("WHEA", s.whea)], nd=0, step=True, h=120, floor0=True)
    thr = cfg["frametime_spitze_ms"]
    chart("ft", "Frametimes" + (f" \u00b7 {s.pm_proc}" if s.pm_proc else ""), "ms",
          [("\u00d8", s.ft_avg)] + [(re.sub(r"^.*\((.+)\).*$", r"\1", c.short), c) for c in s.ft_hi],
          bands=[{"y": thr, "lvl": 1, "label": f"{thr} ms"}], logy=True)
    chart("fps", "Bildrate", "FPS", [("\u00d8", s.fps), ("1 % low", s.fps_low)], floor0=True)
    # Laufwerke mit zweitem Sensor (NVMe, meist Controller) bekommen ein eigenes Diagramm mit Grenzwerten
    for k, d in enumerate(s.drives):
        if d["temp2"]:
            lim = an.drive_levels(d)
            chart(f"drv{k}", f"SSD {d['label']}", "\u00b0C",
                  [(f"Sensor {j + 1}", c) for j, c in enumerate(Analysis.drive_sensors(d))],
                  bands=[{"y": lim[0], "lvl": 1, "label": f"Hinweis {fnum(lim[0], 0)} \u00b0C"},
                         {"y": lim[1], "lvl": 2, "label": f"Warnung {fnum(lim[1], 0)} \u00b0C"}])
    chart("drv", "Laufwerke", "\u00b0C", [(d["label"][:32], d["temp"]) for d in s.drives if not d["temp2"]])
    chart("board", "Mainboard-Temperaturen", "\u00b0C", [(c.short, c) for c in s.board_temps])
    chart("fans", "L\u00fcfter", "RPM", [(c.short, c) for c in s.fans] + [(c.short, c) for c in s.gpu_fans], floor0=True)

    markers = []
    for f in an.F:
        if f.level < 1 and f.cat != "Markierung":
            continue
        for tm in ([f.t] if f.t is not None else []) + list(f.marks):
            if tm is not None and -1 <= tm <= L.duration + 1:
                markers.append({"t": round(tm, 1), "lvl": f.level, "label": f.title})
    seen, mk = set(), []
    for m in sorted(markers, key=lambda m: (-m["lvl"], m["t"])):
        k = round(m["t"])
        if k in seen:
            continue
        seen.add(k)
        mk.append(m)
    t0ms = int((L.t0 - dt.datetime(1970, 1, 1)).total_seconds() * 1000)
    glim = max(L.interval * cfg["log_luecke_faktor"], L.interval + 1.0) if L.interval else 0
    if buckets:
        glim = max(glim, (t[buckets[0][1] - 1] - t[buckets[0][0]]) * 2.5)
    return {"x": X, "t0ms": t0ms, "dst": [[round(r, 1), d] for r, d in L.meta.get("dst", [])], "glim": round(glim, 2), "multiday": (L.at(L.duration).date() != L.t0.date()), "charts": charts,
            "markers": mk[:400], "gaps": [[round(a, 1), round(b, 1)] for a, b in an.gaps],
            "decimated": bool(buckets)}


# --------------------------------------------------------------------------
# HTML
# --------------------------------------------------------------------------
CSS = r"""
:root{color-scheme:light;--page:#f9f9f7;--surface:#fcfcfb;--ink:#0b0b0b;--ink-2:#52514e;--muted:#898781;--grid:#e1e0d9;--axis:#c3c2b7;
--border:rgba(11,11,11,.10);--s1:#2a78d6;--s2:#eb6834;--s3:#1baf7a;--s4:#eda100;--s5:#e87ba4;--s6:#008300;--s7:#4a3aa7;--s8:#e34948;
--good:#0ca30c;--warning:#fab219;--serious:#ec835a;--critical:#d03b3b;--info:#2a78d6;--gapfill:rgba(137,135,129,.16);--sel:rgba(42,120,214,.12);--hover:rgba(11,11,11,.04)}
@media (prefers-color-scheme:dark){:root:where(:not([data-theme="light"])){color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;--ink-2:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--axis:#383835;--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767;
--info:#3987e5;--gapfill:rgba(137,135,129,.20);--sel:rgba(57,135,229,.18);--hover:rgba(255,255,255,.05)}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface:#1a1a19;--ink:#ffffff;--ink-2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--axis:#383835;
--border:rgba(255,255,255,.10);--s1:#3987e5;--s2:#d95926;--s3:#199e70;--s4:#c98500;--s5:#d55181;--s6:#008300;--s7:#9085e9;--s8:#e66767;--info:#3987e5;
--gapfill:rgba(137,135,129,.20);--sel:rgba(57,135,229,.18);--hover:rgba(255,255,255,.05)}
*{box-sizing:border-box}
html{background:var(--page)}
body{margin:0;background:var(--page);color:var(--ink);font:14px/1.5 system-ui,-apple-system,"Segoe UI",sans-serif}
.wrap{max-width:1240px;margin:0 auto;padding:20px 16px 48px}
a{color:var(--info)}
h1{font-size:20px;margin:0 0 2px;font-weight:650;letter-spacing:-.01em;overflow-wrap:anywhere}
h2{font-size:15px;margin:32px 0 10px;font-weight:650;display:flex;align-items:baseline;gap:10px}
h2 small{font-weight:400;color:var(--muted);font-size:12.5px}
h3{font-size:13px;margin:0;font-weight:600}
.top{display:flex;gap:16px;align-items:flex-start;justify-content:space-between;flex-wrap:wrap}
.meta{color:var(--ink-2);font-size:13px}
.hw{color:var(--muted);font-size:12.5px;margin-top:4px}
.status{display:flex;align-items:center;gap:10px;padding:10px 14px;border-radius:10px;background:var(--surface);border:1px solid var(--border);font-weight:650;white-space:nowrap}
.status .ic{width:26px;height:26px;border-radius:50%;display:grid;place-items:center;color:#fff;font-size:13px;font-weight:700}
.st-0 .ic{background:var(--good)}.st-1 .ic{background:var(--warning);color:#0b0b0b}.st-2 .ic{background:var(--serious);color:#0b0b0b}.st-3 .ic{background:var(--critical)}
.status small{display:block;font-weight:400;color:var(--muted);font-size:12px}
.toc{display:flex;gap:6px;flex-wrap:wrap;margin:16px 0 0}
.toc a{font-size:12.5px;text-decoration:none;color:var(--ink-2);padding:4px 10px;border-radius:999px;border:1px solid var(--border);background:var(--surface)}
.toc a:hover{color:var(--ink)}
.tiles{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:8px;margin-top:16px}
.tile{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 12px}
.tile .k{font-size:12px;color:var(--muted)}.tile .v{font-size:18px;font-weight:650}.tile .v small{font-size:12px;font-weight:400;color:var(--ink-2)}
.findings{list-style:none;margin:0;padding:0;display:grid;gap:6px}
.f{background:var(--surface);border:1px solid var(--border);border-left:4px solid var(--muted);border-radius:8px;padding:9px 12px;display:grid;grid-template-columns:auto 1fr auto;gap:4px 10px;align-items:start}
.f.lvl-1{border-left-color:var(--warning)}.f.lvl-2{border-left-color:var(--serious)}.f.lvl-3{border-left-color:var(--critical)}.f.lvl-0{border-left-color:var(--axis)}
.badge{font-size:11.5px;font-weight:650;display:inline-flex;gap:5px;align-items:center;white-space:nowrap;color:var(--ink-2);padding-top:1px;min-width:74px}
.badge i{font-style:normal;width:16px;height:16px;border-radius:50%;display:inline-grid;place-items:center;font-size:9.5px;color:#fff;background:var(--muted)}
.lvl-1 .badge i{background:var(--warning);color:#0b0b0b}.lvl-2 .badge i{background:var(--serious);color:#0b0b0b}.lvl-3 .badge i{background:var(--critical)}
.f .tt{font-weight:600}.f .cat{color:var(--muted);font-weight:400;font-size:12px;margin-left:6px}
.f .dt{grid-column:2/4;color:var(--ink-2);font-size:13px}
.f .tm{font-size:12.5px;font-variant-numeric:tabular-nums;white-space:nowrap}
.f .dt:empty{display:none}
details.info{margin-top:6px}
details.info summary{cursor:pointer;color:var(--ink-2);font-size:13px;padding:4px 0}
.cfg{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:0 24px;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px 14px}
.cfg div{display:flex;justify-content:space-between;gap:12px;padding:5px 0;border-bottom:1px solid var(--grid);font-size:13px}
.cfg div span:first-child{color:var(--muted)}.cfg div span:last-child{text-align:right;overflow-wrap:anywhere}
.cfg .chg span:last-child{font-weight:650}
.cfg .chg span:last-child::after{content:" \25CF";color:var(--serious)}
.tbl{width:100%;border-collapse:collapse;font-size:13px;background:var(--surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.tbl th,.tbl td{padding:6px 10px;text-align:right;border-bottom:1px solid var(--grid);font-variant-numeric:tabular-nums;white-space:nowrap}
.tbl th:first-child,.tbl td:first-child{text-align:left;white-space:normal}
.tbl th{font-weight:600;color:var(--ink-2);font-size:12px;background:var(--surface);position:sticky;top:0}
.tbl tbody tr:hover{background:var(--hover)}
.tbl td.u{color:var(--muted);text-align:left}
.tbl td.g{color:var(--muted);text-align:left;font-size:12px;white-space:normal}
.scroll{overflow:auto;border-radius:10px;border:1px solid var(--border)}
.scroll .tbl{border:0;border-radius:0}
.tall{max-height:560px}
.toolbar{display:flex;gap:6px;flex-wrap:wrap;align-items:center;position:sticky;top:0;z-index:5;background:var(--page);padding:8px 0}
.toolbar button{font:inherit;font-size:12.5px;padding:5px 11px;border-radius:999px;border:1px solid var(--border);background:var(--surface);color:var(--ink);cursor:pointer}
.toolbar button:hover{border-color:var(--axis)}
.toolbar .rng{margin-left:auto;color:var(--muted);font-size:12.5px;font-variant-numeric:tabular-nums}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}
@media (max-width:900px){.grid{grid-template-columns:1fr}}
.card{background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:10px 12px 8px;min-width:0}
.chead{display:flex;flex-direction:column;gap:2px;margin-bottom:4px}
.chead h3 .unit{color:var(--muted);font-weight:400;margin-left:6px}
.readout{min-height:20px;font-size:12px;color:var(--ink-2);display:flex;flex-wrap:wrap;gap:2px 12px;font-variant-numeric:tabular-nums}
.readout .rt{color:var(--muted)}
.readout .ri{display:inline-flex;align-items:center;gap:5px}
.readout b{color:var(--ink);font-weight:650}
.key{display:inline-block;width:12px;height:2px;border-radius:1px}
.cwrap{position:relative;width:100%}
.cwrap canvas{position:absolute;inset:0;width:100%;height:100%}
.cwrap canvas.ov{touch-action:pan-y;cursor:crosshair}
.legend{display:flex;flex-wrap:wrap;gap:2px 4px;margin-top:4px}
.legend button{font:inherit;font-size:12px;color:var(--ink-2);background:none;border:0;padding:2px 6px;border-radius:6px;cursor:pointer;display:inline-flex;align-items:center;gap:6px}
.legend button:hover{background:var(--hover)}
.legend button[aria-pressed="false"]{opacity:.4;text-decoration:line-through}
.note{color:var(--muted);font-size:12.5px;margin:6px 0 0}
input.search{font:inherit;font-size:13px;padding:6px 10px;border-radius:8px;border:1px solid var(--border);background:var(--surface);color:var(--ink);width:min(360px,100%);margin:0 0 8px}
.lv{display:inline-flex;align-items:center;gap:5px}
.lv i{font-style:normal;width:9px;height:9px;border-radius:50%;background:var(--muted);display:inline-block}
.lv.l1 i{background:var(--warning)}.lv.l2 i{background:var(--serious)}.lv.l3 i{background:var(--critical)}.lv.l0 i{background:var(--good)}
.ev-msg{max-width:560px;min-width:240px;white-space:normal!important;text-align:left!important;color:var(--ink-2)}
footer{margin-top:40px;color:var(--muted);font-size:12px}
details.why{grid-column:2/4;margin-top:2px}
details.why summary{cursor:pointer;font-size:12.5px;color:var(--info);list-style-position:inside}
details.why p{margin:6px 0 2px;padding:8px 10px;border-radius:8px;background:var(--hover);color:var(--ink-2);font-size:13px;line-height:1.55}
details.guide{margin:12px 0 0;background:var(--surface);border:1px solid var(--border);border-radius:10px;padding:8px 14px}
details.guide summary{cursor:pointer;font-weight:600;font-size:13px}
details.guide p{color:var(--ink-2);font-size:13px;margin:8px 0}
.tbl.mini{margin:8px 0;font-size:12.5px}
h2.crash{color:var(--critical)}
"""

JS = r"""
(() => {
'use strict';
const D = JSON.parse(document.getElementById('hw-data').textContent);
const X = D.x || [], N = X.length;
const root = document.documentElement;
const S = { x0: N ? X[0] : 0, x1: N ? X[N - 1] : 1, hover: null, drag: null };
const charts = [];
let C = {};
const cv = n => getComputedStyle(root).getPropertyValue(n).trim();
function readColors() {
  C = { ink: cv('--ink'), ink2: cv('--ink-2'), muted: cv('--muted'), grid: cv('--grid'), axis: cv('--axis'),
        surface: cv('--surface'), gap: cv('--gapfill'), sel: cv('--sel'),
        s: [1, 2, 3, 4, 5, 6, 7, 8].map(i => cv('--s' + i)),
        lvl: [cv('--muted'), cv('--warning'), cv('--serious'), cv('--critical')] };
}
const pad2 = n => String(n).padStart(2, '0');
const DST = D.dst || [];
function wallDelta(sec) { let d = 0; for (const p of DST) if (sec >= p[0] - 1e-6) d = p[1]; return d; }
const dateOf = sec => new Date(D.t0ms + (sec + wallDelta(sec)) * 1000);
function fmtClock(sec, withSec) { const d = dateOf(sec); return pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()) + (withSec === false ? '' : ':' + pad2(d.getUTCSeconds())); }
function fmtDate(sec) { const d = dateOf(sec); return pad2(d.getUTCDate()) + '.' + pad2(d.getUTCMonth() + 1) + '.'; }
const CAT = Array.isArray(D.xlabels);
function fmtFull(sec) { if (CAT) return D.xlabels[Math.round(sec)] || ''; return (D.multiday ? fmtDate(sec) + ' ' : '') + fmtClock(sec); }
function fmtVal(v, d) { if (v == null || !isFinite(v)) return '\u2013'; return v.toFixed(d).replace('.', ','); }
function fmtDur(s) { s = Math.round(s); const h = Math.floor(s / 3600), m = Math.floor(s % 3600 / 60), r = s % 60; return h ? h + ' h ' + pad2(m) + ' min' : m ? m + ' min ' + pad2(r) + ' s' : r + ' s'; }
function lb(a, v) { let lo = 0, hi = a.length; while (lo < hi) { const m = (lo + hi) >> 1; if (a[m] < v) lo = m + 1; else hi = m; } return lo; }
function nearestIdx(v) { let i = lb(X, v); if (i >= N) return N - 1; if (i > 0 && (v - X[i - 1]) < (X[i] - v)) i--; return i; }
const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800, 1209600, 2592000];
function timeTicks(a, b, px) {
  if (CAT) { const tg = Math.max(2, Math.floor(px / 110)), st = Math.max(1, Math.ceil((b - a) / tg)), out = []; for (let t = Math.ceil(a); t <= b; t += st) out.push(t); return { ticks: out, step: -1 }; }
  const target = Math.max(2, Math.floor(px / 92)), span = b - a;
  let step = STEPS[STEPS.length - 1];
  for (const s of STEPS) { if (span / s <= target) { step = s; break; } }
  const base = D.t0ms / 1000, out = [];
  for (let t = Math.ceil((a + base) / step) * step - base; t <= b + 1e-9; t += step) out.push(t);
  return { ticks: out, step };
}
function fmtTick(t, step) {
  if (CAT) return D.xlabels[Math.round(t)] || '';
  if (step >= 86400) return fmtDate(t);
  if (D.multiday && step >= 3600) return fmtDate(t) + ' ' + fmtClock(t, false);
  return step >= 60 ? fmtClock(t, false) : fmtClock(t);
}
function niceTicks(lo, hi, count) {
  const raw = (hi - lo) / Math.max(1, count), mag = Math.pow(10, Math.floor(Math.log10(raw))), nrm = raw / mag;
  const step = (nrm < 1.5 ? 1 : nrm < 3 ? 2 : nrm < 7 ? 5 : 10) * mag, out = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + step * 1e-9; v += step) out.push(+v.toFixed(10));
  return { ticks: out, step };
}
function logTicks(lo, hi) {
  const out = [];
  for (let e = Math.floor(Math.log10(lo)); e <= Math.ceil(Math.log10(hi)); e++)
    for (const m of [1, 2, 5]) { const v = m * Math.pow(10, e); if (v >= lo && v <= hi) out.push(v); }
  return { ticks: out, step: 1 };
}
const decFor = step => step >= 1 ? 0 : Math.min(4, Math.ceil(-Math.log10(step) - 1e-9));
function el(tag, cls, text) { const x = document.createElement(tag); if (cls) x.className = cls; if (text != null) x.textContent = text; return x; }

function buildChart(def, host) {
  const card = el('div', 'card chart');
  const head = el('div', 'chead');
  const h = el('h3', null, def.title);
  if (def.unit) h.appendChild(el('span', 'unit', def.unit));
  const read = el('div', 'readout');
  head.append(h, read);
  const wrap = el('div', 'cwrap'); wrap.style.height = (def.h || 190) + 'px';
  const cb = el('canvas'), co = el('canvas', 'ov');
  co.setAttribute('aria-hidden', 'true'); cb.setAttribute('role', 'img'); cb.setAttribute('aria-label', def.title);
  wrap.append(cb, co); card.append(head, wrap);
  const ch = { def, wrap, cb, co, read, hidden: new Set(), geo: null };
  if (def.series.length > 1) {
    const lg = el('div', 'legend');
    def.series.forEach((s, i) => {
      const b = el('button'); b.type = 'button'; b.setAttribute('aria-pressed', 'true');
      const k = el('span', 'key'); k.style.background = 'var(--s' + (s.c % 8 + 1) + ')';
      b.append(k, document.createTextNode(s.name));
      b.addEventListener('click', () => { if (ch.hidden.has(i)) ch.hidden.delete(i); else ch.hidden.add(i); b.setAttribute('aria-pressed', ch.hidden.has(i) ? 'false' : 'true'); draw(ch); drawOverlay(ch); });
      lg.append(b);
    });
    card.append(lg);
  }
  host.append(card);
  co.addEventListener('pointermove', ev => { const s = secAt(ch, ev); if (s == null) return; S.hover = s; if (S.drag) S.drag.cur = s; overlayAll(); });
  co.addEventListener('pointerdown', ev => { if (ev.button !== 0) return; const s = secAt(ch, ev); if (s == null) return; S.drag = { start: s, cur: s, px: ev.clientX }; try { co.setPointerCapture(ev.pointerId); } catch (_) {} });
  co.addEventListener('pointerup', ev => { const d = S.drag; S.drag = null; if (!d) return; if (Math.abs(ev.clientX - d.px) > 6) setView(Math.min(d.start, d.cur), Math.max(d.start, d.cur)); else overlayAll(); });
  co.addEventListener('pointercancel', () => { S.drag = null; overlayAll(); });
  co.addEventListener('pointerleave', () => { if (!S.drag) { S.hover = null; overlayAll(); } });
  co.addEventListener('dblclick', () => setView(X[0] - PADX, X[N - 1] + PADX));
  charts.push(ch);
}
function secAt(ch, ev) {
  const g = ch.geo; if (!g) return null;
  const r = ch.co.getBoundingClientRect(), f = (ev.clientX - r.left - g.l) / g.w;
  return S.x0 + Math.min(1, Math.max(0, f)) * (S.x1 - S.x0);
}
function draw(ch) {
  const def = ch.def, dpr = window.devicePixelRatio || 1;
  const W = ch.wrap.clientWidth, H = ch.wrap.clientHeight;
  if (!W || !H) return;
  for (const c of [ch.cb, ch.co]) { c.width = Math.round(W * dpr); c.height = Math.round(H * dpr); }
  const ctx = ch.cb.getContext('2d'); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, W, H);
  ctx.font = '11px system-ui,-apple-system,"Segoe UI",sans-serif';
  const i0 = Math.max(0, lb(X, S.x0) - 1), i1 = Math.min(N - 1, lb(X, S.x1));
  let lo = Infinity, hi = -Infinity;
  def.series.forEach((s, si) => { if (ch.hidden.has(si)) return; const y = s.y; for (let i = i0; i <= i1; i++) { const v = y[i]; if (v == null) continue; if (def.logy && v <= 0) continue; if (v < lo) lo = v; if (v > hi) hi = v; } });
  if (!isFinite(lo)) { ch.geo = null; ctx.fillStyle = C.muted; ctx.textAlign = 'center'; ctx.fillText('Keine Daten im Ausschnitt', W / 2, H / 2); return; }
  for (const v of def.inc || []) { if (v < lo) lo = v; if (v > hi) hi = v; }
  if (def.floor0 && lo > 0) lo = 0;
  let yt;
  if (def.logy) { lo = Math.max(lo * 0.8, 0.1); hi = Math.max(hi * 1.25, lo * 2); yt = logTicks(lo, hi); }
  else {
    if (hi === lo) { const d = Math.abs(hi) * 0.05 || 1; lo -= d; hi += d; }
    const p = (hi - lo) * 0.08; hi += p; if (!(def.floor0 && lo === 0)) lo -= p;
    yt = niceTicks(lo, hi, Math.max(2, Math.floor(H / 44)));
  }
  const yd = def.logy ? 0 : decFor(yt.step);
  const labels = yt.ticks.map(v => def.logy ? (v >= 1 ? String(v) : fmtVal(v, 1)) : fmtVal(v, yd));
  let L = 30; for (const t of labels) L = Math.max(L, ctx.measureText(t).width + 12);
  const R = 10, T = 8, B = 22, w = Math.max(10, W - L - R), h = Math.max(10, H - T - B);
  const span = (S.x1 - S.x0) || 1;
  const fx = t => L + (t - S.x0) / span * w;
  const lgl = Math.log10(lo), lgh = Math.log10(hi);
  const fy = def.logy ? (v => T + h - (Math.log10(Math.max(v, lo)) - lgl) / (lgh - lgl) * h) : (v => T + h - (v - lo) / (hi - lo) * h);
  ch.geo = { l: L, w, t: T, h, fx, fy, lo, hi };
  ctx.fillStyle = C.gap;
  for (const g of D.gaps || []) { if (g[1] < S.x0 || g[0] > S.x1) continue; const a = Math.max(L, fx(g[0])), b = Math.min(L + w, fx(g[1])); ctx.fillRect(a, T, Math.max(2, b - a), h); }
  ctx.lineWidth = 1; ctx.textAlign = 'right'; ctx.textBaseline = 'middle';
  yt.ticks.forEach((v, k) => { const y = Math.round(fy(v)) + 0.5; if (y < T - 1 || y > T + h + 1) return; ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + w, y); ctx.stroke(); ctx.fillStyle = C.muted; ctx.fillText(labels[k], L - 6, y); });
  const xt = timeTicks(S.x0, S.x1, w); ctx.textAlign = 'center'; ctx.textBaseline = 'top';
  for (const t of xt.ticks) {
    const x = Math.round(fx(t)) + 0.5; ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(x, T); ctx.lineTo(x, T + h); ctx.stroke();
    const lab = fmtTick(t, xt.step), tw = ctx.measureText(lab).width;
    ctx.fillStyle = C.muted; ctx.textAlign = x - tw / 2 < L - 6 ? 'left' : x + tw / 2 > W - 2 ? 'right' : 'center';
    ctx.fillText(lab, ctx.textAlign === 'left' ? Math.max(x, 2) : ctx.textAlign === 'right' ? Math.min(x, W - 2) : x, T + h + 6);
  }
  ctx.strokeStyle = C.axis; ctx.beginPath(); ctx.moveTo(L, T + h + 0.5); ctx.lineTo(L + w, T + h + 0.5); ctx.stroke();
  let bk = 0;
  for (const b of def.bands || []) {
    if (b.y < lo || b.y > hi) continue;
    const left = (bk++ % 2) === 1;
    const y = Math.round(fy(b.y)) + 0.5;
    ctx.strokeStyle = C.lvl[b.lvl] || C.muted; ctx.setLineDash([4, 4]); ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(L + w, y); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = C.ink2; ctx.textAlign = left ? 'left' : 'right'; ctx.textBaseline = b.y >= 0 ? 'bottom' : 'top';
    ctx.fillText(b.label, left ? L + 4 : L + w - 12, b.y >= 0 ? y - 2 : y + 2);
  }
  ctx.save(); ctx.beginPath(); ctx.rect(L, T - 2, w, h + 4); ctx.clip();
  def.series.forEach((s, si) => { if (!ch.hidden.has(si)) drawSeries(ctx, s.y, i0, i1, fx, fy, w, def.step, C.s[s.c % 8]); });
  if (D.dots && (i1 - i0) < 120) def.series.forEach((s, si) => {
    if (ch.hidden.has(si)) return;
    for (let i = i0; i <= i1; i++) { const v = s.y[i]; if (v == null) continue; const x = fx(X[i]), y = fy(v);
      ctx.fillStyle = C.surface; ctx.beginPath(); ctx.arc(x, y, 5, 0, 6.2832); ctx.fill();
      ctx.fillStyle = C.s[s.c % 8]; ctx.beginPath(); ctx.arc(x, y, 3.5, 0, 6.2832); ctx.fill(); }
  });
  ctx.restore();
  for (const m of D.markers || []) {
    if (m.t < S.x0 || m.t > S.x1) continue;
    const x = Math.round(fx(m.t)) + 0.5; ctx.strokeStyle = C.lvl[m.lvl]; ctx.lineWidth = 1.25; ctx.setLineDash([2, 3]);
    ctx.beginPath(); ctx.moveTo(x, T + 6); ctx.lineTo(x, T + h); ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = C.lvl[m.lvl]; ctx.beginPath(); ctx.moveTo(x - 4.5, T - 1); ctx.lineTo(x + 4.5, T - 1); ctx.lineTo(x, T + 6); ctx.closePath(); ctx.fill();
  }
}
function drawSeries(ctx, y, i0, i1, fx, fy, w, step, color) {
  ctx.strokeStyle = color; ctx.lineWidth = 1.75; ctx.lineJoin = 'round'; ctx.lineCap = 'round'; ctx.beginPath();
  if (!step && (i1 - i0) > w * 1.5) {
    let cur = null, mn = 0, mx = 0, f = 0, l = 0, open = false, pt = -Infinity;
    const flush = () => { if (cur == null) return; const px = cur + 0.5; if (!open) { ctx.moveTo(px, fy(f)); open = true; } else ctx.lineTo(px, fy(f)); ctx.lineTo(px, fy(mn)); ctx.lineTo(px, fy(mx)); ctx.lineTo(px, fy(l)); };
    for (let i = i0; i <= i1; i++) {
      const v = y[i];
      if (v == null) { flush(); cur = null; open = false; continue; }
      if (D.glim && X[i] - pt > D.glim) { flush(); cur = null; open = false; }
      pt = X[i];
      const px = Math.floor(fx(X[i]));
      if (px !== cur) { flush(); cur = px; mn = mx = f = l = v; } else { if (v < mn) mn = v; if (v > mx) mx = v; l = v; }
    }
    flush();
  } else {
    let open = false, py = 0, pt = 0;
    for (let i = i0; i <= i1; i++) {
      const v = y[i]; if (v == null) { open = false; continue; }
      if (open && D.glim && X[i] - pt > D.glim) open = false;
      const px = fx(X[i]), yy = fy(v);
      if (!open) { ctx.moveTo(px, yy); open = true; } else if (step) { ctx.lineTo(px, py); ctx.lineTo(px, yy); } else ctx.lineTo(px, yy);
      py = yy; pt = X[i];
    }
  }
  ctx.stroke();
}
function drawOverlay(ch) {
  const g = ch.geo, c = ch.co, dpr = window.devicePixelRatio || 1, ctx = c.getContext('2d');
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, c.width / dpr, c.height / dpr);
  ch.read.textContent = '';
  if (!g) return;
  if (S.drag) { const a = g.fx(Math.min(S.drag.start, S.drag.cur)), b = g.fx(Math.max(S.drag.start, S.drag.cur)); ctx.fillStyle = C.sel; ctx.fillRect(a, g.t, Math.max(1, b - a), g.h); }
  if (S.hover == null || S.hover < S.x0 || S.hover > S.x1) return;
  const i = nearestIdx(S.hover), x = Math.round(g.fx(X[i])) + 0.5;
  ctx.strokeStyle = C.ink2; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x, g.t); ctx.lineTo(x, g.t + g.h); ctx.stroke();
  ch.read.append(el('span', 'rt', fmtFull(X[i])));
  ch.def.series.forEach((s, si) => {
    if (ch.hidden.has(si)) return;
    const v = s.y[i];
    if (v != null) {
      const y = g.fy(v);
      if (y >= g.t - 2 && y <= g.t + g.h + 2) { ctx.fillStyle = C.surface; ctx.beginPath(); ctx.arc(x, y, 5, 0, 6.2832); ctx.fill(); ctx.fillStyle = C.s[s.c % 8]; ctx.beginPath(); ctx.arc(x, y, 3.2, 0, 6.2832); ctx.fill(); }
    }
    const it = el('span', 'ri'), k = el('span', 'key'); k.style.background = 'var(--s' + (s.c % 8 + 1) + ')';
    it.append(k, el('b', null, fmtVal(v, ch.def.d) + (ch.def.unit ? '\u2009' + ch.def.unit : '')), document.createTextNode(s.name));
    ch.read.append(it);
  });
}
let raf = 0;
function redrawAll() { cancelAnimationFrame(raf); raf = requestAnimationFrame(() => { charts.forEach(draw); charts.forEach(drawOverlay); }); }
function overlayAll() { charts.forEach(drawOverlay); }
const rng = document.getElementById('rng');
const PADX = CAT ? 0.35 : 0;
function setView(a, b) {
  if (!N) return;
  const lo = X[0] - PADX, hi = X[N - 1] + PADX;
  const minSpan = CAT ? 1 : 10;
  if (b - a < minSpan) { const m = (a + b) / 2; a = m - minSpan / 2; b = m + minSpan / 2; }
  a = Math.max(lo, a); b = Math.min(hi, b); if (b <= a) { a = lo; b = hi; }
  S.x0 = a; S.x1 = b; redrawAll();
  if (rng) rng.textContent = 'Ausschnitt ' + fmtFull(a) + ' \u2013 ' + fmtFull(b) + (CAT ? '' : ' (' + fmtDur(b - a) + ')');
}
function zoomOut() { const m = (S.x0 + S.x1) / 2, w = (S.x1 - S.x0) * 2; setView(m - w / 2, m + w / 2); }
const host = document.getElementById('charts');
if (host && N) {
  readColors();
  (D.charts || []).forEach(def => buildChart(def, host));
  setView(X[0] - PADX, X[N - 1] + PADX);
  if (window.ResizeObserver) new ResizeObserver(() => redrawAll()).observe(host); else window.addEventListener('resize', redrawAll);
  const mq = window.matchMedia('(prefers-color-scheme: dark)');
  const re = () => { readColors(); redrawAll(); };
  if (mq.addEventListener) mq.addEventListener('change', re); else if (mq.addListener) mq.addListener(re);
  const bind = (id, fn) => { const b = document.getElementById(id); if (b) b.addEventListener('click', fn); };
  bind('z-all', () => setView(X[0] - PADX, X[N - 1] + PADX));
  bind('z-10m', () => setView(X[N - 1] - 600, X[N - 1]));
  bind('z-60s', () => setView(X[N - 1] - 60, X[N - 1]));
  bind('z-out', zoomOut);
  document.querySelectorAll('[data-t]').forEach(a => a.addEventListener('click', ev => {
    ev.preventDefault(); const t = parseFloat(a.dataset.t); setView(t - 120, t + 120);
    document.getElementById('diagramme').scrollIntoView({ behavior: 'smooth', block: 'start' });
  }));
}
const sf = document.getElementById('sf');
if (sf) sf.addEventListener('input', () => {
  const q = sf.value.trim().toLowerCase();
  document.querySelectorAll('#sensors tbody tr').forEach(tr => { tr.style.display = !q || tr.textContent.toLowerCase().includes(q) ? '' : 'none'; });
});
})();
"""


def page(title, body, data=None):
    js = ""
    if data is not None:
        payload = json.dumps(clean_json(data), ensure_ascii=False, separators=(",", ":"), allow_nan=False).replace("</", "<\\/")
        js = f'<script type="application/json" id="hw-data">{payload}</script>\n<script>{JS}</script>'
    return (f'<!doctype html>\n<html lang="de">\n<head>\n<meta charset="utf-8">\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n<title>{e(title)}</title>\n'
            f'<style>{CSS}</style>\n</head>\n<body>\n<div class="wrap">\n{body}\n</div>\n{js}\n</body>\n</html>\n')


def status_box(level, sub):
    labels = {0: ("\u2713", "Unauff\u00e4llig"), 1: ("!", "Hinweise"), 2: ("\u25B2", "Warnung"), 3: ("\u2716", "Kritisch")}
    ic, lab = labels[level]
    return (f'<div class="status st-{level}" role="status"><span class="ic" aria-hidden="true">{ic}</span>'
            f'<div>{lab}<small>{e(sub)}</small></div></div>')


def tlink(an, t):
    if t is None:
        return ""
    return f'<a href="#diagramme" data-t="{t:.1f}">{e(an.log.clock(t))}</a>'


def render_report(an: Analysis, history_link=None):
    L, s = an.log, an.s
    name = os.path.basename(L.path)
    cnt = {k: sum(1 for f in an.F if f.level == k) for k in range(4)}
    sub = " \u00b7 ".join(f"{cnt[k]} {LEVELS[k]}" for k in (3, 2, 1) if cnt[k]) or "keine Auff\u00e4lligkeiten"
    end = L.at(L.duration)
    period = f"{L.t0:%d.%m.%Y %H:%M:%S} \u2013 " + (f"{end:%H:%M:%S}" if end.date() == L.t0.date() else f"{end:%d.%m.%Y %H:%M:%S}")
    hw = [x for x in (s.cpu_name, s.board_name, s.gpu_name) if x]
    parts = []
    parts.append('<div class="top"><div>')
    parts.append(f"<h1>{e(name)}</h1>")
    src = "LibreHardwareMonitor" if L.source == "LHM" else "HWiNFO"
    parts.append(f'<div class="meta">{e(src)} \u00b7 {e(period)} \u00b7 {e(fdur(L.duration))} \u00b7 {L.n} Messpunkte \u00e0 {fnum(L.interval, 1)} s'
                 + (f" \u00b7 {e(an.profile)}" if an.profile else "") + "</div>")
    if hw:
        parts.append(f'<div class="hw">{e(SEP.join(hw))}</div>')
    parts.append("</div>")
    parts.append(status_box(an.worst, sub))
    parts.append("</div>")
    toc = [("befunde", "Befunde"), ("konfig", "Konfiguration"), ("diagramme", "Diagramme"), ("kennzahlen", "Kennzahlen"),
           ("logende", "Letzte Minute"), ("ereignisse", "Ereignisse"), ("sensoren", "Alle Sensoren")]
    parts.append('<nav class="toc">' + "".join(f'<a href="#{a}">{b}</a>' for a, b in toc)
                 + (f'<a href="{e(history_link)}">Verlauf \u2192</a>' if history_link else "") + "</nav>")

    # Kacheln
    tiles = []

    def tile(k, v, unit=""):
        tiles.append(f'<div class="tile"><div class="k">{e(k)}</div><div class="v">{e(v)}'
                     + (f" <small>{e(unit)}</small>" if unit else "") + "</div></div>")
    if s.tctl:
        tile("CPU max", fnum(s.tctl.vmax, 1), "\u00b0C")
    if s.gpu_hot:
        tile("GPU-Hotspot max", fnum(s.gpu_hot.vmax, 1), "\u00b0C")
    elif s.gpu_temp:
        tile("GPU max", fnum(s.gpu_temp.vmax, 1), "\u00b0C")
    dt_ = [d["temp"].vmax for d in s.dimms if d["temp"]]
    if dt_:
        tile("RAM max", fnum(max(dt_), 1), "\u00b0C")
    if s.gpu_rails:
        tile("12 V min (GPU)", fnum(s.gpu_rails[0][0].vmin, 3), "V")
    elif s.rails:
        tile("12 V min (Board)", fnum(s.rails[0][0].vmin, 3), "V")
    if s.whea:
        tile("WHEA neu", str(int(s.whea.vlast - s.whea.vfirst)))
    if s.pcie:
        tr = an.kpi.get("pcie_err_trans") or 0
        tile("PCIe-Fehler unter Last", str(an.kpi.get("pcie_err") or 0), f"+{tr} bei Linkwechseln" if tr else "")
    if s.dimms:
        tile("RAM-Module", str(len(s.dimms)), f"von {an.cfg.get('erwartete_riegel')}")
    if s.fps and not s.pm_desktop:
        tile("Bildrate \u00d8", fnum(s.fps.vavg, 0), "FPS")
    parts.append('<div class="tiles">' + "".join(tiles) + "</div>")
    parts.append(f'<details class="guide"><summary>So liest du diesen Bericht</summary>{GUIDE}</details>')

    # Befunde
    main = [f for f in an.F if f.level >= 1]
    info = [f for f in an.F if f.level == 0]

    ctx = explain_context(an)

    def fitem(f):
        why = explain(f, ctx)
        return (f'<li class="f lvl-{f.level}"><span class="badge"><i aria-hidden="true">{LEVEL_ICON[f.level]}</i>{LEVELS[f.level]}</span>'
                f'<div><span class="tt">{e(f.title)}</span><span class="cat">{e(f.cat)}</span></div>'
                f'<span class="tm">{tlink(an, f.t)}</span><div class="dt">{e(f.detail)}</div>'
                + (f'<details class="why"><summary>Was bedeutet das?</summary><p>{e(why)}</p></details>' if why else "")
                + '</li>')
    parts.append(f'<h2 id="befunde">Befunde <small>{len(main)} auff\u00e4llig \u00b7 {len(info)} Info</small></h2>')
    if main:
        parts.append('<ul class="findings">' + "".join(fitem(f) for f in main) + "</ul>")
    else:
        parts.append('<ul class="findings"><li class="f lvl-0"><span class="badge"><i style="background:var(--good)">\u2713</i>OK</span>'
                     '<div><span class="tt">Keine Auff\u00e4lligkeiten gefunden</span></div><span></span></li></ul>')
    if info:
        parts.append(f'<details class="info" open><summary>{len(info)} Info-Eintr\u00e4ge</summary><ul class="findings">'
                     + "".join(fitem(f) for f in info) + "</ul></details>")

    # Konfiguration
    parts.append('<h2 id="konfig">Konfiguration <small>aus den Sensorwerten abgeleitet' +
                 (" \u00b7 \u25CF ge\u00e4ndert gegen\u00fcber dem letzten Log" if an.fp_changes else "") + "</small></h2>")
    changed = {k for k, _, _ in an.fp_changes}
    parts.append('<div class="cfg">' + "".join(
        f'<div class="{"chg" if k in changed else ""}"><span>{e(k)}</span><span>{e(v)}</span></div>' for k, v in an.fp.items()) + "</div>")

    # Diagramme
    cd = build_chart_data(an)
    parts.append('<h2 id="diagramme">Diagramme <small>Zeiger zeigt Werte \u00b7 Ziehen zoomt \u00b7 Doppelklick zeigt alles'
                 + (" \u00b7 \u00dcbersicht verdichtet (Min/Max)" if cd["decimated"] else "") + "</small></h2>")
    parts.append('<section class="diag"><div class="toolbar"><button id="z-all" type="button">Gesamt</button><button id="z-10m" type="button">Letzte 10 min</button>'
                 '<button id="z-60s" type="button">Letzte 60 s</button><button id="z-out" type="button">Rauszoomen</button>'
                 '<span class="rng" id="rng"></span></div>')
    parts.append('<div class="grid" id="charts"></div>')
    parts.append('<p class="note">Gestrichelte senkrechte Linien markieren Befunde (Farbe = Schwere), graue Fl\u00e4chen L\u00fccken im Log. '
                 'Werte zum Nachschlagen stehen in den Tabellen.</p></section>')

    # Kennzahlen
    rows = an.stat_rows()
    parts.append(f'<h2 id="kennzahlen">Kennzahlen <small>\u201eunter Last\u201c = GPU \u2265 {an.cfg["lastgrenzen"]["gpu_prozent"]} % '
                 f'oder CPU \u2265 {an.cfg["lastgrenzen"]["cpu_prozent"]} %</small></h2>')
    parts.append('<div class="scroll"><table class="tbl"><thead><tr><th>Sensor</th><th>Min</th><th>\u00d8</th><th>Max</th>'
                 '<th>\u00d8 unter Last</th><th></th><th>Max um</th></tr></thead><tbody>')
    for lab, u, d, mn, av, mx, al, tmax in rows:
        parts.append(f"<tr><td>{e(lab)}</td><td>{fnum(mn, d)}</td><td>{fnum(av, d)}</td><td>{fnum(mx, d)}</td>"
                     f'<td>{fnum(al, d)}</td><td class="u">{e(u)}</td><td>{tlink(an, tmax)}</td></tr>')
    parts.append("</tbody></table></div>")

    # Letzte Minute
    if L.source == "LHM":
        end_note = "Dauerlog \u2013 Stand der letzten Aufzeichnung"
    else:
        end_note = "Log regul\u00e4r beendet" if L.footer else "Log ohne Abschluss \u2013 hier steht, was zuletzt gemessen wurde"

    def end_table(rows):
        out = ['<div class="scroll"><table class="tbl"><thead><tr><th>Zeit</th>'
               + "".join(f"<th>{e(lab)}</th>" for lab, _ in an.end_cols) + "</tr></thead><tbody>"]
        for tm, vals in rows:
            out.append(f"<tr><td>{e(L.clock(tm))}</td>" + "".join(f"<td>{fnum(v, d)}</td>" for v, (_, d) in zip(vals, an.end_cols)) + "</tr>")
        out.append("</tbody></table></div>")
        return "".join(out)
    for title, rows in an.crash_tables[:5]:
        parts.append(f'<h2 class="crash">{e(title)} <small>Werte unmittelbar vor dem Abbruch der Aufzeichnung</small></h2>')
        parts.append(end_table(rows))
    parts.append(f'<h2 id="logende">Letzte Minute vor Logende <small>{end_note}</small></h2>')
    if an.end_rows:
        parts.append(end_table(an.end_rows))

    # Ereignisse
    parts.append(f'<h2 id="ereignisse">Windows-Ereignisse <small>{e(an.events_note)}</small></h2>')
    if an.events:
        parts.append('<details class="guide"><summary>Spickzettel: h\u00e4ufige Ereignis-IDs</summary><table class="tbl mini"><tbody>'
                     + "".join(f"<tr><td>{e(a)}</td><td style='text-align:left;white-space:normal'>{e(b)}</td></tr>" for a, b in EVENT_GUIDE)
                     + "</tbody></table></details>")
        parts.append('<div class="scroll tall"><table class="tbl"><thead><tr><th>Bewertung</th><th>Zeit</th><th>Quelle</th><th>ID</th>'
                     '<th style="text-align:left">Meldung</th></tr></thead><tbody>')
        rows_ev = []
        for ev in an.events:
            key = (ev.get("t", "")[:19], ev.get("p"), ev.get("id"), ev.get("msg"), ev.get("_lvl"))
            if rows_ev and rows_ev[-1][0] == key:
                rows_ev[-1][2] += 1
            else:
                rows_ev.append([key, ev, 1])
        for _, ev, cnt in rows_ev[:300]:
            lvl = ev.get("_lvl")
            lv = lvl if lvl is not None else -1
            when = ev["t"][11:19] + (" (nach Logende)" if ev.get("_after") else "") + (f" \u00b7 {cnt}\u00d7" if cnt > 1 else "")
            badge = f'<span class="lv l{max(lv, 0)}"><i></i>{LEVELS.get(lv, DASH) if lv >= 0 else DASH}</span>'
            hint = event_hint(ev)
            msg = (hint + " \u00b7 " if hint else "") + (ev.get("_extra") + " \u00b7 " if ev.get("_extra") else "") + (ev.get("msg") or "")
            parts.append(f"<tr><td>{badge}</td><td>{e(when)}</td><td>{e(ev.get('p', ''))}</td><td>{e(ev.get('id', ''))}</td>"
                         f'<td class="ev-msg">{e(msg[:400])}</td></tr>')
        parts.append("</tbody></table></div>")

    if an.dumps:
        parts.append('<details class="guide"><summary>Dateien in C:\\Windows\\LiveKernelReports</summary><table class="tbl mini"><tbody>'
                     + "".join(f"<tr><td>{t:%d.%m.%Y %H:%M:%S}</td><td style='text-align:left'>{e(d.get('f', ''))}</td>"
                               f"<td>{fnum(d.get('mb'), 1)} MB</td></tr>" for t, d in an.dumps[-30:])
                     + "</tbody></table></details>")

    # Alle Sensoren
    allc = [c for c in L.cols if c is not None]
    parts.append(f'<h2 id="sensoren">Alle Sensoren <small>{len(allc)} Spalten \u00b7 Sensorgruppen: {e(L.group_source)}</small></h2>')
    parts.append('<input class="search" id="sf" type="search" placeholder="Filtern, z. B. \u201eVDD\u201c oder \u201eDIMM\u201c" aria-label="Sensoren filtern">')
    parts.append('<div class="scroll tall"><table class="tbl" id="sensors"><thead><tr><th>Sensor</th><th>Gruppe</th><th>Min</th><th>\u00d8</th>'
                 '<th>Max</th><th>Erster</th><th>Letzter</th><th></th></tr></thead><tbody>')
    for c in allc:
        d = digits_for(c.unit)
        if c.kind == "bool":
            yes = sum(1 for x in c.vals if x == 1.0)
            vals = f'<td colspan="5" style="text-align:left">{"Ja: " + str(yes) + " Messpunkte" if yes else "immer Nein"}</td>'
        elif c.n_valid:
            vals = "".join(f"<td>{fnum(x, d)}</td>" for x in (c.vmin, c.vavg, c.vmax, c.vfirst, c.vlast))
        else:
            vals = '<td colspan="5" style="text-align:left;color:var(--muted)">keine Werte</td>'
        parts.append(f'<tr><td>{e(c.short)}</td><td class="g">{e(c.group)}</td>{vals}<td class="u">{e(c.unit)}</td></tr>')
    parts.append("</tbody></table></div>")
    parts.append(f'<footer>hwlog_check {VERSION} \u00b7 erstellt {dt.datetime.now():%d.%m.%Y %H:%M} \u00b7 {e(L.path)} '
                 f'\u00b7 {e(L.encoding)}, Trennzeichen \u201e{e(L.delimiter)}\u201c \u00b7 Grenzwerte sind Richtwerte und per Konfigurationsdatei anpassbar.</footer>')
    return page(f"HWiNFO-Log {name}", "\n".join(parts), cd)


# --------------------------------------------------------------------------
# Verlauf
# --------------------------------------------------------------------------
def write_file(path, text):
    """Schreibt ueber eine frisch angelegte Temp-Datei und tauscht sie dann ein.

    'x' scheitert, falls dort schon etwas liegt (auch ein untergeschobener Link); os.replace ersetzt
    nur den Verzeichniseintrag - ein Symlink oder Hardlink am Ziel wird ueberschrieben, nicht sein Ziel.
    """
    tmp = f"{path}.{os.getpid()}.tmp"
    with open(tmp, "x", encoding="utf-8") as f:
        f.write(text)
    os.replace(tmp, path)


def sanitize_entry(x):
    """Verlaufseintrag pruefen: Die Datei ist von Hand editierbar, also nichts ungeprueft weiterverwenden."""
    if not isinstance(x, dict) or not isinstance(x.get("start"), str):
        return None
    try:
        dt.datetime.fromisoformat(x["start"][:19])
    except ValueError:
        return None
    num = lambda v: v if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) else None
    k = x.get("kpi") if isinstance(x.get("kpi"), dict) else {}
    x["kpi"] = {str(key): num(v) for key, v in k.items()}
    fp = x.get("fp") if isinstance(x.get("fp"), dict) else {}
    x["fp"] = {str(key): str(v) for key, v in fp.items()}
    x["status"] = x.get("status") if x.get("status") in (0, 1, 2, 3) else 0
    x["kurz"] = [str(t) for t in x.get("kurz", [])] if isinstance(x.get("kurz"), list) else []
    x["dauer_s"] = num(x.get("dauer_s")) or 0
    x["quelle"] = "LHM" if x.get("quelle") == "LHM" else "HWiNFO"
    x["groesse"] = num(x.get("groesse"))
    for key in ("id", "datei", "bericht", "profil", "ende"):
        x[key] = str(x.get(key, ""))
    x["abschluss"] = bool(x.get("abschluss"))
    return x


def load_history(path):
    out = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if ln:
                    try:
                        x = json.loads(ln)
                    except ValueError:
                        continue
                    x = sanitize_entry(x)
                    if x:
                        out.append(x)
    return out


def save_history(path, entries):
    entries = sorted(entries, key=lambda x: x.get("start", ""))
    write_file(path, "".join(json.dumps(x, ensure_ascii=False) + "\n" for x in entries))


def clean_json(x):
    """NaN/Inf durch None ersetzen, damit Verlauf und Diagrammdaten g\u00fcltiges JSON bleiben."""
    if isinstance(x, float):
        return x if math.isfinite(x) else None
    if isinstance(x, dict):
        return {k: clean_json(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean_json(v) for v in x]
    return x


def history_entry(an: Analysis, report_name):
    L = an.log
    cnt = {str(k): sum(1 for f in an.F if f.level == k) for k in range(4)}
    return clean_json({
        "id": L.file_id, "datei": os.path.basename(L.path), "pfad": os.path.abspath(L.path), "bericht": report_name,
        "analysiert": dt.datetime.now().isoformat(timespec="seconds"), "start": L.t0.isoformat(timespec="seconds"),
        "ende": L.at(L.duration).isoformat(timespec="seconds"), "dauer_s": round(L.duration),
        "abschluss": L.footer, "quelle": L.source, "groesse": L.size, "status": an.worst, "anzahl": cnt, "profil": an.profile,
        "kurz": [f.title for f in an.F if f.level >= 2][:4], "fp": an.fp, "kpi": an.kpi, "version": VERSION,
    })


def previous_entry(history, an_start_iso, file_id, source="HWiNFO"):
    """Vorheriges Log derselben Quelle (HWiNFO und LHM messen z. B. die SoC-Spannung an verschiedenen Stellen)."""
    cands = [h for h in history if h.get("start", "") < an_start_iso and h.get("id") != file_id
             and h.get("quelle", "HWiNFO") == source]
    return max(cands, key=lambda h: h["start"]) if cands else None


def ende_text(x):
    k = x.get("kpi", {})
    n = k.get("abstuerze") or 0
    if n:
        return f"<b>{int(n)}\u00d7 Absturz</b>"
    if x.get("quelle") == "LHM":
        return "Dauerlog"
    return "regul\u00e4r" if x.get("abschluss") else "<b>abgebrochen</b>"


def whea_text(k):
    if k.get("whea") is not None:
        return k["whea"]
    if k.get("whea_ev") is not None:
        return f"{k['whea_ev']} (Ereignisse)"
    return DASH


def render_overview(entries, title="Verlauf", sysd=None):
    entries = sorted(entries, key=lambda x: x.get("start", ""), reverse=True)
    last = max((x.get("status", 0) for x in entries[:1]), default=0)
    sw = sysd["worst"] if sysd else 0
    parts = [f'<div class="top"><div><h1>{e(title)}</h1><div class="meta">{len(entries)} ausgewertete Logs</div></div>'
             + status_box(max(last, sw), "letztes Log und Systemzustand" if sysd else "letztes Log") + "</div>"]
    if sysd:
        parts.append(render_system(sysd))
    if len(entries) >= 2:
        parts.append('<h2 id="diagramme">Trends je Log <small>Ziehen zoomt \u00b7 Doppelklick zeigt alles</small></h2>'
                     '<section class="diag"><div class="toolbar"><button id="z-all" type="button">Gesamt</button><span class="rng" id="rng"></span></div>'
                     '<div class="grid" id="charts"></div></section>')
    parts.append('<h2>Sitzungen</h2><div class="scroll"><table class="tbl"><thead><tr><th>Status</th><th>Start</th><th>Quelle</th><th>Dauer</th>'
                 '<th>Ende</th><th>CPU max</th><th>GPU-Hotspot</th><th>RAM max</th><th>SSD max</th><th>12 V min</th><th>WHEA</th>'
                 '<th>PCIe-Fehler unter Last</th><th>Module</th><th>RAM-Takt</th><th>SoC</th><th style="text-align:left">Wichtigste Befunde</th></tr></thead><tbody>')
    for x in entries:
        k = x.get("kpi") if isinstance(x.get("kpi"), dict) else {}
        fp = x.get("fp") if isinstance(x.get("fp"), dict) else {}
        st = x.get("status", 0)
        st = st if st in (0, 1, 2, 3) else 0
        v12 = k.get("gpu12_min") if k.get("gpu12_min") is not None else k.get("v12_min")
        rep = str(x.get("bericht", ""))
        rep = rep if re.fullmatch(r"[\w.-]+_bericht\.html", rep) else ""
        link = f'<a href="{e(rep)}">{e(x.get("start", "")[:16].replace("T", " "))}</a>'
        parts.append(
            f'<tr><td><span class="lv l{st}"><i></i>{["OK", "Hinweis", "Warnung", "Kritisch"][st]}</span></td><td>{link}</td>'
            f'<td>{"LHM" if x.get("quelle") == "LHM" else "HWiNFO"}</td>'
            f'<td>{e(fdur(x.get("dauer_s", 0)))}</td><td>{ende_text(x)}</td>'
            f'<td>{fnum(k.get("cpu_max"), 1)}</td><td>{fnum(k.get("gpu_hot_max"), 1)}</td><td>{fnum(k.get("ram_max"), 1)}</td><td>{fnum(k.get("ssd_max"), 1)}</td>'
            f'<td>{fnum(v12, 3)}</td><td>{e(whea_text(k))}</td><td>{e(DASH if k.get("pcie_err") is None else k["pcie_err"])}</td>'
            f'<td>{e(DASH if k.get("dimms") is None else k["dimms"])}</td><td>{e(fp.get("RAM-Takt", DASH))}</td><td>{e(fp.get("SoC-Spannung", DASH))}</td>'
            f'<td class="ev-msg">{e(SEP.join(x.get("kurz", [])) or x.get("profil", ""))}</td></tr>')
    parts.append("</tbody></table></div>")
    parts.append(f"<footer>hwlog_check {VERSION} \u00b7 erstellt {dt.datetime.now():%d.%m.%Y %H:%M}</footer>")
    data = None
    if len(entries) >= 2:
        es = sorted(entries, key=lambda x: x["start"])
        t0 = dt.datetime.fromisoformat(es[0]["start"])
        X = [round((dt.datetime.fromisoformat(x["start"]) - t0).total_seconds(), 1) for x in es]
        # gleiche Startzeiten auseinanderziehen
        for i in range(1, len(X)):
            if X[i] <= X[i - 1]:
                X[i] = X[i - 1] + 1

        def ser(key, src="kpi"):
            return [x.get(src, {}).get(key) for x in es]
        charts = [
            {"id": "t", "title": "Temperaturen (Maximum je Log)", "unit": "\u00b0C", "d": 1, "h": 200, "step": False, "logy": False, "inc": [], "bands": [], "floor0": False,
             "series": [{"name": "CPU", "c": 0, "y": ser("cpu_max")}, {"name": "GPU-Hotspot", "c": 1, "y": ser("gpu_hot_max")},
                        {"name": "RAM", "c": 2, "y": ser("ram_max")}, {"name": "SSD", "c": 3, "y": ser("ssd_max")}]},
            {"id": "v", "title": "12 V (Minimum je Log)", "unit": "V", "d": 3, "h": 200, "step": False, "logy": False, "inc": [], "bands": [], "floor0": False,
             "series": [{"name": "GPU 12 V", "c": 0, "y": ser("gpu12_min")}, {"name": "Board +12 V", "c": 1, "y": ser("v12_min")}]},
            {"id": "e", "title": "Fehlerz\u00e4hler je Log", "unit": "", "d": 0, "h": 170, "step": False, "logy": False, "inc": [0, 1], "bands": [], "floor0": True,
             "series": [{"name": "WHEA", "c": 7, "y": ser("whea")}, {"name": "PCIe-Fehler unter Last", "c": 1, "y": ser("pcie_err")},
                        {"name": "L\u00fccken", "c": 3, "y": ser("gaps")}]},
            {"id": "s", "title": "Status je Log (0 = OK \u2026 3 = Kritisch)", "unit": "", "d": 0, "h": 170, "step": False, "logy": False, "inc": [0, 3], "bands": [], "floor0": True,
             "series": [{"name": "Status", "c": 6, "y": [x.get("status", 0) for x in es]}]},
        ]
        chans = sorted({c for x in es for c in ((x.get("kpi") or {}).get("dimm") or {})})
        dser = lambda c, key: [(((x.get("kpi") or {}).get("dimm") or {}).get(c) or {}).get(key) for x in es]
        dname = lambda c: f"Kanal {c}" if len(c) == 1 else c
        if chans:
            charts[2:2] = [
                {"id": "rv", "title": "RAM-Eingangsspannung VIN je Kanal (Minimum je Log)", "unit": "V", "d": 3, "h": 200, "step": False,
                 "logy": False, "inc": [], "bands": [], "floor0": False,
                 "series": [{"name": dname(c), "c": i, "y": dser(c, "vin_min")} for i, c in enumerate(chans)]},
                {"id": "rd", "title": "RAM-Modulspannung VDD je Kanal (\u00d8 je Log)", "unit": "V", "d": 3, "h": 200, "step": False,
                 "logy": False, "inc": [], "bands": [], "floor0": False,
                 "series": [{"name": dname(c), "c": i, "y": dser(c, "vdd")} for i, c in enumerate(chans)]},
                {"id": "rt", "title": "RAM-Temperatur je Kanal (Maximum je Log)", "unit": "\u00b0C", "d": 1, "h": 200, "step": False,
                 "logy": False, "inc": [], "bands": [], "floor0": False,
                 "series": [{"name": dname(c), "c": i, "y": dser(c, "temp_max")} for i, c in enumerate(chans)]},
                {"id": "rg", "title": "Arbeitsspeicher nutzbar (je Log)", "unit": "GB", "d": 1, "h": 170, "step": False,
                 "logy": False, "inc": [0], "bands": [], "floor0": True, "series": [{"name": "RAM nutzbar", "c": 2, "y": ser("ram_gb")}]},
            ]
        for ch in charts:
            ch["series"] = [sr for sr in ch["series"] if any(v is not None for v in sr["y"])]
        charts = [c for c in charts if c["series"]]
        t0ms = int((t0 - dt.datetime(1970, 1, 1)).total_seconds() * 1000)
        data = {"x": list(range(len(es))), "t0ms": t0ms, "multiday": True, "charts": charts, "markers": [], "gaps": [],
                "xlabels": [x["start"][8:10] + "." + x["start"][5:7] + ". " + x["start"][11:16] for x in es], "dots": True}
    return page(title, "\n".join(parts), data)


# --------------------------------------------------------------------------
# Systemzustand (pro Aufruf, unabhaengig von den Logs)
# Ereignis-Chronik, Systemstand (BIOS, Microcode, Treiber), Speicherabbilder, Geraete mit Fehler, SMART-Zaehler
# --------------------------------------------------------------------------
PS_SYSINFO = r"""
$ErrorActionPreference = 'SilentlyContinue'
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ci = [Globalization.CultureInfo]::InvariantCulture
$r = [ordered]@{}
$b = Get-CimInstance Win32_BIOS
$r.bios = [string]$b.SMBIOSBIOSVersion
if ($b.ReleaseDate) { $r.bios_datum = $b.ReleaseDate.ToString('yyyy-MM-dd', $ci) }
$bb = Get-CimInstance Win32_BaseBoard
$r.board = ((([string]$bb.Manufacturer) + ' ' + ([string]$bb.Product)).Trim())
$p = Get-CimInstance Win32_Processor | Select-Object -First 1
$r.cpu = ([string]$p.Name).Trim()
try {
  $u = (Get-ItemProperty -LiteralPath 'HKLM:\HARDWARE\DESCRIPTION\System\CentralProcessor\0' -ErrorAction Stop).'Update Revision'
  if ($u) { $r.microcode_raw = (($u | ForEach-Object { '{0:X2}' -f $_ }) -join '') }
} catch {}
$r.gpus = @(Get-CimInstance Win32_VideoController | ForEach-Object { [pscustomobject]@{ name = [string]$_.Name; treiber = [string]$_.DriverVersion } })
$cv = Get-ItemProperty -LiteralPath 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion'
$r.win_build = [string]$cv.CurrentBuild; $r.win_ubr = [string]$cv.UBR; $r.win_version = [string]$cv.DisplayVersion
$os = Get-CimInstance Win32_OperatingSystem
if ($os.LastBootUpTime) { $r.boot = $os.LastBootUpTime.ToString('s', $ci) }
$pw = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Power'
if ($null -ne $pw.FwPOSTTime) { $r.post_ms = [int64]$pw.FwPOSTTime }
$cc = Get-ItemProperty -LiteralPath 'HKLM:\SYSTEM\CurrentControlSet\Control\CrashControl'
if ($null -ne $cc.CrashDumpEnabled) { $r.dump_modus = [int]$cc.CrashDumpEnabled }
if ($null -ne $cc.AutoReboot) { $r.auto_neustart = [int]$cc.AutoReboot }
$cs = Get-CimInstance Win32_ComputerSystem
$r.ram_nutzbar_gb = [math]::Round([double]$cs.TotalPhysicalMemory / 1GB, 1)
$r.ram = @(Get-CimInstance Win32_PhysicalMemory | ForEach-Object { [pscustomobject]@{
  slot = ([string]$_.DeviceLocator).Trim(); bank = ([string]$_.BankLabel).Trim(); gb = [math]::Round([double]$_.Capacity / 1GB, 1)
  sn = ([string]$_.SerialNumber).Trim(); pn = ([string]$_.PartNumber).Trim(); mts = [int]$_.ConfiguredClockSpeed } })
$r.pagefile_auto = [bool]$cs.AutomaticManagedPagefile
$r.pagefiles = @(Get-CimInstance Win32_PageFileUsage | ForEach-Object { [string]$_.Name })
$dumps = New-Object System.Collections.ArrayList
$r.dumpok = $true
try {
  if (Test-Path -LiteralPath '__WINDIR__\Minidump') {
    Get-ChildItem -LiteralPath '__WINDIR__\Minidump' -File -Filter '*.dmp' -ErrorAction Stop | ForEach-Object {
      [void]$dumps.Add([pscustomobject]@{ f = $_.Name; t = $_.LastWriteTime.ToString('s', $ci); kb = [math]::Round($_.Length / 1KB) }) }
  }
  $m = Get-Item -LiteralPath '__WINDIR__\MEMORY.DMP' -ErrorAction SilentlyContinue
  if ($m) { [void]$dumps.Add([pscustomobject]@{ f = 'MEMORY.DMP'; t = $m.LastWriteTime.ToString('s', $ci); kb = [math]::Round($m.Length / 1KB) }) }
} catch { $r.dumpok = $false }
$r.minidumps = @($dumps)
$r.geraete = @(Get-CimInstance Win32_PnPEntity -Filter 'ConfigManagerErrorCode <> 0' | Where-Object { $_.ConfigManagerErrorCode -notin 22, 45 } |
  ForEach-Object { [pscustomobject]@{ name = [string]$_.Name; code = [int]$_.ConfigManagerErrorCode; klasse = [string]$_.PNPClass } })
ConvertTo-Json -InputObject $r -Depth 4 -Compress
"""

DUMP_MODES = {0: "keine", 1: "vollständig", 2: "Kernel", 3: "klein (Minidump)", 7: "automatisch"}
DEVICE_CODES = {10: "kann nicht starten", 28: "kein Treiber installiert", 31: "Treiber lässt sich nicht laden",
                39: "Treiber beschädigt oder fehlt", 43: "Gerät hat ein Problem gemeldet"}
SMART_ATA = {5: "realloc", 187: "unkorrigierbar", 197: "ausstehend", 198: "offline_unkorr", 199: "crc",
             174: "unsicher_aus", 192: "unsicher_aus", 12: "einschaltvorgaenge", 9: "betriebsstunden"}
SMART_BAD = (("medienfehler", "Medienfehler"), ("realloc", "ersetzte Sektoren"), ("unkorrigierbar", "unkorrigierbare Fehler"),
             ("ausstehend", "ausstehende Sektoren"), ("offline_unkorr", "Offline-unkorrigierbare Sektoren"))
SYS_KEYS = (("bios", "BIOS"), ("board", "Mainboard"), ("cpu", "CPU"), ("microcode", "Microcode"),
            ("gpu_treiber", "Grafiktreiber"), ("windows", "Windows"), ("ram_takt", "RAM-Takt"))
MARKER_FILE = "markierungen.jsonl"


def _run_ps(script, timeout=120):
    if os.name != "nt":
        return None, "nur unter Windows möglich"
    enc = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    try:
        r = subprocess.run([powershell_path(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                            "-EncodedCommand", enc], capture_output=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError) as ex:
        return None, f"PowerShell nicht ausführbar ({ex})"
    out = r.stdout.decode("utf-8", errors="replace").strip().lstrip("﻿")
    if not out:
        return None, (r.stderr.decode("utf-8", errors="replace").strip()[:200] or f"Exitcode {r.returncode}")
    try:
        return json.loads(out), None
    except ValueError:
        return None, "Ausgabe von PowerShell nicht lesbar"


def fetch_sysinfo():
    windir = os.path.dirname(system_dir()).replace("'", "''")
    data, err = _run_ps(PS_SYSINFO.replace("__WINDIR__", windir))
    return (data if isinstance(data, dict) else None), err


def _as_list(x):
    if isinstance(x, list):
        return x
    return [x] if x not in (None, "") else []


def _microcode(raw):
    try:
        b = bytes.fromhex(str(raw))
    except ValueError:
        return ""
    if len(b) < 4:
        return ""
    # AMD liefert 4 Byte (z. B. 0C 12 60 0A = A60120C), Intel 8 Byte mit der Revision im oberen Teil
    lo = int.from_bytes(b[0:4], "little")
    hi = int.from_bytes(b[4:8], "little") if len(b) >= 8 else 0
    v = hi or lo
    return f"{v:X}" if v else ""


def _ram_channel(m):
    """Kanal aus BankLabel/DeviceLocator: 'P0 CHANNEL B', 'DIMM_B2', 'Channel A-DIMM1' \u2026"""
    for txt in (m.get("bank"), m.get("slot")):
        t = str(txt or "")
        g = re.search(r"CHANNEL\s*([A-H])\b", t, re.I) or re.search(r"(?:DIMM|CH)[\s_-]*([A-H])\s*\d", t, re.I) \
            or re.search(r"\b([A-H])[12]\b", t)
        if g:
            return g.group(1).upper()
    return ""


def _ram_label(m):
    return f"Kanal {m['kanal']} ({m.get('slot') or m.get('bank')})" if m.get("kanal") else str(m.get("slot") or m.get("bank") or "?")


def ram_findings(snap, prev, cfg, snaps):
    """RAM-Bestand laut Windows (SMBIOS, nach dem Einmessen beim Start) \u2013 unabh\u00e4ngig davon, ob ein Log lief."""
    F = []
    mods = snap.get("ram")
    if mods is None:
        return F
    exp_n = int(cfg.get("erwartete_riegel") or 0)
    exp_gb = float(cfg.get("ram_gb_erwartet") or 0) or max([float(x.get("ram_gb") or 0) for x in snaps] + [0.0])
    have = {m.get("kanal") for m in mods if m.get("kanal")}
    prev_mods = (prev or {}).get("ram") or []
    # nur bei weniger Modulen als zuvor \u2013 umgesteckte Module sind kein Ausfall
    gone = [m for m in prev_mods if (m.get("slot"), m.get("bank")) not in {(x.get("slot"), x.get("bank")) for x in mods}] \
        if len(mods) < len(prev_mods) else []
    missing_ch = sorted({m.get("kanal") for m in gone if m.get("kanal")} - have)
    lost = (exp_n and len(mods) < exp_n) or (exp_gb and snap.get("ram_gb", 0) < exp_gb * 0.85) or gone
    if lost:
        what = f"{len(mods)} von {exp_n or len(prev_mods)} Modulen, {fnum(snap.get('ram_gb'), 0)} GB" + \
            (f" statt {fnum(exp_gb, 0)} GB" if exp_gb else "")
        F.append((3, "RAM", (f"Speicherkanal {', '.join(missing_ch)} fehlt" if missing_ch else "RAM-Modul fehlt") + f" ({what})",
                  "Laut Windows " + (f"fehlt seit dem letzten Aufruf: {', '.join(_ram_label(m) for m in gone)}. " if gone else "")
                  + "Windows sieht nur die Module, die das BIOS beim Start erfolgreich eingemessen hat. Fehlt ein Kanal, ist er "
                  "ausgefallen oder nicht trainiert \u2013 auch wenn der PC startet. Steckpl\u00e4tze nicht umstecken, bevor der "
                  "Zustand dokumentiert ist (Foto BIOS-Speicherseite, Markierung setzen)."))
    # Module umgesteckt? Seriennummer je Steckplatz vergleichen
    old = {m.get("sn"): m for m in prev_mods if m.get("sn")}
    # Ort = Steckplatz + Bank: Gigabyte nennt beide Steckpl\u00e4tze "DIMM 1", der Kanal steht nur im BankLabel
    loc = lambda m: (m.get("slot"), m.get("bank"))
    moved = [(old[m["sn"]], m) for m in mods if m.get("sn") in old and loc(old[m["sn"]]) != loc(m)]
    if moved:
        F.append((1, "RAM", "RAM-Module umgesteckt",
                  "; ".join(f"Modul {m['sn']}: {_ram_label(o)} \u2192 {_ram_label(m)}" for o, m in moved)
                  + ". Ab jetzt zeigt der Verlauf, ob ein Fehler dem Modul oder dem Steckplatz folgt."))
    new_sn = [m for m in mods if m.get("sn") and prev_mods and m["sn"] not in old]
    if new_sn:
        F.append((1, "RAM", "Anderes RAM-Modul eingesetzt",
                  "; ".join(f"{_ram_label(m)}: {m.get('pn', '')} Seriennummer {m['sn']}" for m in new_sn)))
    return F


def _driver_label(name, ver):
    """NVIDIA meldet z. B. 32.0.15.9192 -> Treiberversion 591.92."""
    if "nvidia" in name.lower():
        digits = "".join(ver.split(".")[-2:])
        if len(digits) >= 5 and digits[-5:].isdigit():
            return f"{digits[-5:-2]}.{digits[-2:]}"
    return ver


def smartctl_path(cfg):
    p = str(cfg.get("smartctl_pfad") or "")
    if p:
        return p if os.path.isfile(p) else ""
    if os.name == "nt":
        # fester Pfad statt PATH-Suche: das Tool laeuft ggf. mit Adminrechten
        cand = os.path.join(system_dir()[:3], "Program Files", "smartmontools", "bin", "smartctl.exe")
        return cand if os.path.isfile(cand) else ""
    import shutil
    return shutil.which("smartctl") or ""


def parse_smart(j):
    if not isinstance(j, dict) or not (j.get("model_name") or j.get("serial_number")):
        return None
    d = {"modell": str(j.get("model_name", "")).strip(), "serie": str(j.get("serial_number", "")).strip(),
         "ok": (j.get("smart_status") or {}).get("passed")}
    isnumber = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)
    nv = j.get("nvme_smart_health_information_log")
    if isinstance(nv, dict):
        d["typ"] = "NVMe"
        for src, k in (("critical_warning", "krit_warnung"), ("media_errors", "medienfehler"),
                       ("num_err_log_entries", "fehlerlog"), ("unsafe_shutdowns", "unsicher_aus"),
                       ("power_cycles", "einschaltvorgaenge"), ("power_on_hours", "betriebsstunden"),
                       ("temperature", "temp"), ("percentage_used", "verbraucht"), ("available_spare", "reserve")):
            if isnumber(nv.get(src)):
                d[k] = nv[src]
    else:
        d["typ"] = "SATA"
        for a in ((j.get("ata_smart_attributes") or {}).get("table") or []):
            if not isinstance(a, dict):
                continue
            k = SMART_ATA.get(a.get("id"))
            raw = (a.get("raw") or {}).get("value")
            if k and isnumber(raw) and k not in d:
                d[k] = raw
        t = (j.get("temperature") or {}).get("current")
        if isnumber(t):
            d["temp"] = t
    return d


def fetch_smart(cfg):
    exe = smartctl_path(cfg)
    if not exe:
        return None, "smartctl nicht gefunden – für Fehlerzähler der SSDs smartmontools installieren (kostenlos)"

    def run(args):
        r = subprocess.run([exe] + args, capture_output=True, timeout=60,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return json.loads(r.stdout.decode("utf-8", errors="replace") or "{}")
    try:
        scan = run(["--scan-open", "-j"])
    except (OSError, subprocess.SubprocessError, ValueError) as ex:
        return None, f"smartctl nicht ausführbar ({ex})"
    out = []
    for dev in (scan.get("devices") or []) if isinstance(scan, dict) else []:
        name, typ = dev.get("name"), dev.get("type")
        if not name:
            continue
        try:
            d = parse_smart(run(["-a", "-j"] + (["-d", typ] if typ else []) + [name]))
        except (OSError, subprocess.SubprocessError, ValueError):
            continue
        if d:
            out.append(d)
    if not out:
        return None, "smartctl fand keine Laufwerke (Adminrechte nötig – mit --admin starten)"
    return out, None


def system_snapshot(raw, smart):
    snap = {"zeit": dt.datetime.now().isoformat(timespec="seconds"), "host": socket.gethostname()}
    if raw:
        bios = str(raw.get("bios") or "").strip()
        if bios:
            snap["bios"] = bios + (f" ({raw['bios_datum']})" if raw.get("bios_datum") else "")
        for k in ("board", "cpu"):
            if raw.get(k):
                snap[k] = str(raw[k]).strip()
        mc = _microcode(raw.get("microcode_raw") or "")
        if mc:
            snap["microcode"] = mc
        gpus = [g for g in _as_list(raw.get("gpus")) if isinstance(g, dict) and g.get("name")]
        if gpus:
            snap["gpu_treiber"] = "; ".join(f"{g['name']} {_driver_label(str(g['name']), str(g.get('treiber', '')))}".strip()
                                            for g in gpus)
        if raw.get("win_build"):
            snap["windows"] = (f"{raw.get('win_version', '')} " if raw.get("win_version") else "") + \
                f"Build {raw['win_build']}" + (f".{raw['win_ubr']}" if raw.get("win_ubr") else "")
        if raw.get("boot"):
            snap["boot"] = str(raw["boot"])[:19]
        if isinstance(raw.get("post_ms"), (int, float)) and raw["post_ms"] > 0:
            snap["post_s"] = round(raw["post_ms"] / 1000.0, 1)
        if isinstance(raw.get("dump_modus"), int):
            snap["dump_modus"] = raw["dump_modus"]
        if isinstance(raw.get("auto_neustart"), int):
            snap["auto_neustart"] = raw["auto_neustart"]
        snap["pagefile"] = "automatisch" if raw.get("pagefile_auto") else \
            ", ".join(str(x) for x in _as_list(raw.get("pagefiles"))) or "keine"
        mods = [m for m in _as_list(raw.get("ram")) if isinstance(m, dict) and (m.get("slot") or m.get("bank"))]
        if mods:
            snap["ram"] = [{"slot": str(m.get("slot") or ""), "bank": str(m.get("bank") or ""), "kanal": _ram_channel(m),
                            "gb": m.get("gb"), "sn": str(m.get("sn") or ""), "pn": str(m.get("pn") or ""), "mts": m.get("mts")}
                           for m in mods]
            snap["ram_gb"] = round(sum(float(m.get("gb") or 0) for m in mods), 1)
            mts = sorted({int(m["mts"]) for m in mods if isinstance(m.get("mts"), (int, float)) and m["mts"] > 0})
            if mts:
                snap["ram_takt"] = " / ".join(f"{v} MT/s" for v in mts)
        if isinstance(raw.get("ram_nutzbar_gb"), (int, float)):
            snap["ram_nutzbar_gb"] = raw["ram_nutzbar_gb"]
        snap["minidumps"] = [d for d in _as_list(raw.get("minidumps")) if isinstance(d, dict)] if raw.get("dumpok") else None
        snap["geraete"] = [d for d in _as_list(raw.get("geraete")) if isinstance(d, dict) and d.get("name")]
    if smart is not None:
        snap["smart"] = smart
    return snap


def load_jsonl(path):
    out = []
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    x = json.loads(ln)
                except ValueError:
                    continue
                if isinstance(x, dict):
                    out.append(x)
    return out


def save_jsonl(path, entries):
    write_file(path, "".join(json.dumps(clean_json(x), ensure_ascii=False) + "\n" for x in entries))


def _ts(s):
    try:
        return dt.datetime.fromisoformat(str(s)[:19])
    except ValueError:
        return None


# -- Markierungen -------------------------------------------------------------
def marker_dir(cfg):
    """Fester Ort f\u00fcr Markierungen, unabh\u00e4ngig davon, welches Log gerade ausgewertet wird: der konfigurierte
    Ausgabeordner, sonst Dokumente\\hwlog_berichte (dort landen auch die LHM-Berichte)."""
    out = str(cfg.get("ausgabeordner") or "")
    return os.path.expandvars(os.path.expanduser(out)) if out else os.path.join(documents_dir(), "hwlog_berichte")


def marker_path(cfg):
    return os.path.join(marker_dir(cfg), MARKER_FILE)


def add_marker(text, cfg):
    text = " ".join(str(text).split())[:300] or "(ohne Text)"
    entry = {"t": dt.datetime.now().isoformat(timespec="seconds"), "text": text}
    os.makedirs(marker_dir(cfg), exist_ok=True)
    with open(marker_path(cfg), "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def load_markers(cfg, extra_dirs=()):
    """Markierungen aus dem festen Ort, dem aktuellen Ausgabeordner und (bis 1.8.0) neben dem Skript."""
    legacy = os.path.dirname(os.path.abspath(__file__))
    seen, out = set(), []
    for d in dict.fromkeys([marker_dir(cfg), *extra_dirs, legacy]):
        for m in load_jsonl(os.path.join(d, MARKER_FILE)):
            key = (m.get("t"), m.get("text"))
            if _ts(m.get("t")) and isinstance(m.get("text", ""), str) and key not in seen:
                seen.add(key)
                out.append(m)
    return sorted(out, key=lambda m: str(m["t"]))


# -- Ereignis-Chronik ------------------------------------------------------------
MEMDIAG = "Microsoft-Windows-MemoryDiagnostics-Results"
CHRONIK_KEEP = Analysis.CRASH_EV | Analysis.CLEAN_EV | Analysis.BOOT_EV | Analysis.SLEEP_EV


def _event_key(ev):
    return f"{ev.get('t')}|{ev.get('p')}|{ev.get('id')}|{str(ev.get('msg') or '')[:60]}"


def scan_events(since, cfg):
    """Ereignisprotokoll seit der letzten Auswertung, unabhaengig von Logs (Abstuerze vor der Anmeldung, Bootschleifen ...)."""
    now = dt.datetime.now()
    start = now - dt.timedelta(days=int(cfg["ereignisse"].get("chronik_tage", 14)))
    prev = _ts(since)
    if prev:
        start = max(start, prev - dt.timedelta(minutes=10))
    evs, err = fetch_events(start, now + dt.timedelta(minutes=1))
    if evs is None:
        return None, None, err
    dumps = fetch_events.dumps
    keep = []
    for ev in evs:
        lvl, label = classify_event(ev)
        ts = _ts(ev.get("t"))
        if ts is None:
            continue
        if ev.get("_lke") and dumps is not None:
            real = any(_ts(d.get("t")) and -60 <= (ts - _ts(d.get("t"))).total_seconds() <= 300 for d in dumps)
            if not real:
                continue  # nur erneut gemeldeter alter Bericht
        if (lvl is None or lvl < 1) and (ev.get("p"), ev.get("id")) not in CHRONIK_KEEP and ev.get("p") != MEMDIAG:
            continue
        keep.append({"t": str(ev["t"])[:19], "p": str(ev.get("p", "")), "id": ev.get("id"),
                     "lvl": lvl if lvl is not None else -1, "label": label, "extra": str(ev.get("_extra") or ""),
                     "msg": str(ev.get("msg") or "")[:300]})
    return keep, start, None


def crash_incidents(events):
    """Absturzmeldungen (Kernel-Power 41, EventLog 6008, BugCheck) eines Neustarts zu einem Vorfall zusammenfassen."""
    crash = sorted((ev for ev in events if (ev.get("p"), ev.get("id")) in Analysis.CRASH_EV
                    or str(ev.get("label", "")).startswith("Fehlerbericht: BlueScreen")), key=lambda x: x["t"])
    out = []
    for ev in crash:
        ts = _ts(ev["t"])
        if out and ts and (ts - out[-1]["_t"]).total_seconds() <= 600:
            out[-1]["ev"].append(ev)
        elif ts:
            out.append({"_t": ts, "ev": [ev]})
    res = []
    for g in out:
        info = list(dict.fromkeys(x.get("extra") or x.get("label") for x in g["ev"] if x.get("extra") or x.get("label")))
        codes = [m.group(0) for x in g["ev"] for m in [re.search(r"Bugcheck (0x[0-9A-F]+[^,;]*)", x.get("extra") or "")] if m]
        bsod = next((c for c in codes if not c.startswith("Bugcheck 0x0 ")), "")
        art = f"Bluescreen {bsod.replace('Bugcheck ', '')}" if bsod else "Harter Absturz/Reset ohne Bluescreen"
        m = re.match(r"Bugcheck 0x([0-9A-F]+)", bsod)
        res.append({"t": g["ev"][0]["t"], "art": art, "info": info, "code": int(m.group(1), 16) if m else None})
    return res


def crash_pattern(incidents, days=30):
    """Abst\u00fcrze der letzten Tage nach Art z\u00e4hlen. Wechselnde Codes plus Abst\u00fcrze ohne Code sprechen f\u00fcr Hardware:
    Ein fehlerhafter Treiber stirbt meist immer an derselben Stelle, kippende Bits treffen zuf\u00e4llig irgendwo."""
    now = dt.datetime.now()
    recent = [x for x in incidents if _ts(x["t"]) and (now - _ts(x["t"])).days < days]
    groups = {}
    for x in recent:
        code = x.get("code")
        key = bugcheck_name(code) if code is not None else "ohne Bluescreen (Hardreset/H\u00e4nger)"
        g = groups.setdefault(key, {"art": key, "n": 0, "letzter": x["t"], "speicher": code in MEM_BUGCHECKS})
        g["n"] += 1
        g["letzter"] = max(g["letzter"], x["t"])
    rows = sorted(groups.values(), key=lambda g: (-g["n"], g["art"]))
    return {"n": len(recent), "rows": rows, "tage": days,
            "hardware": len(recent) >= 3 and len(rows) >= 2, "speicher": any(g["speicher"] for g in rows)}


def boot_loops(events):
    boots = sorted(_ts(ev["t"]) for ev in events if (ev.get("p"), ev.get("id")) == ("Microsoft-Windows-Kernel-General", 12)
                   and _ts(ev.get("t")))
    loops, i = [], 0
    while i + 2 < len(boots):
        if (boots[i + 2] - boots[i]).total_seconds() <= 300:
            j = i + 2
            while j + 1 < len(boots) and (boots[j + 1] - boots[j]).total_seconds() <= 150:
                j += 1
            loops.append((boots[i], boots[j], j - i + 1))
            i = j + 1
        else:
            i += 1
    return loops


def stability(events, incidents, since):
    now = dt.datetime.now()
    last = _ts(incidents[-1]["t"]) if incidents else None
    boots_since = sum(1 for ev in events if (ev.get("p"), ev.get("id")) == ("Microsoft-Windows-Kernel-General", 12)
                      and _ts(ev["t"]) and (last is None or _ts(ev["t"]) > last + dt.timedelta(minutes=10)))
    return {"seit": since, "letzter": incidents[-1]["t"] if incidents else "",
            "tage": max(0.0, round((now - (last or _ts(since) or now)).total_seconds() / 86400, 1)),
            "n7": sum(1 for x in incidents if _ts(x["t"]) and (now - _ts(x["t"])).days < 7),
            "n30": sum(1 for x in incidents if _ts(x["t"]) and (now - _ts(x["t"])).days < 30),
            "starts": boots_since, "gesamt": len(incidents)}


# -- Befunde zum Systemzustand ------------------------------------------------------
def _smart_label(d):
    return f"{d.get('typ', '')} {d.get('modell', '')}".strip()


def system_findings(snap, prev, cfg, smart_err, new_incidents, new_loops, snaps=None, incidents=None, new_events=None):
    F = []
    add = lambda lvl, cat, title, detail="": F.append((lvl, cat, title, detail))
    prev = prev or {}
    for inc in new_incidents:
        add(3, "Absturz", f"{inc['art']} – erkannt beim Start am {_ts(inc['t']):%d.%m. %H:%M}",
            "; ".join(inc["info"])[:400] + ". Unabhängig davon, ob gerade ein Log lief.")
    for a, b, n in new_loops:
        add(2, "Absturz", f"{n} Systemstarts innerhalb von {fdur((b - a).total_seconds())} am {a:%d.%m. %H:%M}",
            "Mehrere Starts kurz hintereinander: Das System ist gleich nach dem Start wieder ausgegangen oder hat neu "
            "gestartet (Bootschleife).")
    F += ram_findings(snap, prev, cfg, snaps or [])
    for ev in new_events or []:
        if ev.get("p") == MEMDIAG:
            add(3 if ev.get("lvl") == 3 else 0, "RAM", f"{ev.get('label')} ({_ts(ev['t']):%d.%m. %H:%M})", ev.get("msg", "")[:300])
    pat = crash_pattern(incidents or [])
    if pat["hardware"]:
        add(2, "Absturz", f"{pat['n']} Abst\u00fcrze in {pat['tage']} Tagen mit {len(pat['rows'])} verschiedenen Arten",
            "; ".join(f"{g['art']} \u00d7{g['n']}" + (" (speichertypisch)" if g["speicher"] else "") for g in pat["rows"])
            + ". Wechselnde Codes und Abst\u00fcrze ohne Code sprechen eher f\u00fcr Hardware (RAM, Speichercontroller, "
            "Spannungsversorgung) als f\u00fcr einen einzelnen Treiber." + (" Mindestens ein Code ist typisch f\u00fcr kaputte "
            "Speicherinhalte." if pat["speicher"] else ""))
    ch = [(lab, prev[k], snap[k]) for k, lab in SYS_KEYS if k in snap and prev.get(k) and prev[k] != snap[k]]
    if ch:
        add(1, "Systemstand", "Systemstand geändert seit der letzten Auswertung",
            "; ".join(f"{lab}: {a} → {b}" for lab, a, b in ch))
    if snap.get("dump_modus") == 0:
        add(2, "Dumps", "Windows schreibt bei Bluescreens keine Speicherabbilder",
            "Einstellen unter Systemeigenschaften → Erweitert → Starten und Wiederherstellen → "
            "„Kleines Speicherabbild“ oder „Automatisches Speicherabbild“.")
    if snap.get("pagefile") == "keine":
        add(2, "Dumps", "Keine Auslagerungsdatei",
            "Ohne Auslagerungsdatei auf dem Systemlaufwerk kann Windows beim Bluescreen kein Speicherabbild schreiben.")
    dumps = snap.get("minidumps")
    if dumps is None and "minidumps" in snap:
        add(0, "Dumps", "Minidump-Ordner nicht lesbar", "Mit --admin starten, dann prüft das Tool neue Speicherabbilder.")
    elif dumps:
        since = _ts(prev.get("zeit")) or (dt.datetime.now() - dt.timedelta(days=int(cfg["ereignisse"].get("chronik_tage", 14))))
        new = [d for d in dumps if _ts(d.get("t")) and _ts(d["t"]) > since]
        if new:
            add(2, "Dumps", f"{len(new)} neue(s) Speicherabbild(er) seit {since:%d.%m. %H:%M}",
                "Windows hat bei einem Bluescreen ein Abbild geschrieben: "
                + ", ".join(f"{d.get('f', '')} ({_ts(d['t']):%d.%m. %H:%M})" for d in new[:6])
                + ". Auswerten z. B. mit WinDbg (!analyze -v) oder BlueScreenView.")
    devs = snap.get("geraete") or []
    if devs:
        gpu = any(re.search(r"Display|NVIDIA|GeForce|Radeon", f"{d.get('klasse', '')} {d.get('name', '')}", re.I) for d in devs)
        add(2 if gpu else 1, "Geräte", f"{len(devs)} Gerät(e) mit Fehler im Gerätemanager",
            "; ".join(f"{d.get('name')} (Code {d.get('code')}: {DEVICE_CODES.get(d.get('code'), 'Fehler')})" for d in devs[:8]))
    post = snap.get("post_s")
    lim = cfg.get("post_zeit_hinweis_s", 60)
    if isinstance(post, (int, float)) and lim and post > lim and snap.get("boot") != prev.get("boot"):
        add(1, "Start", f"Letzter Start: BIOS brauchte {fnum(post, 0)} s",
            f"Start am {_ts(snap.get('boot')) or dt.datetime.now():%d.%m. %H:%M}. So lange dauert meist ein neues Memory "
            "Training. Nach einer BIOS-Änderung ist das normal, ohne Änderung hat das Board den RAM neu einmessen "
            "müssen – ein Frühwarnzeichen bei RAM- oder Speichercontroller-Problemen.")
    old = {(d.get("serie") or d.get("modell")): d for d in (prev.get("smart") or []) if isinstance(d, dict)}
    for d in snap.get("smart") or []:
        lab, o = _smart_label(d), old.get(d.get("serie") or d.get("modell"), {})
        delta = lambda k: (d[k] - o[k]) if isinstance(d.get(k), (int, float)) and isinstance(o.get(k), (int, float)) else 0
        if d.get("ok") is False:
            add(3, "SMART", f"{lab}: SMART-Gesamtstatus FEHLGESCHLAGEN", "Sofort Datensicherung prüfen.")
        if d.get("krit_warnung"):
            add(3, "SMART", f"{lab}: kritische Warnung {d['krit_warnung']}",
                "Die NVMe meldet ein kritisches Problem (Reserve, Temperatur, Zuverlässigkeit oder Schreibschutz).")
        for k, txt in SMART_BAD:
            v = d.get(k)
            if isinstance(v, (int, float)) and v > 0:
                add(3 if delta(k) > 0 else 2, "SMART", f"{lab}: {int(v)} {txt}" + (f" (+{int(delta(k))} neu)" if delta(k) > 0 else ""),
                    "Das Laufwerk hat Fehler beim Lesen/Schreiben der Speicherzellen festgestellt. Datensicherung prüfen.")
        if delta("crc") > 0:
            add(1, "SMART", f"{lab}: +{int(delta('crc'))} Übertragungsfehler (CRC)",
                "Fehler auf dem Weg zwischen Laufwerk und Board – meist SATA-Kabel oder Anschluss.")
        if delta("unsicher_aus") > 0:
            add(1, "SMART", f"{lab}: +{int(delta('unsicher_aus'))} unsichere Abschaltung(en) seit der letzten Auswertung",
                "Zählt jeden harten Absturz, Stromausfall und jedes Ausschalten per Knopf. Passt die Zahl zu den "
                "Abstürzen in der Chronik, ist die harte Abschaltung unabhängig vom Ereignisprotokoll belegt.")
        if delta("fehlerlog") > 0:
            add(0, "SMART", f"{lab}: +{int(delta('fehlerlog'))} Einträge im Fehlerprotokoll",
                "Oft harmlos (z. B. abgelehnte Befehle des Treibers); nur zusammen mit Medienfehlern wichtig.")
    if smart_err:
        add(0, "SMART", "SMART-Fehlerzähler nicht erfasst", smart_err)
    return F


def run_system_check(out_dir, cfg, markers, events_enabled=True):
    """Einmal pro Aufruf: Systemstand, Ereignis-Chronik und SMART erfassen und dauerhaft im Ausgabeordner ablegen."""
    sys_path, ev_path = os.path.join(out_dir, "system.jsonl"), os.path.join(out_dir, "ereignisse.jsonl")
    snaps = load_jsonl(sys_path)
    prev = snaps[-1] if snaps else None
    raw, raw_err = fetch_sysinfo()
    smart, smart_err = fetch_smart(cfg) if (os.name == "nt" or cfg.get("smartctl_pfad")) else (None, "")
    store = [x for x in load_jsonl(ev_path) if _ts(x.get("t"))]
    new_events, start, ev_err = scan_events(prev.get("scan_bis") if prev else None, cfg) if events_enabled \
        else (None, None, "abgeschaltet")
    known = {_event_key(x) for x in store}
    incidents_before = {x["t"] for x in crash_incidents(store)}
    loops_before = {a for a, _, _ in boot_loops(store)}
    if new_events:
        store += [x for x in new_events if _event_key(x) not in known]
        store.sort(key=lambda x: x["t"])
    snap = system_snapshot(raw, smart)
    snap["scan_bis"] = snap["zeit"] if new_events is not None else (prev or {}).get("scan_bis", "")
    snap["chronik_seit"] = (prev or {}).get("chronik_seit") or (start.isoformat(timespec="seconds") if start else "")
    incidents = crash_incidents(store)
    new_inc = [x for x in incidents if x["t"] not in incidents_before] if prev else []
    new_loops = [x for x in boot_loops(store) if x[0] not in loops_before] if prev else []
    fresh = [x for x in new_events or [] if _event_key(x) not in known] if prev else []
    F = system_findings(snap, prev, cfg, smart_err, new_inc, new_loops, snaps + [snap], incidents, fresh)
    gathered = raw is not None or smart is not None or new_events is not None
    if gathered:
        save_jsonl(sys_path, snaps + [snap])
        save_jsonl(ev_path, store)
        snaps = snaps + [snap]
    notes = []
    if raw is None:
        notes.append(f"Systemstand: {raw_err}")
    if new_events is None:
        notes.append(f"Ereignisprotokoll: {ev_err}")
    return {"findings": F, "snaps": snaps, "events": store, "incidents": incidents, "loops": boot_loops(store),
            "markers": markers, "stab": stability(store, incidents, snap["chronik_seit"]) if snap["chronik_seit"] else None,
            "notes": notes, "worst": max([f[0] for f in F if f[0] > 0], default=0)}


def console_system(sysd, quiet=False):
    tag = {0: "INFO    ", 1: "HINWEIS ", 2: "WARNUNG ", 3: "KRITISCH"}
    st = sysd.get("stab")
    print("\nSystemzustand" + (f"  (Abstürze 7 Tage: {st['n7']}, 30 Tage: {st['n30']}, letzter: "
                                f"{(_ts(st['letzter']).strftime('%d.%m. %H:%M') if st['letzter'] else 'keiner')})" if st else ""))
    for lvl, cat, title, _ in sorted(sysd["findings"], key=lambda f: -f[0]):
        if lvl >= 1 or not quiet:
            print(f"  {tag[lvl]} {cat:<10} {title}")
    for n in sysd["notes"]:
        print(f"  {n}")


def _collapse_events(evs, gap_s=600):
    """Gleiche Ereignisse kurz hintereinander (z. B. zehn Grafiktreiber-Resets) zu einer Chronik-Zeile zusammenfassen."""
    norm = lambda v: re.sub(r"[\W_]+", "", str(v)).lower()
    groups = []
    for x in sorted(evs, key=lambda v: v["t"]):
        ts = _ts(x["t"])
        if ts is None:
            continue
        g = groups[-1] if groups else None
        if g and g["label"] == x.get("label", "") and g["lvl"] == x.get("lvl") and (ts - g["last"]).total_seconds() <= gap_s:
            g["n"] += 1
            g["last"] = ts
        else:
            groups.append({"t": x["t"], "first": ts, "last": ts, "n": 1, "lvl": x.get("lvl", 0),
                           "label": x.get("label", ""), "det": x.get("extra") or x.get("msg", "")})
    rows = []
    for g in groups:
        det = "" if norm(g["det"]) == norm(g["label"]) else str(g["det"])
        if g["n"] > 1:
            span = f"von {g['first']:%H:%M} bis {g['last']:%H:%M}"
            rows.append((g["t"], g["lvl"], f"{g['n']}\u00d7 {g['label']}", span + (f"; {det}" if det else "")))
        else:
            rows.append((g["t"], g["lvl"], g["label"], det))
    return rows


def render_system(sysd):
    """Abschnitte Stabilitaet, Systemzustand, Chronik, Systemstand und SMART fuer verlauf.html."""
    parts = []
    st = sysd.get("stab")
    fmt = lambda s, f="%d.%m.%Y %H:%M": _ts(s).strftime(f) if _ts(s) else DASH
    if st:
        parts.append('<h2 id="stabilitaet">Stabilität <small>aus dem Ereignisprotokoll, seit '
                     f'{e(fmt(st["seit"], "%d.%m.%Y"))}</small></h2><div class="tiles">')
        for k, v in (("Letzter Absturz", fmt(st["letzter"]) if st["letzter"] else "keiner"),
                     ("Tage ohne Absturz", fnum(st["tage"], 1)), ("Abstürze 7 / 30 Tage", f"{st['n7']} / {st['n30']}"),
                     ("Starts seit letztem Absturz", str(st["starts"]))):
            parts.append(f'<div class="tile"><div class="k">{e(k)}</div><div class="v">{e(v)}</div></div>')
        parts.append("</div>")
        pat = crash_pattern(sysd.get("incidents") or [], 3650)
        if pat["rows"]:
            parts.append('<table class="tbl mini"><thead><tr><th>Absturzart (gesamte Chronik)</th><th>Anzahl</th><th>zuletzt</th>'
                         '<th style="text-align:left">Hinweis</th></tr></thead><tbody>')
            for g in pat["rows"]:
                parts.append(f'<tr><td>{e(g["art"])}</td><td>{g["n"]}</td><td>{e(fmt(g["letzter"]))}</td>'
                             f'<td class="g">{"speichertypisch" if g["speicher"] else ""}</td></tr>')
            parts.append("</tbody></table>")
    F = sorted(sysd.get("findings") or [], key=lambda f: -f[0])
    if F or sysd.get("notes"):
        parts.append('<h2 id="system">Systemzustand <small>bei dieser Auswertung</small></h2><ul class="findings">')
        for lvl, cat, title, detail in F:
            parts.append(f'<li class="f lvl-{lvl}"><span class="badge"><i aria-hidden="true">{LEVEL_ICON[lvl]}</i>{LEVELS[lvl]}</span>'
                         f'<div><span class="tt">{e(title)}</span><span class="cat">{e(cat)}</span></div><span></span>'
                         f'<div class="dt">{e(detail)}</div></li>')
        parts.append("</ul>")
        for n in sysd.get("notes") or []:
            parts.append(f'<p class="note">{e(n)}</p>')
    # Chronik
    rows = [(x["t"], 3, x["art"], "; ".join(x["info"])) for x in sysd.get("incidents") or []]
    rows += [(a.isoformat(timespec="seconds"), 2, f"Bootschleife: {n} Starts", f"bis {b:%H:%M:%S}")
             for a, b, n in sysd.get("loops") or []]
    rows += _collapse_events([x for x in sysd.get("events") or [] if isinstance(x.get("lvl"), int)
                              and (x["lvl"] >= 2 or x.get("p") == MEMDIAG)
                              and (x.get("p"), x.get("id")) not in Analysis.CRASH_EV
                              and not str(x.get("label", "")).startswith("Fehlerbericht: Blue")])
    rows += [(m["t"], 0, "Eigene Markierung", m.get("text", "")) for m in sysd.get("markers") or []]
    if rows:
        rows.sort(key=lambda r: r[0], reverse=True)
        parts.append('<h2 id="chronik">Abstürze, Ereignisse und Markierungen <small>neueste zuerst</small></h2>'
                     '<div class="scroll tall"><table class="tbl"><thead><tr><th>Zeit</th><th>Bewertung</th>'
                     '<th style="text-align:left">Was</th><th style="text-align:left">Details</th></tr></thead><tbody>')
        for t, lvl, what, det in rows[:200]:
            lvl = lvl if lvl in (0, 1, 2, 3) else 0
            parts.append(f'<tr><td>{e(fmt(t))}</td><td><span class="lv l{lvl}"><i></i>{LEVELS[lvl]}</span></td>'
                         f'<td class="ev-msg">{e(what)}</td><td class="ev-msg">{e(str(det)[:300])}</td></tr>')
        parts.append("</tbody></table></div>")
    # Systemstand je Start
    snaps = [x for x in sysd.get("snaps") or [] if x.get("bios") or x.get("windows")]
    if snaps:
        keyf = lambda x: tuple(str(x.get(k, "")) for k in ("boot", "bios", "microcode", "gpu_treiber", "windows", "dump_modus"))
        uniq = []
        for x in snaps:
            if not uniq or keyf(uniq[-1]) != keyf(x):
                uniq.append(x)
        mc = any(x.get("microcode") for x in uniq)  # Windows liefert die Revision nicht auf jedem System
        parts.append('<h2 id="systemstand">Systemstand <small>je Systemstart · BIOS-Zeit = Dauer bis Windows startet '
                     '(Memory Training)</small></h2><div class="scroll"><table class="tbl"><thead><tr><th>Start</th>'
                     '<th>BIOS-Zeit</th><th style="text-align:left">BIOS</th>' + ('<th>Microcode</th>' if mc else '') +
                     '<th style="text-align:left">Grafiktreiber</th><th style="text-align:left">Windows</th><th>Dumps</th></tr></thead><tbody>')
        for x in reversed(uniq[-60:]):
            post = x.get("post_s")
            parts.append(f'<tr><td>{e(fmt(x.get("boot")))}</td><td>{e(fnum(post, 0) + " s" if isinstance(post, (int, float)) else DASH)}</td>'
                         f'<td class="ev-msg">{e(x.get("bios", DASH))}</td>' + (f'<td>{e(x.get("microcode") or DASH)}</td>' if mc else '') +
                         f'<td class="ev-msg">{e(x.get("gpu_treiber", DASH))}</td><td class="ev-msg">{e(x.get("windows", DASH))}</td>'
                         f'<td>{e(DUMP_MODES.get(x.get("dump_modus"), DASH))}</td></tr>')
        parts.append("</tbody></table></div>")
    # RAM je Steckplatz (Seriennummern) \u2013 zeigt, ob ein Fehler dem Modul oder dem Steckplatz folgt
    rsn = [x for x in sysd.get("snaps") or [] if x.get("ram")]
    if rsn:
        rkey = lambda x: (tuple(sorted((m.get("slot"), m.get("sn")) for m in x["ram"])), x.get("ram_takt"), x.get("boot"))
        ru = []
        for x in rsn:
            if not ru or rkey(ru[-1]) != rkey(x):
                ru.append(x)
        exp_n = len(max((x["ram"] for x in rsn), key=len))
        parts.append('<h2 id="ram">Arbeitsspeicher laut Windows <small>je Systemstart · Steckplatz und Seriennummer</small></h2>'
                     '<div class="scroll"><table class="tbl"><thead><tr><th>Start</th><th style="text-align:left">Belegung</th>'
                     '<th>verbaut</th><th>nutzbar</th><th>Takt</th></tr></thead><tbody>')
        for x in reversed(ru[-60:]):
            mods = sorted(x["ram"], key=lambda m: (m.get("kanal") or "", m.get("slot") or ""))
            lvl = 3 if len(mods) < exp_n else 0
            bel = "; ".join(f"{_ram_label(m)}: {fnum(m.get('gb'), 0)} GB" + (f" \u00b7 SN {m['sn']}" if m.get("sn") else "") for m in mods)
            parts.append(f'<tr><td>{e(fmt(x.get("boot") or x.get("zeit")))}</td><td class="ev-msg"><span class="lv l{lvl}"><i></i></span> {e(bel)}</td>'
                         f'<td>{e(fnum(x.get("ram_gb"), 0) + " GB")}</td><td>{e(fnum(x["ram_nutzbar_gb"], 1) + " GB" if isinstance(x.get("ram_nutzbar_gb"), (int, float)) else DASH)}</td>'
                         f'<td>{e(x.get("ram_takt") or DASH)}</td></tr>')
        parts.append("</tbody></table></div>")
    # SMART
    smart_snaps = [x for x in sysd.get("snaps") or [] if x.get("smart")]
    if smart_snaps:
        first = {(d.get("serie") or d.get("modell")): d for d in smart_snaps[0]["smart"] if isinstance(d, dict)}
        parts.append(f'<h2 id="smart">Laufwerke (SMART) <small>Stand {e(fmt(smart_snaps[-1].get("zeit")))} · Δ = seit '
                     f'{e(fmt(smart_snaps[0].get("zeit"), "%d.%m.%Y"))}</small></h2><div class="scroll"><table class="tbl"><thead><tr>'
                     '<th>Laufwerk</th><th>Status</th><th>°C</th><th>Verschleiß</th><th>Medienfehler / Sektoren</th>'
                     '<th>CRC</th><th>Unsichere Abschaltungen (Δ)</th><th>Einschaltvorgänge</th><th>Betriebsstunden</th></tr></thead><tbody>')
        num = lambda v, d=0: fnum(v, d) if isinstance(v, (int, float)) else DASH
        for d in smart_snaps[-1]["smart"]:
            if not isinstance(d, dict):
                continue
            o = first.get(d.get("serie") or d.get("modell"), {})
            bad = sum(int(d.get(k, 0) or 0) for k, _ in SMART_BAD if isinstance(d.get(k), (int, float)))
            us, us0 = d.get("unsicher_aus"), o.get("unsicher_aus")
            wear = f"{num(d.get('verbraucht'))} %" if isinstance(d.get("verbraucht"), (int, float)) else DASH
            ok = {True: "OK", False: "FEHLER"}.get(d.get("ok"), DASH)
            parts.append(f'<tr><td>{e(_smart_label(d))}</td><td>{e(ok)}</td><td>{num(d.get("temp"))}</td><td>{e(wear)}</td>'
                         f'<td>{bad}</td><td>{num(d.get("crc"))}</td>'
                         f'<td>{num(us)}' + (f' (+{int(us - us0)})' if isinstance(us, (int, float)) and isinstance(us0, (int, float)) and us > us0 else "")
                         + f'</td><td>{num(d.get("einschaltvorgaenge"))}</td><td>{num(d.get("betriebsstunden"))}</td></tr>')
        parts.append("</tbody></table></div>")
    return "\n".join(parts)


# --------------------------------------------------------------------------
# Konsole
# --------------------------------------------------------------------------
def console_summary(an: Analysis, report_path, quiet=False):
    L = an.log
    tag = {0: "OK      ", 1: "HINWEIS ", 2: "WARNUNG ", 3: "KRITISCH"}
    print(f"\n{os.path.basename(L.path)}  {L.t0:%d.%m.%Y %H:%M:%S}  {fdur(L.duration)}  ->  {tag[an.worst].strip()}")
    if not quiet:
        for f in an.F:
            if f.level >= 1:
                when = f" [{L.clock(f.t)}]" if f.t is not None else ""
                print(f"  {tag[f.level]} {f.cat:<10} {f.title}{when}")
    print(f"  Bericht: {report_path}")


# --------------------------------------------------------------------------
# Hauptprogramm
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Windows: mit Administratorrechten neu starten (fuer C:\Windows\LiveKernelReports)
# --------------------------------------------------------------------------
def is_admin():
    if os.name != "nt":
        return True
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        return False


def open_in_browser(path):
    webbrowser.open("file:///" + os.path.abspath(path).replace("\\", "/").lstrip("/"))


def run_elevated(a):
    """Startet das Skript per UAC-Abfrage als Administrator, wartet darauf und gibt dessen Ausgabe hier aus.

    Liefert den Exitcode oder None, wenn die Erhoehung abgelehnt wurde bzw. nicht moeglich ist.
    Der Browser wird anschliessend von diesem (nicht erhoehten) Prozess geoeffnet.
    """
    import ctypes
    import tempfile
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", ctypes.c_uint32), ("fMask", ctypes.c_uint32), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    fd, result_file = tempfile.mkstemp(prefix="hwlog_", suffix=".json")
    os.close(fd)
    args = [os.path.abspath(__file__)] + [os.path.abspath(p) for p in a.pfade]
    if a.ausgabe:
        args += ["-o", os.path.abspath(a.ausgabe)]
    if a.config:
        args += ["--config", os.path.abspath(a.config)]
    if a.riegel is not None:
        args += ["--riegel", str(a.riegel)]
    if a.aufraeumen is not None:
        args += ["--aufraeumen", str(a.aufraeumen)]
    for flag, on in (("--neu", a.neu), ("--keine-ereignisse", a.keine_ereignisse),
                     ("--kein-verlauf", a.kein_verlauf), ("--leise", a.leise)):
        if on:
            args.append(flag)
    args += ["--ergebnisdatei", result_file]

    sei = SHELLEXECUTEINFOW()
    sei.cbSize = ctypes.sizeof(sei)
    sei.fMask = 0x00000040  # SEE_MASK_NOCLOSEPROCESS: Prozess-Handle zum Warten behalten
    sei.lpVerb = "runas"
    sei.lpFile = sys.executable
    # -I: isolierter Modus - kein Skriptordner, keine PYTHON*-Variablen, keine Benutzer-Pakete im Suchpfad.
    # Sonst koennte eine untergeschobene json.py o. ae. neben dem Skript mit Adminrechten ausgefuehrt werden.
    sei.lpParameters = subprocess.list2cmdline(["-I"] + args)
    sei.lpDirectory = os.getcwd()
    sei.nShow = 7  # minimiert, ohne den Fokus zu stehlen
    print("Starte mit Administratorrechten (f\u00fcr C:\\Windows\\LiveKernelReports) \u2026")
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(sei)) or not sei.hProcess:
        err = ctypes.GetLastError()
        if os.path.exists(result_file):
            os.remove(result_file)
        print("Administratorrechte abgelehnt \u2013 Auswertung l\u00e4uft ohne Zugriff auf die Dump-Dateien."
              if err == 1223 else f"Rechteerh\u00f6hung nicht m\u00f6glich (Fehler {err}) \u2013 Auswertung l\u00e4uft normal.")
        return None
    ctypes.windll.kernel32.WaitForSingleObject(sei.hProcess, 0xFFFFFFFF)
    code = wintypes.DWORD()
    ctypes.windll.kernel32.GetExitCodeProcess(sei.hProcess, ctypes.byref(code))
    ctypes.windll.kernel32.CloseHandle(sei.hProcess)
    try:
        with open(result_file, encoding="utf-8") as f:
            res = json.load(f)
        os.remove(result_file)
    except (OSError, ValueError):
        res = {}
    print(res.get("ausgabe", "").rstrip() or "(keine Ausgabe vom Administrator-Prozess erhalten)")
    if a.oeffnen and res.get("oeffnen"):
        open_in_browser(res["oeffnen"])
    return int(code.value)


class _Tee:
    """Schreibt Konsolenausgabe zusaetzlich in einen Puffer (fuer die Rueckgabe an den nicht erhoehten Prozess)."""

    def __init__(self, stream, buf):
        self.stream, self.buf = stream, buf

    def write(self, text):
        self.buf.append(text)
        try:
            return self.stream.write(text)
        except (ValueError, OSError):
            return len(text)

    def flush(self):
        try:
            self.stream.flush()
        except (ValueError, OSError):
            pass


def documents_dir():
    """Dokumente-Ordner des Benutzers (beachtet Umleitungen, z. B. nach OneDrive)."""
    if os.name == "nt":
        try:
            import ctypes
            import uuid
            from ctypes import wintypes

            class GUID(ctypes.Structure):
                _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                            ("Data4", ctypes.c_ubyte * 8)]
            u = uuid.UUID("{FDD39AD0-238F-46AF-ADB4-6C85480369C7}")  # FOLDERID_Documents
            g = GUID(u.fields[0], u.fields[1], u.fields[2], (ctypes.c_ubyte * 8).from_buffer_copy(u.bytes[8:]))
            ptr = ctypes.c_wchar_p()
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(g), 0, None, ctypes.byref(ptr)) == 0:
                path = ptr.value
                ctypes.windll.ole32.CoTaskMemFree(ptr)
                if path:
                    return path
        except (AttributeError, OSError, ValueError):
            pass
    return os.path.join(os.path.expanduser("~"), "Documents")


def _long_path(path):
    """Windows-Kurznamen (RUNNER~1) in lange Namen umwandeln, sonst unver\u00e4ndert."""
    if os.name != "nt":
        return path
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(32768)
        n = ctypes.windll.kernel32.GetLongPathNameW(path, buf, len(buf))
        return buf.value if 0 < n < len(buf) else path
    except (AttributeError, OSError, ValueError):
        return path


def is_redirected(path):
    """True, wenn der Pfad \u00fcber einen Symlink oder eine Junction woanders hinf\u00fchrt. Kurznamen (8.3) und
    Gro\u00df-/Kleinschreibung z\u00e4hlen nicht als Umleitung."""
    a = os.path.abspath(path)
    return os.path.normcase(os.path.realpath(a)) != os.path.normcase(_long_path(a))


LHM_FILE_RX = re.compile(r"^LibreHardwareMonitorLog-(\d{4})-(\d{2})-(\d{2})(?:-\d+)?\.csv$", re.I)


def cleanup_lhm_logs(dirs, days):
    """Loescht LHM-Logdateien, deren Datum im Dateinamen aelter als `days` Tage ist.

    Nur Dateien mit exakt diesem Namensmuster, nur echte Dateien (keine Links) und nur direkt im angegebenen
    Ordner. Laeuft ggf. mit Adminrechten, deshalb bewusst eng gefasst.
    """
    cutoff = dt.date.today() - dt.timedelta(days=days)
    removed, failed = [], []
    for d in dirs:
        if is_redirected(d):
            print(f"Aufr\u00e4umen \u00fcbersprungen: {d} ist eine Verkn\u00fcpfung.", file=sys.stderr)
            continue
        try:
            entries = list(os.scandir(d))
        except OSError as ex:
            failed.append((d, ex))
            continue
        for entry in entries:
            m = LHM_FILE_RX.match(entry.name)
            if not m:
                continue
            try:
                if not entry.is_file(follow_symlinks=False) or entry.is_symlink():
                    continue
                day = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            except (OSError, ValueError):
                continue
            if day >= cutoff:
                continue
            try:
                os.remove(entry.path)
                removed.append(entry.name)
            except OSError as ex:
                failed.append((entry.name, ex))
    return removed, failed


def collect_files(paths):
    out = []
    for p in paths:
        if os.path.isdir(p):
            for fn in sorted(os.listdir(p)):
                if fn.lower().endswith(".csv"):
                    out.append(os.path.join(p, fn))
        elif os.path.isfile(p):
            out.append(p)
        else:
            print(f"Nicht gefunden: {p}", file=sys.stderr)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(
        prog="hwlog_check",
        description="Wertet HWiNFO- und LibreHardwareMonitor-Sensorlogs (CSV) aus und erzeugt HTML-Berichte.",
        epilog="Exitcode: 0 = unauff\u00e4llig, 1 = Hinweise, 2 = Warnung, 3 = kritisch, 4 = Fehler.")
    ap.add_argument("pfade", nargs="*", help="CSV-Dateien oder Ordner mit CSV-Dateien")
    ap.add_argument("-o", "--ausgabe", help="Ausgabeordner (Standard: Unterordner hwlog_berichte neben dem ersten Log)")
    ap.add_argument("--neu", action="store_true", help="nur Logs auswerten, die noch nicht im Verlauf stehen")
    ap.add_argument("--oeffnen", action="store_true", help="Bericht danach im Browser \u00f6ffnen")
    ap.add_argument("--config", help="JSON-Datei mit eigenen Grenzwerten")
    ap.add_argument("--config-schreiben", metavar="DATEI", help="Standard-Grenzwerte als JSON schreiben und beenden")
    ap.add_argument("--riegel", type=int, help="erwartete Anzahl RAM-Module (Standard 2)")
    ap.add_argument("--keine-ereignisse", action="store_true", help="Windows-Ereignisprotokoll nicht abfragen")
    ap.add_argument("--kein-verlauf", action="store_true", help="Verlauf nicht lesen/schreiben")
    ap.add_argument("-q", "--leise", action="store_true", help="nur Zusammenfassung je Log ausgeben")
    ap.add_argument("--aufraeumen", type=int, metavar="TAGE",
                    help="LHM-Tagesdateien (LibreHardwareMonitorLog-*.csv) in angegebenen Ordnern l\u00f6schen, "
                         "die \u00e4lter als TAGE sind (mindestens 2)")
    ap.add_argument("--admin", action="store_true",
                    help="unter Windows per UAC-Abfrage mit Administratorrechten laufen (Zugriff auf LiveKernelReports)")
    ap.add_argument("--markieren", metavar="TEXT",
                    help="eigene Beobachtung mit Uhrzeit festhalten (z. B. Bildaussetzer) und beenden")
    ap.add_argument("--ergebnisdatei", help=argparse.SUPPRESS)
    ap.add_argument("--version", action="version", version=f"hwlog_check {VERSION}")
    a = ap.parse_args(argv)

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(errors="replace")
        except (ValueError, OSError):
            pass

    if a.config_schreiben:
        with open(a.config_schreiben, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        print(f"Standard-Konfiguration geschrieben: {a.config_schreiben}")
        return 0

    if a.markieren is not None:
        try:
            m = add_marker(a.markieren, load_config(a))
        except (OSError, ValueError) as ex:
            print(f"Markierung nicht gespeichert: {ex}", file=sys.stderr)
            return 4
        print(f"Markierung gespeichert: {m['t'].replace('T', ' ')}  {m['text']}")
        return 0
    if a.admin and os.name == "nt" and not is_admin() and not a.ergebnisdatei and a.pfade:
        rc = run_elevated(a)
        if rc is not None:
            return rc
    out_buf = None
    if a.ergebnisdatei:
        out_buf = []
        sys.stdout = _Tee(sys.stdout, out_buf)
        sys.stderr = _Tee(sys.stderr, out_buf)
    res = {"oeffnen": None}
    try:
        return _main_run(a, ap, res)
    finally:
        if a.ergebnisdatei:
            sys.stdout, sys.stderr = sys.stdout.stream, sys.stderr.stream
            write_result_file(a.ergebnisdatei, {"ausgabe": "".join(out_buf), "oeffnen": res["oeffnen"]})


def write_result_file(path, data):
    """Rueckgabe an den nicht erhoehten Prozess. Die Datei hat dieser vorher leer angelegt; Links werden abgelehnt,
    damit der Admin-Prozess nicht ueber eine umgebogene Datei an anderer Stelle schreibt."""
    try:
        st = os.lstat(path)
        reparse = getattr(st, "st_file_attributes", 0) & 0x400  # FILE_ATTRIBUTE_REPARSE_POINT
        import stat as _stat
        if not _stat.S_ISREG(st.st_mode) or reparse or st.st_nlink != 1 or st.st_size != 0:
            return
        with open(path, "r+", encoding="utf-8") as f:
            f.write(json.dumps(data, ensure_ascii=False))
    except OSError:
        pass


def load_config(a):
    cfg = DEFAULT_CONFIG
    cfg_path = a.config
    if not cfg_path:
        here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hwlog_config.json")
        if os.path.exists(here):
            cfg_path = here
    if cfg_path:
        with open(cfg_path, encoding="utf-8") as f:
            cfg = deep_merge(DEFAULT_CONFIG, json.load(f))
    if a.riegel is not None:
        cfg = deep_merge(cfg, {"erwartete_riegel": a.riegel})
    return cfg


def _main_run(a, ap, res):
    target = None
    cfg = load_config(a)

    days = a.aufraeumen if a.aufraeumen is not None else int(cfg.get("lhm_aufbewahrung_tage") or 0)
    if days:
        dirs = [p for p in a.pfade if os.path.isdir(p)]
        if days < 2:
            print("Aufr\u00e4umen: mindestens 2 Tage angeben \u2013 nichts gel\u00f6scht.", file=sys.stderr)
        elif dirs:
            removed, failed = cleanup_lhm_logs(dirs, days)
            if removed:
                print(f"Aufger\u00e4umt: {len(removed)} LHM-Log(s) \u00e4lter als {days} Tage gel\u00f6scht.")
            for name, ex in failed[:5]:
                hint = " (Rechte fehlen \u2013 mit --admin starten)" if isinstance(ex, PermissionError) else ""
                print(f"Nicht gel\u00f6scht: {name}: {ex}{hint}", file=sys.stderr)

    files = collect_files(a.pfade)
    if not files:
        ap.print_help()
        return 4
    def _is_lhm(path):
        try:
            return log_identity(path)[2]
        except OSError:
            return False
    first_is_lhm = any(_is_lhm(p) for p in files)
    if a.ausgabe:
        out_dir = a.ausgabe
    elif cfg.get("ausgabeordner"):
        out_dir = os.path.expandvars(os.path.expanduser(str(cfg["ausgabeordner"])))
    elif first_is_lhm:
        # LHM-Logs liegen im Programmordner (nur Admins duerfen schreiben) -> Berichte in die Dokumente
        out_dir = os.path.join(documents_dir(), "hwlog_berichte")
    else:
        out_dir = os.path.join(os.path.dirname(os.path.abspath(files[0])), "hwlog_berichte")
    os.makedirs(out_dir, exist_ok=True)
    if os.name == "nt" and is_admin():
        # Mit Adminrechten nicht in umgeleitete Ordner schreiben (Junction/Symlink z. B. nach C:\Windows).
        if is_redirected(out_dir):
            print(f"Abbruch: Ausgabeordner {out_dir} ist eine Verkn\u00fcpfung auf {os.path.realpath(out_dir)}. "
                  "Mit Administratorrechten wird dorthin nicht geschrieben.", file=sys.stderr)
            return 4
    hist_path = None if a.kein_verlauf else os.path.join(out_dir, "verlauf.jsonl")
    if not cfg.get("ram_gb_erwartet"):
        # Sollwert f\u00fcr den nutzbaren RAM: gr\u00f6\u00dfter bisher von Windows gemeldeter Wert (nutzbar, nicht verbaut)
        seen = [float(x["ram_nutzbar_gb"]) for x in load_jsonl(os.path.join(out_dir, "system.jsonl"))
                if isinstance(x.get("ram_nutzbar_gb"), (int, float))]
        if seen:
            cfg = dict(cfg, _ram_gb_auto=max(seen))
    history = load_history(hist_path) if hist_path else []
    cache_path = os.path.join(out_dir, "sensor_schema.json")
    try:
        with open(cache_path, encoding="utf-8") as f:
            schema_cache = json.load(f)
        if not isinstance(schema_cache, dict):
            schema_cache = {}
        schema_cache = {k: v for k, v in schema_cache.items()
                        if isinstance(v, list) and all(isinstance(g, str) for g in v)}
    except (OSError, ValueError):
        schema_cache = {}

    # chronologisch: fruehere (meist sauber beendete) Logs fuellen den Sensorgruppen-Zwischenspeicher
    files = sorted(files, key=lambda p: os.path.getmtime(p))
    worst, last_report, done = 0, None, 0
    markers = load_markers(cfg, [out_dir])
    known = {h.get("id"): h.get("groesse") for h in history}
    for path in files:
        if a.neu and history:
            try:
                fid, size, is_lhm = log_identity(path)
            except OSError:
                continue
            # LHM-Tagesdateien wachsen: nur ueberspringen, wenn sich die Groesse nicht geaendert hat
            if fid in known and (not is_lhm or known[fid] == size):
                continue
        try:
            log = load_log(path, schema_cache)
        except Exception as ex:  # noqa: BLE001 - jede Datei einzeln, Fehler melden und weitermachen
            print(f"\n{os.path.basename(path)}: nicht auswertbar ({ex})", file=sys.stderr)
            worst = max(worst, 4)
            continue
        prev = previous_entry(history, log.t0.isoformat(timespec="seconds"), log.file_id, log.source) if history else None
        an = Analysis(log, cfg, prev_entry=prev, events_enabled=not a.keine_ereignisse, markers=markers).run()
        base = re.sub(r"[^\w.-]+", "_", os.path.splitext(os.path.basename(path))[0])
        rep_name = f"{base}_bericht.html"
        rep_path = os.path.join(out_dir, rep_name)
        write_file(rep_path, render_report(an, history_link=None if a.kein_verlauf else "verlauf.html"))
        if hist_path:
            history = [h for h in history if h.get("id") != log.file_id] + [history_entry(an, rep_name)]
        console_summary(an, rep_path, quiet=a.leise)
        worst = max(worst, an.worst)
        last_report = rep_path
        done += 1
    try:
        write_file(cache_path, json.dumps(schema_cache))
    except OSError:
        pass
    sysd = None
    if hist_path:
        sysd = run_system_check(out_dir, cfg, markers,
                                events_enabled=not a.keine_ereignisse and cfg["ereignisse"].get("aktiv", True))
        console_system(sysd, quiet=a.leise)
        worst = max(worst, sysd["worst"])
    if hist_path and done:
        save_history(hist_path, history)
    if hist_path:
        write_file(os.path.join(out_dir, "verlauf.html"), render_overview(history, sysd=sysd))
        print(f"\nVerlauf: {os.path.join(out_dir, 'verlauf.html')}")
    if not done:
        print("Keine neuen Logs ausgewertet.")
    if last_report:
        target = os.path.join(out_dir, "verlauf.html") if done > 1 and hist_path else last_report
    elif hist_path and os.path.exists(os.path.join(out_dir, "verlauf.html")):
        target = os.path.join(out_dir, "verlauf.html")  # nichts Neues: Verlauf zeigen
    res["oeffnen"] = target
    if a.oeffnen and target:
        if os.name == "nt" and is_admin():
            print("L\u00e4uft bereits mit Administratorrechten \u2013 Bericht wird nicht automatisch ge\u00f6ffnet, "
                  f"damit der Browser keine Adminrechte erbt. Bitte selbst \u00f6ffnen: {target}")
        else:
            open_in_browser(target)
    return worst


if __name__ == "__main__":
    sys.exit(main())
