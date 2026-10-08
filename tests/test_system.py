"""Systemzustand pro Aufruf mit ersetzten Windows-Abfragen: RAM-Bestand, Chronik, Speicherabbilder, SMART."""
import datetime as dt
import json
import os
import unittest

from tests.helpers import TempDirCase, config, fake_events, h, patched

MOD_A = {"slot": "DIMM 1", "bank": "P0 CHANNEL A", "gb": 32, "sn": "AAA111", "pn": "CMK64GX5M2B6000Z30", "mts": 4800}
MOD_B = {"slot": "DIMM 1", "bank": "P0 CHANNEL B", "gb": 32, "sn": "BBB222", "pn": "CMK64GX5M2B6000Z30", "mts": 4800}


def iso(minutes_ago):
    return (dt.datetime.now() - dt.timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")


def sysinfo(ram=(MOD_A, MOD_B), nutzbar=63.1, bios="F12d", devices=(), dumps=(), post_ms=19000, boot=30):
    return {"bios": bios, "bios_datum": "2026-09-23", "board": "Gigabyte Technology Co., Ltd. B850 AORUS ELITE WIFI7",
            "cpu": "AMD Ryzen 7 7800X3D 8-Core Processor", "microcode_raw": "0C12600A",
            "gpus": [{"name": "NVIDIA GeForce RTX 4080 SUPER", "treiber": "32.0.15.9192"}],
            "win_build": "26200", "win_ubr": "9457", "win_version": "25H2", "boot": iso(boot), "post_ms": post_ms,
            "dump_modus": 7, "auto_neustart": 1, "pagefile_auto": True, "pagefiles": [], "dumpok": True,
            "minidumps": list(dumps), "geraete": list(devices), "ram": list(ram), "ram_nutzbar_gb": nutzbar}


def smart(unsafe=128):
    return [{"modell": "WD_BLACK SN770 2TB", "serie": "S1", "ok": True, "typ": "NVMe", "krit_warnung": 0,
             "medienfehler": 0, "unsicher_aus": unsafe, "einschaltvorgaenge": 2513, "betriebsstunden": 5594, "temp": 26}]


class SystemCheck(TempDirCase):
    def run_round(self, info, events=(), unsafe=128):
        cfg = config(smartctl_pfad="im-test")
        with patched(fetch_sysinfo=lambda: (info, None), fetch_events=fake_events(list(events), dumps=[]),
                     fetch_smart=lambda c: (smart(unsafe), None)):
            return h.run_system_check(self.tmp, cfg, markers=[])

    def findings(self, sysd, min_level=0):
        return [(f[0], f[2]) for f in sysd["findings"] if f[0] >= min_level]

    def test_erster_lauf_ist_ruhig_und_speichert_den_stand(self):
        sysd = self.run_round(sysinfo())
        self.assertEqual(sysd["worst"], 0, self.findings(sysd, 1))
        snap = sysd["snaps"][-1]
        self.assertEqual(snap["microcode"], "A60120C")
        self.assertEqual(snap["ram_gb"], 64)
        self.assertEqual(snap["ram_takt"], "4800 MT/s")
        self.assertEqual([m["kanal"] for m in snap["ram"]], ["A", "B"])
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "system.jsonl")))

    def test_kanal_b_faellt_aus(self):
        self.run_round(sysinfo())
        sysd = self.run_round(sysinfo(ram=(MOD_A,), nutzbar=31.1))
        crit = [t for lvl, t in self.findings(sysd, 3)]
        self.assertIn("Speicherkanal B fehlt (1 von 2 Modulen, 32 GB statt 64 GB)", crit)
        html = h.render_system(sysd)
        self.assertIn('id="ram"', html)
        self.assertIn("SN BBB222", html)

    def test_umgesteckte_module_sind_kein_ausfall(self):
        self.run_round(sysinfo())
        swapped = (dict(MOD_A, sn="BBB222"), dict(MOD_B, sn="AAA111"))
        sysd = self.run_round(sysinfo(ram=swapped))
        self.assertFalse(self.findings(sysd, 2))
        self.assertIn((1, "RAM-Module umgesteckt"), self.findings(sysd))

    def test_platzhalter_seriennummern_sind_kein_umstecken(self):
        # Fall vom 08.10.: beide Module melden 00000000
        ram = (dict(MOD_A, sn="00000000"), dict(MOD_B, sn="00000000"))
        self.run_round(sysinfo(ram=ram))
        sysd = self.run_round(sysinfo(ram=ram))
        self.assertEqual(self.findings(sysd, 1), [])
        self.assertNotIn("SN 00000000", h.render_system(sysd))

    def test_schwankende_startzeit_ist_derselbe_start(self):
        info = sysinfo()
        self.run_round(info)
        later = dict(info, boot=(h._ts(info["boot"]) + dt.timedelta(seconds=1)).isoformat(timespec="seconds"))
        sysd = self.run_round(later)
        html = h.render_system(sysd)
        self.assertEqual(html.count("SN AAA111"), 1)
        sec = html.split('id="systemstand"', 1)[1].split("</table>", 1)[0]
        self.assertEqual(sec.count("<tr><td>"), 1)

    def test_neue_ereignisse_im_zweiten_lauf(self):
        self.run_round(sysinfo())
        evs = [{"t": iso(3), "p": "Microsoft-Windows-Kernel-General", "id": 12, "lvl": 4, "msg": "Start"},
               {"t": iso(2), "p": "Microsoft-Windows-Kernel-Power", "id": 41, "lvl": 1, "msg": "x", "props": "0;0;0;0;0;0;0;0"},
               {"t": iso(1), "p": "Microsoft-Windows-MemoryDiagnostics-Results", "id": 1202, "lvl": 2,
                "msg": "Die Windows-Speicherdiagnose hat Hardwarefehler erkannt."}]
        sysd = self.run_round(sysinfo(), events=evs, unsafe=129)
        crit = [t for lvl, t in self.findings(sysd, 3)]
        self.assertTrue(any(t.startswith("Harter Absturz/Reset ohne Bluescreen") for t in crit), crit)
        self.assertTrue(any(t.startswith("Windows-Speicherdiagnose: Fehler gefunden") for t in crit), crit)
        self.assertIn((1, "NVMe WD_BLACK SN770 2TB: +1 unsichere Abschaltung(en) seit der letzten Auswertung"),
                      self.findings(sysd))
        self.assertEqual(sysd["stab"]["n7"], 1)
        with open(os.path.join(self.tmp, "ereignisse.jsonl"), encoding="utf-8") as f:
            stored = [json.loads(x) for x in f]
        self.assertTrue(any(x["id"] == 1202 for x in stored))

    def test_grafikkarte_mit_fehlercode_und_bios_wechsel(self):
        self.run_round(sysinfo())
        dev = [{"name": "NVIDIA GeForce RTX 4080 SUPER", "code": 43, "klasse": "Display"}]
        sysd = self.run_round(sysinfo(bios="F13", devices=dev, post_ms=152000, boot=5))
        got = self.findings(sysd)
        self.assertIn((2, "1 Gerät(e) mit Fehler im Gerätemanager"), got)
        self.assertIn((1, "Systemstand geändert seit der letzten Auswertung"), got)
        self.assertTrue(any(lvl == 1 and "BIOS brauchte 152 s" in t for lvl, t in got), got)


class Markers(TempDirCase):
    def test_markierung_im_ausgabeordner(self):
        cfg = config(ausgabeordner=self.tmp)
        h.add_marker("  Bildaussetzer   auf dem Desktop ", cfg)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, h.MARKER_FILE)))
        got = h.load_markers(cfg)
        self.assertEqual([m["text"] for m in got if m["text"].startswith("Bildaussetzer")], ["Bildaussetzer auf dem Desktop"])

    def test_doppelte_markierungen_aus_mehreren_orten(self):
        cfg = config(ausgabeordner=self.tmp)
        h.add_marker("eins", cfg)
        got = h.load_markers(cfg, [self.tmp, self.tmp])
        self.assertEqual(sum(1 for m in got if m["text"] == "eins"), 1)


if __name__ == "__main__":
    unittest.main()
