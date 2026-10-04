"""Kommandozeile von aussen: Exitcodes, Ausgabedateien, Verlauf, Konfiguration, Markierungen."""
import json
import os
import unittest

from tests.helpers import TempDirCase, h, no_windows, run_main, write_hwinfo


class Cli(TempDirCase):
    def setUp(self):
        super().setUp()
        self._nw = no_windows()
        self._nw.__enter__()
        self.cfg = self.p("cfg.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"ausgabeordner": self.p("out")}, f)

    def tearDown(self):
        self._nw.__exit__(None, None, None)
        super().tearDown()

    def test_version(self):
        rc, out = run_main(["--version"])
        self.assertEqual(rc, 0)
        self.assertIn(h.VERSION, out)

    def test_exitcodes_sauber_und_absturz(self):
        ok = write_hwinfo(self.p("ok.CSV"))
        bad = write_hwinfo(self.p("bad.CSV"), footer=False, nul_tail=True, values={"whea": lambda i: 0 if i < 100 else 1})
        rc, _ = run_main([ok, "--config", self.cfg, "--kein-verlauf"])
        self.assertEqual(rc, 0)
        rc, _ = run_main([bad, "--config", self.cfg, "--kein-verlauf"])
        self.assertEqual(rc, 3)
        self.assertTrue(os.path.exists(self.p("out", "ok_bericht.html")))

    def test_verlauf_und_nur_neue_logs(self):
        write_hwinfo(self.p("a.CSV"))
        write_hwinfo(self.p("b.CSV"), values={"b_vin": 4.75})
        rc, out = run_main([self.tmp, "--config", self.cfg])
        self.assertEqual(rc, 2)  # VIN-Abweichung im zweiten Log
        with open(self.p("out", "verlauf.html"), encoding="utf-8") as f:
            html = f.read()
        self.assertIn("RAM-Eingangsspannung VIN je Kanal", html)
        with open(self.p("out", "verlauf.jsonl"), encoding="utf-8") as f:
            self.assertEqual(len(f.readlines()), 2)
        rc, out = run_main([self.tmp, "--config", self.cfg, "--neu"])
        self.assertIn("Keine neuen Logs ausgewertet.", out)

    def test_config_schreiben_ist_gueltig(self):
        rc, _ = run_main(["--config-schreiben", self.p("c.json")])
        self.assertEqual(rc, 0)
        with open(self.p("c.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), h.DEFAULT_CONFIG)

    def test_beispielkonfiguration_ist_aktuell(self):
        with open(os.path.join(h.os.path.dirname(h.__file__), "hwlog_config.example.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), h.DEFAULT_CONFIG, "hwlog_config.example.json mit --config-schreiben neu erzeugen")

    def test_markieren_landet_im_ausgabeordner(self):
        rc, out = run_main(["--config", self.cfg, "--markieren", "DRAM-LED rot nach Absturz"])
        self.assertEqual(rc, 0, out)
        with open(self.p("out", h.MARKER_FILE), encoding="utf-8") as f:
            self.assertEqual(json.loads(f.readline())["text"], "DRAM-LED rot nach Absturz")

    def test_nicht_lesbare_datei(self):
        with open(self.p("kaputt.CSV"), "w", encoding="utf-8") as f:
            f.write("kein,log\n")
        rc, out = run_main([self.p("kaputt.CSV"), "--config", self.cfg, "--kein-verlauf"])
        self.assertEqual(rc, 4)


if __name__ == "__main__":
    unittest.main()
