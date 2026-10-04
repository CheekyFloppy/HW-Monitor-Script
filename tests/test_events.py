"""Einordnung von Windows-Ereignissen und die reinen Hilfsfunktionen der Chronik."""
import datetime as dt
import unittest

from tests.helpers import h


def ev(p, i, lvl=2, msg="", props=""):
    return {"t": "2026-10-03T18:00:00", "p": p, "id": i, "lvl": lvl, "msg": msg, "props": props}


class ClassifyEvent(unittest.TestCase):
    def test_kernel_power_41_ohne_bluescreen(self):
        e = ev("Microsoft-Windows-Kernel-Power", 41, 1, props="0;0;0;0;0;0;0;0")
        self.assertEqual(h.classify_event(e)[0], 3)
        self.assertIn("kein Bluescreen-Code", e["_extra"])

    def test_kernel_power_41_mit_einschaltknopf(self):
        e = ev("Microsoft-Windows-Kernel-Power", 41, 1, props="0;0;0;0;0;0;1;0")
        h.classify_event(e)
        self.assertIn("Einschaltknopf", e["_extra"])

    def test_bluescreen_code_wird_benannt(self):
        lvl, label = h.classify_event(ev("Microsoft-Windows-WER-SystemErrorReporting", 1001,
                                         msg="Der Fehlercode war: 0x00000050 (0xffff...)"))
        self.assertEqual(lvl, 3)
        self.assertEqual(label, "Bluescreen 0x50 PAGE_FAULT_IN_NONPAGED_AREA")

    def test_speicherdiagnose(self):
        p = "Microsoft-Windows-MemoryDiagnostics-Results"
        self.assertEqual(h.classify_event(ev(p, 1201, 4, "Die Windows-Speicherdiagnose hat keine Fehler erkannt."))[0], 0)
        self.assertEqual(h.classify_event(ev(p, 1101, 4, "The Windows Memory Diagnostic detected no errors"))[0], 0)
        self.assertEqual(h.classify_event(ev(p, 1202, 2, "Die Windows-Speicherdiagnose hat Hardwarefehler erkannt."))[0], 3)

    def test_livekernelevent_grafik(self):
        e = ev("Windows Error Reporting", 1001, 4, msg="LiveKernelEvent", props="x;x;x;x;x;141;x")
        lvl, label = h.classify_event(e)
        self.assertEqual(lvl, 2)
        self.assertIn("VIDEO_ENGINE_TIMEOUT_DETECTED", label)
        self.assertEqual(e["_lke"], "141")

    def test_neue_ereignisarten(self):
        self.assertEqual(h.classify_event(ev("disk", 157))[0], 2)
        self.assertEqual(h.classify_event(ev("Microsoft-Windows-Kernel-PnP", 411))[0], 1)
        self.assertEqual(h.classify_event(ev("Microsoft-Windows-Kernel-Processor-Power", 37))[0], 1)

    def test_unbekannter_fehler_ist_info(self):
        self.assertEqual(h.classify_event(ev("Irgendwas", 1, 2))[0], 0)


class Helpers(unittest.TestCase):
    def test_microcode_amd_und_intel(self):
        self.assertEqual(h._microcode("0C12600A"), "A60120C")  # AMD: 4 Byte (Registry 12 18 96 10)
        self.assertEqual(h._microcode("00000000AE000000"), "AE")  # Intel: 8 Byte, Revision oben
        self.assertEqual(h._microcode(""), "")
        self.assertEqual(h._microcode("00000000"), "")

    def test_nvidia_treiberversion(self):
        self.assertEqual(h._driver_label("NVIDIA GeForce RTX 4080 SUPER", "32.0.15.9192"), "591.92")

    def test_ram_kanal_aus_bios_bezeichnung(self):
        self.assertEqual(h._ram_channel({"bank": "P0 CHANNEL B", "slot": "DIMM 1"}), "B")
        self.assertEqual(h._ram_channel({"bank": "BANK 0", "slot": "DIMM_A2"}), "A")
        self.assertEqual(h._ram_channel({"bank": "", "slot": "Channel B-DIMM1"}), "B")
        self.assertEqual(h._ram_channel({"bank": "BANK 0", "slot": "DIMM 0"}), "")


def rec(t, p, i, lvl, label="", extra=""):
    return {"t": t, "p": p, "id": i, "lvl": lvl, "label": label, "extra": extra, "msg": ""}


class Chronik(unittest.TestCase):
    def test_absturzmeldungen_eines_neustarts_sind_ein_vorfall(self):
        evs = [rec("2026-09-18T13:00:00", "Microsoft-Windows-Kernel-Power", 41, 3, extra="Bugcheck 0x1E KMODE_EXCEPTION_NOT_HANDLED"),
               rec("2026-09-18T13:00:05", "EventLog", 6008, 3, "Unerwartetes Herunterfahren (EventLog 6008)"),
               rec("2026-09-18T15:52:00", "Microsoft-Windows-Kernel-Power", 41, 3, extra="BugcheckCode 0 (kein Bluescreen)")]
        inc = h.crash_incidents(evs)
        self.assertEqual(len(inc), 2)
        self.assertEqual(inc[0]["code"], 0x1E)
        self.assertIsNone(inc[1]["code"])
        self.assertEqual(inc[1]["art"], "Harter Absturz/Reset ohne Bluescreen")

    def test_bootschleife(self):
        evs = [rec(f"2026-09-08T23:2{m}:00", "Microsoft-Windows-Kernel-General", 12, -1) for m in (2, 3, 4)]
        loops = h.boot_loops(evs)
        self.assertEqual(len(loops), 1)
        self.assertEqual(loops[0][2], 3)

    def test_gleiche_ereignisse_werden_zusammengefasst(self):
        lab = "LiveKernelEvent 141 (VIDEO_ENGINE_TIMEOUT_DETECTED)"
        evs = [rec(f"2026-10-02T16:0{m}:00", "Windows Error Reporting", 1001, 2, lab, "LiveKernelEvent 141 = VIDEO_ENGINE_TIMEOUT_DETECTED")
               for m in range(6, 10)] + [rec("2026-10-02T18:00:00", "Windows Error Reporting", 1001, 2, lab)]
        rows = h._collapse_events(evs)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][2], "4× " + lab)
        self.assertEqual(rows[0][3], "von 16:06 bis 16:09")  # doppelter Text faellt weg

    def test_absturzmuster(self):
        now = dt.datetime.now()
        inc = [{"t": (now - dt.timedelta(days=d)).isoformat(timespec="seconds"), "code": c}
               for d, c in ((1, 0x1E), (2, 0xEF), (3, None), (4, 0x50), (40, 0x0A))]
        pat = h.crash_pattern(inc)
        self.assertEqual(pat["n"], 4)  # 0x0A liegt ausserhalb von 30 Tagen
        self.assertTrue(pat["hardware"])
        self.assertTrue(pat["speicher"])  # 0x50
        same = h.crash_pattern([dict(x, code=0x116) for x in inc])
        self.assertFalse(same["hardware"])  # immer derselbe Code -> eher Treiber


if __name__ == "__main__":
    unittest.main()
