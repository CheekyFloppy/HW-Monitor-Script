"""Gemeinsame Werkzeuge fuer die Tests: Testlogs erzeugen, Windows-Abfragen ersetzen, Ausgabe abfangen."""
import contextlib
import datetime as dt
import gzip
import io
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import hwlog_check as h  # noqa: E402

CPU = "CPU [#0]: AMD Ryzen 7 7800X3D"
BOARD = "GIGABYTE B850 AORUS ELITE WIFI7 (ITE IT8696E)"
GPU = "GPU [#0]: NVIDIA GeForce RTX 4080 SUPER"
SYS = "System: GIGABYTE B850 AORUS ELITE WIFI7"
WHEA = "Windows Hardware Errors (WHEA)"
DIMM_A = "DDR5 DIMM [#1] (P0 CHANNEL A/DIMM 1): Corsair CMK64GX5M2B6000Z30"
DIMM_B = "DDR5 DIMM [#3] (P0 CHANNEL B/DIMM 1): Corsair CMK64GX5M2B6000Z30"

# Spalte -> (Name, Gruppe, Standardwert); Werte koennen je Test ueberschrieben werden
BASE_COLUMNS = [
    ("cpu", "CPU (Tctl/Tdie) [°C]", CPU, 55.0),
    ("cpu_load", "Total CPU Usage [%]", CPU, 30.0),
    ("v12", "+12V [V]", BOARD, 12.05),
    ("v5", "+5V [V]", BOARD, 5.0),
    ("v33", "+3.3V [V]", BOARD, 3.33),
    ("gpu", "GPU Temperature [°C]", GPU, 60.0),
    ("gpu_load", "GPU Core Load [%]", GPU, 90.0),
    ("whea", "Total Errors [#]", WHEA, 0),
    ("mem_used", "Physical Memory Used [MB]", SYS, 20000),
    ("mem_avail", "Physical Memory Available [MB]", SYS, 44600),
    ("a_temp", "SPD Hub Temperature [°C]", DIMM_A, 35.0),
    ("a_vdd", "VDD (SWA) Voltage [V]", DIMM_A, 1.10),
    ("a_vin", "VIN Voltage [V]", DIMM_A, 4.95),
    ("b_temp", "SPD Hub Temperature [°C]", DIMM_B, 36.0),
    ("b_vdd", "VDD (SWA) Voltage [V]", DIMM_B, 1.10),
    ("b_vin", "VIN Voltage [V]", DIMM_B, 4.95),
]


def _fmt(v):
    if isinstance(v, bool):
        return "Yes" if v else "No"
    if isinstance(v, float):
        return f"{v:.3f}"
    return str(v)


def write_hwinfo(path, n=300, start=dt.datetime(2026, 10, 1, 18, 0, 0), interval=2.0, values=None, drop=(),
                 extra=(), footer=True, nul_tail=False):
    """HWiNFO-CSV wie vom echten Logger: Kopfzeile, Messzeilen, beim regulaeren Stopp Kopfzeile + Gruppenzeile.

    values: {Schluessel: Wert oder Funktion(i) -> Wert}; drop: Schluessel weglassen; extra: zusaetzliche Spalten
    (Schluessel, Name, Gruppe, Standardwert)."""
    values = values or {}
    cols = [c for c in BASE_COLUMNS if c[0] not in drop] + list(extra)
    head = "Date,Time," + ",".join(c[1] for c in cols) + ","
    lines = [head]
    for i in range(n):
        t = start + dt.timedelta(seconds=i * interval)
        row = []
        for key, _, _, default in cols:
            v = values.get(key, default)
            row.append(_fmt(v(i) if callable(v) else v))
        lines.append(f"{t.day}.{t.month}.{t.year},{t:%H:%M:%S}.000," + ",".join(row) + ",")
    if footer:
        lines.append(head)
        lines.append(",," + ",".join(c[2] for c in cols) + ",")
    data = ("\r\n".join(lines) + "\r\n").encode("utf-8")
    if nul_tail:
        data = data[:-40] + b"\x00" * 64
    with open(path, "wb") as f:
        f.write(data)
    return path


def lhm_fixture(dst_dir):
    """Ausschnitt eines echten LHM-Logs (03.10.2026, 17:40-18:30, mit Absturz um 17:52)."""
    dst = os.path.join(dst_dir, "LibreHardwareMonitorLog-2026-10-03.csv")
    with gzip.open(os.path.join(FIXTURES, "LibreHardwareMonitorLog-2026-10-03.csv.gz"), "rb") as src, open(dst, "wb") as f:
        shutil.copyfileobj(src, f)
    return dst


def config(**over):
    return h.deep_merge(h.DEFAULT_CONFIG, over)


@contextlib.contextmanager
def patched(**attrs):
    """Funktionen in hwlog_check voruebergehend ersetzen (z. B. fetch_events), danach wiederherstellen."""
    old = {k: getattr(h, k) for k in attrs}
    try:
        for k, v in attrs.items():
            setattr(h, k, v)
        yield
    finally:
        for k, v in old.items():
            setattr(h, k, v)


def no_windows():
    """Alle Windows-Abfragen abschalten, damit Tests auf jedem System gleich laufen (auch auf Windows-CI)."""
    def ev(start, end):
        return None, "im Test abgeschaltet"
    ev.dumps = None
    return patched(fetch_events=ev, fetch_sysinfo=lambda: (None, "im Test abgeschaltet"),
                   fetch_smart=lambda cfg: (None, "im Test abgeschaltet"))


def fake_events(events, dumps=None):
    def ev(start, end):
        out = [dict(x, u=x.get("u", x["t"])) for x in events if start <= dt.datetime.fromisoformat(x["t"]) <= end]
        ev.dumps = dumps
        return out, None
    ev.dumps = dumps
    return ev


def analyze(path, cfg=None, prev=None, events=True, markers=None):
    log = h.load_log(path, {})
    return h.Analysis(log, cfg or config(), prev_entry=prev, events_enabled=events, markers=markers).run()


def titles(an, min_level=0):
    return [f.title for f in an.F if f.level >= min_level]


def run_main(argv):
    """main() mit abgefangener Ausgabe; liefert (Exitcode, Ausgabe)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
        try:
            rc = h.main(argv)
        except SystemExit as ex:
            rc = ex.code
    return rc, buf.getvalue()


class TempDirCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="hwlog_test_")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, *parts):
        return os.path.join(self.tmp, *parts)
