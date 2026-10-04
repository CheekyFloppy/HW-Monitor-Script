"""LibreHardwareMonitor-Dauerlog: echter Ausschnitt vom 03.10.2026 mit Absturz um 17:52."""
import unittest

from tests.helpers import TempDirCase, analyze, fake_events, h, lhm_fixture, no_windows, patched, titles

CRASH = [
    {"t": "2026-10-03T18:18:30", "p": "Microsoft-Windows-Kernel-General", "id": 12, "lvl": 4, "msg": "Systemstart"},
    {"t": "2026-10-03T18:18:35", "p": "Microsoft-Windows-Kernel-Power", "id": 41, "lvl": 1, "msg": "neu gestartet",
     "props": "0;0;0;0;0;0;0;0"},
    {"t": "2026-10-03T18:18:36", "p": "EventLog", "id": 6008, "lvl": 2, "msg": "unerwartet heruntergefahren"},
]


class LhmLog(TempDirCase):
    def setUp(self):
        super().setUp()
        self.path = lhm_fixture(self.tmp)

    def test_erkennung_und_zuordnung(self):
        with no_windows():
            an = analyze(self.path)
        self.assertEqual(an.log.source, "LHM")
        self.assertEqual([d["label"] for d in an.s.dimms], ["Kanal A · DIMM 1", "Kanal B · DIMM 1"])
        self.assertIn("Board-Profil Gigabyte IT8696E angewendet", titles(an))
        rails = {c.short: c.vavg for c, _, _ in an.s.rails}
        self.assertAlmostEqual(rails["+12V"], 12.0, delta=0.3)  # Teiler x6 aus dem Board-Profil
        self.assertAlmostEqual(rails["+5V"], 5.0, delta=0.15)

    def test_luecke_ohne_ereignisprotokoll_bleibt_offen(self):
        with no_windows():
            an = analyze(self.path)
        self.assertEqual(an.worst, 0, titles(an, 1))
        self.assertTrue([t for t in titles(an) if "Unterbrechung" in t and "nicht zuzuordnen" in t])

    def test_absturz_mit_ereignisprotokoll(self):
        with patched(fetch_events=fake_events(CRASH)):
            an = analyze(self.path)
        crit = titles(an, 3)
        self.assertTrue(any(t.startswith("Absturz: Aufzeichnung bricht um 17:51:57 ab") for t in crit), crit)

    def test_bericht_mit_letzter_minute(self):
        with patched(fetch_events=fake_events(CRASH)):
            an = analyze(self.path)
        html = h.render_report(an)
        self.assertIn("17:51:57", html)


if __name__ == "__main__":
    unittest.main()
