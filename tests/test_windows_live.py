"""Echte Windows-Abfragen (nur unter Windows, z. B. GitHub Actions windows-latest).

Prueft, dass PowerShell-Skripte laufen und ihre Ausgabe die Form hat, die das Tool erwartet. Werte des
Testrechners werden nicht bewertet, nur Typen und Pflichtfelder."""
import datetime as dt
import os
import unittest

from tests.helpers import config, h

WINDOWS = os.name == "nt"


@unittest.skipUnless(WINDOWS, "nur unter Windows")
class LiveWindows(unittest.TestCase):
    def test_pfade(self):
        self.assertTrue(os.path.isdir(h.system_dir()))
        self.assertTrue(os.path.isfile(h.powershell_path()))
        self.assertTrue(os.path.isabs(h.documents_dir()))

    def test_systemstand(self):
        raw, err = h.fetch_sysinfo()
        self.assertIsNone(err)
        self.assertIsInstance(raw, dict)
        for key in ("bios", "cpu", "win_build", "boot", "ram", "ram_nutzbar_gb"):
            self.assertIn(key, raw)
        snap = h.system_snapshot(raw, None)
        self.assertIn("Build", snap.get("windows", ""))
        self.assertIsInstance(snap.get("ram", []), list)
        for m in snap.get("ram", []):
            self.assertIn("slot", m)
            self.assertIn("kanal", m)
        self.assertIsInstance(snap.get("geraete"), list)

    def test_ereignisprotokoll(self):
        now = dt.datetime.now()
        evs, err = h.fetch_events(now - dt.timedelta(days=2), now)
        self.assertIsNone(err)
        self.assertIsInstance(evs, list)
        for ev in evs[:200]:
            for key in ("t", "p", "id", "lvl"):
                self.assertIn(key, ev)
            h.classify_event(ev)  # darf bei keinem echten Ereignis abstuerzen
        self.assertTrue(h.fetch_events.dumps is None or isinstance(h.fetch_events.dumps, list))

    def test_systemcheck_komplett(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            sysd = h.run_system_check(d, config(), markers=[])
            self.assertIn(sysd["worst"], (0, 1, 2, 3))
            self.assertIsInstance(h.render_system(sysd), str)

    def test_smartctl_fehlt_oder_liefert_liste(self):
        res, err = h.fetch_smart(config())
        self.assertTrue(res is None and isinstance(err, str) or isinstance(res, list))


if __name__ == "__main__":
    unittest.main()
