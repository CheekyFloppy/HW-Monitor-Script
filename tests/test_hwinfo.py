"""Pruefungen an HWiNFO-Logs (synthetisch erzeugt, Format wie der echte Logger)."""
import unittest

from tests.helpers import TempDirCase, analyze, config, h, no_windows, titles, write_hwinfo


class HwinfoChecks(TempDirCase):
    def setUp(self):
        super().setUp()
        self._nw = no_windows()
        self._nw.__enter__()

    def tearDown(self):
        self._nw.__exit__(None, None, None)
        super().tearDown()

    def run_log(self, cfg=None, prev=None, **kw):
        return analyze(write_hwinfo(self.p("t.CSV"), **kw), cfg=cfg, prev=prev, events=False)

    def test_sauberes_log_ist_unauffaellig(self):
        an = self.run_log()
        self.assertEqual(an.worst, 0, titles(an, 1))
        self.assertIn("Log regulär beendet", titles(an))
        self.assertIn("2 RAM-Module erkannt (Kanäle A, B)", titles(an))

    def test_fehlender_abschluss_mit_nul_bytes(self):
        an = self.run_log(footer=False, nul_tail=True)
        self.assertGreaterEqual(an.worst, 2)
        self.assertIn("Log endet ohne regulären Abschluss", titles(an, 2))

    def test_whea_anstieg_ist_kritisch(self):
        an = self.run_log(values={"whea": lambda i: 0 if i < 150 else 2})
        self.assertTrue(any("WHEA" in t for t in titles(an, 3)), titles(an, 1))

    def test_12v_einbruch(self):
        an = self.run_log(values={"v12": lambda i: 11.3 if i % 50 == 0 else 12.05})
        self.assertTrue(any(t.startswith("+12 V") for t in titles(an, 3)), titles(an, 1))

    def test_x3d_eigene_temperaturgrenzen(self):
        an = self.run_log(values={"cpu": 87.0})  # 7800X3D: [80, 85, 89] -> Warnung
        self.assertIn("CPU-Temperatur: max 87,0 °C", titles(an, 2))
        self.assertFalse(any("CPU-Temperatur" in t for t in titles(an, 3)))


class RamChecks(TempDirCase):
    """Das Fehlerbild des Referenzsystems: Speicherkanal B faellt aus oder driftet."""

    def setUp(self):
        super().setUp()
        self._nw = no_windows()
        self._nw.__enter__()

    def tearDown(self):
        self._nw.__exit__(None, None, None)
        super().tearDown()

    def run_log(self, cfg=None, prev=None, **kw):
        return analyze(write_hwinfo(self.p("t.CSV"), **kw), cfg=cfg, prev=prev, events=False)

    def test_vin_abweichung_zwischen_modulen(self):
        an = self.run_log(values={"b_vin": 4.75})
        hit = [f for f in an.F if "weicht um" in f.title]
        self.assertEqual(len(hit), 1, titles(an, 1))
        self.assertEqual(hit[0].level, 2)
        self.assertIn("Kanal B", hit[0].title)

    def test_einzelne_fehlmessung_ist_keine_abweichung(self):
        an = self.run_log(values={"b_vin": lambda i: 4.60 if i == 100 else 4.95})
        self.assertFalse([t for t in titles(an) if "weicht um" in t])

    def test_temperatur_abweichung(self):
        an = self.run_log(values={"b_temp": 45.0})
        self.assertTrue([f for f in an.F if "Temperatur weicht um" in f.title and f.level == 1], titles(an, 1))

    def test_halber_speicher_trotz_zwei_sensoren(self):
        an = self.run_log(cfg=config(ram_gb_erwartet=64), values={"mem_avail": 11800})
        hit = [f for f in an.F if "Arbeitsspeicher nutzbar" in f.title]
        self.assertEqual(hit[0].level, 3)
        self.assertIn("Sensoren zeigen trotzdem 2 Module", hit[0].detail)

    def test_sollwert_automatisch_aus_vorigem_log(self):
        prev = {"fp": {"RAM nutzbar": "63,1 GB"}, "start": "2026-09-30T18:00:00"}
        an = self.run_log(prev=prev, values={"mem_avail": 11800})
        self.assertTrue([t for t in titles(an, 3) if "63,1 GB im vorigen Log" in t], titles(an, 1))

    def test_voller_speicher_ist_ok(self):
        an = self.run_log(cfg=config(ram_gb_erwartet=64))
        self.assertFalse([t for t in titles(an) if "Arbeitsspeicher nutzbar" in t])

    def test_fehlendes_modul(self):
        an = self.run_log(drop=("b_temp", "b_vdd", "b_vin"))
        self.assertIn("Nur 1 von 2 RAM-Modulen erkannt", titles(an, 3))

    def test_pmic_fehlerflag(self):
        flag = ("b_flag", "PMIC High Temperature Warning [Yes/No]", "DDR5 DIMM [#3] (P0 CHANNEL B/DIMM 1): Corsair "
                "CMK64GX5M2B6000Z30", False)
        an = self.run_log(extra=[flag], values={"b_flag": lambda i: i == 200})
        self.assertTrue([t for t in titles(an, 3) if "PMIC" in t], titles(an, 1))

    def test_kpi_je_kanal_fuer_den_verlauf(self):
        an = self.run_log(values={"b_vin": 4.80})
        self.assertEqual(sorted(an.kpi["dimm"]), ["A", "B"])
        self.assertAlmostEqual(an.kpi["dimm"]["B"]["vin_min"], 4.80, places=3)
        self.assertAlmostEqual(an.kpi["ram_gb"], 63.1, places=1)


class Report(TempDirCase):
    def test_bericht_wird_erzeugt(self):
        with no_windows():
            an = analyze(write_hwinfo(self.p("t.CSV"), values={"b_vin": 4.75}), events=False)
        html = h.render_report(an)
        self.assertIn("<!doctype html>", html.lower())
        self.assertIn("weicht um", html)
        self.assertNotIn("http://", html.replace("http://www.w3.org", ""))  # keine externen Ressourcen


if __name__ == "__main__":
    unittest.main()
