# hwlog_check

Wertet Sensorlogs von **HWiNFO** und **LibreHardwareMonitor (LHM)** aus und sucht nach Stabilitäts- und
Hardwareauffälligkeiten. Ergebnis ist ein interaktiver HTML-Bericht pro Log plus ein Verlauf über alle
ausgewerteten Sitzungen. Unter Windows werden passende Einträge aus dem Ereignisprotokoll zugeordnet
(Kernel-Power 41, Bluescreen, WHEA-Logger, TDR, LiveKernelEvents …).

Entstanden ist das Tool, um einen sporadischen Ausfall eines RAM-Kanals bzw. Abstürze auf einem
AM5-System (Ryzen 7 7800X3D) einzugrenzen – entsprechend liegt der Schwerpunkt auf RAM, SoC-Spannung,
PCIe, Netzteilschienen und der Frage „Was war kurz vor dem Absturz los?“.

- Nur Python-Standardbibliothek (ab 3.8), keine Zusatzpakete
- Ausgaben und Bericht auf Deutsch; HWiNFO-Sensornamen werden auf Deutsch und Englisch erkannt
- Läuft auch unter Linux/macOS (dann ohne Ereignisprotokoll und ohne UAC)

## Dateien

| Datei | Zweck |
|---|---|
| `hwlog_check.py` | das eigentliche Tool |
| `hwlog_check.bat` | Drag-&-Drop-Starter für Windows |
| `hwlog_config.example.json` | Standard-Grenzwerte als Vorlage (erzeugt mit `--config-schreiben`) |

## Schnellstart (Windows)

1. Python 3 installieren (der `py`-Launcher wird bevorzugt, sonst `python`).
2. `hwlog_check.py` und `hwlog_check.bat` in denselben Ordner legen.
3. Eine oder mehrere HWiNFO-CSV-Dateien **oder einen Log-Ordner** auf `hwlog_check.bat` ziehen.

Die BAT ruft das Skript immer mit `--admin --oeffnen` auf; wird ein Ordner übergeben, zusätzlich mit `--neu`.

- **`--admin`**: Windows fragt per UAC nach Adminrechten, damit `C:\Windows\LiveKernelReports` gelesen werden
  kann. „Nein“ ist in Ordnung – dann läuft alles außer der Dump-Datei-Prüfung.
- **`--neu`** (bei Ordnern): nur neue bzw. gewachsene Logs werden ausgewertet. Ist nichts neu, öffnet sich der Verlauf.

Hinweis: Die BAT prüft nur das **erste** Argument darauf, ob es ein Ordner ist.

## Kommandozeile

```
py hwlog_check.py gaming.CSV --oeffnen
py hwlog_check.py D:\HWiNFO-Logs --neu
py hwlog_check.py "C:\Program Files\LibreHardwareMonitor" --neu --aufraeumen 30
py hwlog_check.py --config-schreiben hwlog_config.json
```

| Option | Bedeutung |
|---|---|
| `pfade …` | CSV-Dateien oder Ordner (alle `*.csv` direkt darin) |
| `-o`, `--ausgabe` | Ausgabeordner für Berichte und Verlauf |
| `--neu` | nur Logs auswerten, die noch nicht im Verlauf stehen (LHM-Tagesdateien: auch wenn sie gewachsen sind) |
| `--oeffnen` | Bericht bzw. Verlauf danach im Browser öffnen |
| `--config DATEI` | eigene Grenzwerte (JSON, wird mit den Standardwerten zusammengeführt) |
| `--config-schreiben DATEI` | Standard-Grenzwerte als JSON schreiben und beenden |
| `--riegel N` | erwartete Anzahl RAM-Module (Standard 2) |
| `--keine-ereignisse` | Windows-Ereignisprotokoll nicht abfragen |
| `--kein-verlauf` | `verlauf.jsonl`/`verlauf.html` weder lesen noch schreiben |
| `-q`, `--leise` | Konsole: nur eine Zeile pro Log |
| `--aufraeumen TAGE` | `LibreHardwareMonitorLog-JJJJ-MM-TT*.csv` älter als TAGE in den übergebenen Ordnern löschen (min. 2) |
| `--admin` | unter Windows per UAC mit Adminrechten neu starten |
| `--version` | Version ausgeben |

### Exitcodes

`0` unauffällig · `1` Hinweise · `2` Warnung · `3` kritisch · `4` Fehler (Datei nicht auswertbar, keine Dateien,
unsicherer Ausgabeordner). Bei mehreren Logs gilt der schlechteste Wert.

## Ausgabe

Ohne `-o` landet alles in `hwlog_berichte\` neben dem ersten Log. Bei LHM-Logs (die meist im Programmordner
liegen, wo nur Admins schreiben dürfen) stattdessen in `Dokumente\hwlog_berichte`. Ein fester Ordner lässt sich
per Konfiguration (`ausgabeordner`) setzen.

| Datei | Inhalt |
|---|---|
| `<log>_bericht.html` | Bericht zu einem Log: Status, Befunde mit Erklärung, Konfiguration, Diagramme, Kennzahlen, Ereignisse, „Letzte Minute vor Logende/Absturz“ |
| `verlauf.html` | Übersicht aller Sitzungen mit Trenddiagrammen |
| `verlauf.jsonl` | Verlaufsdaten (eine Zeile pro Log: Kennzahlen, Hardware-Fingerabdruck, wichtigste Befunde) |
| `sensor_schema.json` | Zwischenspeicher der Sensorgruppen (siehe unten) |

Die HTML-Seiten sind eigenständig (CSS/JS inline, keine externen Ressourcen), mit Hell-/Dunkelmodus. In den
Diagrammen: Ziehen zoomt, Doppelklick zeigt alles, Klick auf eine Uhrzeit in den Befunden springt an die Stelle.
Lange Logs werden für die Diagramme auf max. 6000 Punkte per Min/Max-Verdichtung reduziert.

## Was geprüft wird

Jeder Befund hat eine Stufe: **Info** · **Hinweis** · **Warnung** · **Kritisch**. Der Status eines Logs ist die
schwerste Stufe.

| Bereich | Prüfung |
|---|---|
| Log-Integrität | regulärer Logabschluss vorhanden? NUL-Bytes / halbe letzte Zeile (typisch für Absturz), unlesbare Zeilen, Zeitumstellung (EU-Sommer-/Winterzeit wird herausgerechnet) |
| Lücken | Abstand zwischen Messpunkten > `log_luecke_faktor` × Intervall (Freeze, Treiber-Reset, Standby) |
| WHEA | Anstieg des HWiNFO-Zählers „Windows Hardware Errors“ während des Logs |
| PCIe (GPU) | Receiver/Replay/NAK/LCRC/Bad TLP/Correctable/Fatal-Zähler – getrennt nach „unter Dauerlast“ und „bei Link-Wechsel/Leerlauf“ (letzteres ist normal); Link-Herabstufung unter Last |
| Drosselung | CPU-Throttling/PROCHOT (inkl. EXT vom Board), GPU thermisches Limit |
| Temperaturen | CPU, GPU Kern/Hotspot/Speicher, RAM-Module, NVMe/SATA, VRM, Chipsatz |
| Spannungen | Netzteilschienen +12/+5/+3,3 V, 3VSB/5VSB, GPU-12V (12VHPWR, 8-Pin, Slot) als %-Abweichung; Vcore (eigene Grenze für X3D), SoC, VDDIO_MEM, RAM VDD/VDDQ, RAM-VIN, CMOS-Batterie |
| RAM-Module | Anzahl erkannter DIMMs vs. erwartet (Kanalausfall!), PMIC-Fehlerflags, Sensoren eines Moduls verstummen |
| Laufwerke | SMART-Fehler/-Warnung, Restlebensdauer |
| Lüfter | Stillstand (kritisch bei CPU/Pumpe/AIO unter Wärme), GPU-Lüfter steht bei ≥ 72 °C |
| Frametimes | Spitzen > `frametime_spitze_ms`, getrennt nach Spiel und Lade-/Menüphasen (PresentMon, dwm.exe wird erkannt) |
| Sensor-Aussetzer | zentrale Sensoren liefern zeitweise oder bis Logende keine Werte |
| Logende | Zustand beim letzten Messpunkt (Last/Leerlauf), Spannungseinbruch in den letzten 30 s |
| Konfiguration | Fingerabdruck (CPU, Board, GPU, RAM-Module/-Takt/-Timings, FCLK, UCLK-Verhältnis, SoC/VDDIO/VDD, PCIe-Gen, GPU-Limit, Laufwerke) und Vergleich mit dem vorherigen Log derselben Quelle – fällt z. B. auf, wenn ein BIOS-Update EXPO zurückgesetzt hat |
| Ereignisprotokoll | Kernel-Power 41 (mit BugcheckCode/Einschaltknopf), EventLog 6008, Bluescreens (WER 1001), WHEA-Logger, Display 4101/nvlddmkm/amdkmdag, Datenträgerfehler, Programmabstürze, LiveKernelEvents |

**Absturzerkennung bei HWiNFO:** Fehlt der Logabschluss und folgt nach Logende ein Kernel-Power 41, wird der
Befund auf „Absturz“ hochgestuft. Der Bericht zeigt dann die letzte Minute vor Logende als Tabelle.

**LiveKernelEvents:** Windows meldet alte Berichte wiederholt, bis sie hochgeladen sind. Das Tool gleicht deshalb
mit den Zeitstempeln der Dump-Dateien in `C:\Windows\LiveKernelReports` ab (dafür braucht es Adminrechte) und
trennt echte neue Ereignisse von Wiederholungen.

## LibreHardwareMonitor-Dauerlogs

LHM-Logs (Kopfzeilen `,/amdcpu/0/…` und `Time,…`) werden automatisch erkannt. Sensoren werden auf
HWiNFO-ähnliche Namen und Gruppen abgebildet, damit dieselben Prüfungen greifen.

- LHM schreibt **keinen Logabschluss**. Abstürze werden stattdessen über Unterbrechungen der Aufzeichnung plus
  Ereignisprotokoll erkannt. Jede Lücke wird eingeordnet: Absturz, Hänger beim Herunterfahren, Neustart,
  Update-Neustart, Herunterfahren, Standby, Ab-/Anmelden, Zeitumstellung, Uhrzeitkorrektur, LHM beendet
  (erkannt am Änderungsdatum von `LibreHardwareMonitor.config`), LHM selbst abgestürzt, kurzer Aussetzer.
- Bei einem Absturz zeigt der Bericht die letzte Minute davor.
- **Board-Profil Gigabyte ITE IT8696E:** LHM kennt diesen Chip auf manchen Gigabyte-Boards noch nicht und liefert
  nur `Voltage #1…#7`. Das Tool rechnet mit den Teilern der X870 AORUS ELITE WIFI7-Belegung um (3,3 V ×1,649,
  12 V ×6, 5 V ×2,5) – aber nur, wenn die Ergebnisse plausibel sind. Abgeglichen gegen HWiNFO auf einem
  B850 AORUS ELITE WIFI7.
- DIMM-Kanäle werden aus der SPD-Adresse abgeleitet (DIMM #1 = Kanal A, #3 = Kanal B).
- WHEA kommt bei LHM nur aus dem Ereignisprotokoll (kein Zähler).
- `lhm_aufbewahrung_tage` / `--aufraeumen` löscht alte Tagesdateien.

## Sensorgruppen ohne Logabschluss

Die Zuordnung „Spalte → Gerät“ (z. B. „DDR5 DIMM [#3] … CHANNEL B“) steht bei HWiNFO nur im Logabschluss. Fehlt
er (Absturz), versucht das Tool der Reihe nach:

1. `sensor_schema.json` – Zwischenspeicher aus einem früheren sauberen Log mit identischer Kopfzeile
2. ein Nachbarlog im selben Ordner mit gleicher Kopfzeile und Abschluss
3. Schätzung anhand der Spaltennamen (RAM- und SMART-Blöcke)

Logs werden deshalb chronologisch verarbeitet, damit frühere saubere Logs den Zwischenspeicher füllen.
Tipp: HWiNFO-Logging nach Möglichkeit normal stoppen.

## Konfiguration

`hwlog_config.json` neben dem Skript wird automatisch geladen, alternativ `--config DATEI`. Es genügt, nur die
abweichenden Werte einzutragen. Vorlage: [`hwlog_config.example.json`](hwlog_config.example.json).

Die meisten Grenzwerte sind Tripel `[Hinweis, Warnung, Kritisch]`:

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `erwartete_riegel` | `2` | erwartete Anzahl RAM-Module |
| `temperaturen.cpu` | `[85, 89, 95]` | °C, Tctl/Tdie |
| `temperaturen.gpu_kern` / `gpu_hotspot` / `gpu_speicher` | `[80,87,90]` / `[95,105,110]` / `[90,100,105]` | °C |
| `temperaturen.ram` | `[65, 75, 85]` | °C, SPD-Hub |
| `temperaturen.nvme` / `sata` | `[70,80,85]` / `[55,65,70]` | °C |
| `temperaturen.vrm` / `chipsatz` | `[90,105,115]` / `[75,85,95]` | °C |
| `schienen_toleranz_prozent` | `[3, 4, 5]` | Abweichung der 12/5/3,3-V-Schienen vom Sollwert |
| `spannungen.soc_max` | `[1.25, 1.30, 1.35]` | V |
| `spannungen.vcore_max` / `vcore_max_x3d` | `[1.45,1.50,1.55]` / `[1.30,1.35,1.40]` | V, X3D wird am CPU-Namen erkannt |
| `spannungen.ram_max` | `[1.42, 1.45, 1.50]` | V, VDD/VDDQ/VDDIO_MEM |
| `spannungen.ram_vin_min` | `[4.75, 4.60, 4.40]` | V, Untergrenze |
| `spannungen.cmos_batterie_min` | `[2.90, 2.70, 2.50]` | V, Untergrenze |
| `ssd_restlebensdauer_min` | `[20, 10, 3]` | % |
| `frametime_spitze_ms` | `100` | ab hier gilt eine Frametime als Ruckler |
| `lhm_aufbewahrung_tage` | `0` | LHM-Tagesdateien älter als N Tage löschen (0 = nie) |
| `ausgabeordner` | `""` | fester Ausgabeordner (Umgebungsvariablen und `~` erlaubt) |
| `log_luecke_faktor` | `3.0` | Lücke = Abstand > Faktor × Messintervall |
| `log_luecke_warnung_s` | `10` | ab dieser Lückenlänge Warnung statt Hinweis |
| `lastgrenzen.gpu_prozent` / `cpu_prozent` | `80` / `60` | ab hier gilt ein Messpunkt als „unter Last“ |
| `ereignisse.aktiv` | `true` | Ereignisprotokoll abfragen |
| `ereignisse.minuten_vor_start` / `minuten_nach_ende` | `2` / `60` | Zeitfenster um das Log |

## Empfohlene HWiNFO-Einstellungen

- Sensor „Windows Hardware Errors (WHEA)“ aktivieren
- DIMM-Sensoren (SPD-Hub, PMIC) aktivieren – sonst ist ein Kanalausfall im Log nicht erkennbar
- GPU-PCIe-Fehlerzähler und „PCIe Link Speed“ mitloggen
- für Spiele: PresentMon-Werte (Framerate, Frametime 1 %/0,1 % high)
- Logging regulär beenden, damit der Logabschluss geschrieben wird

## Sicherheit beim Admin-Modus

Weil das Skript optional mit Adminrechten läuft, sind einige Vorsichtsmaßnahmen eingebaut:

- Der erhöhte Prozess startet Python mit `-I` (isolierter Modus), damit keine untergeschobenen Module aus dem
  Skriptordner geladen werden.
- PowerShell wird über den absoluten Pfad aus `GetSystemDirectoryW` gestartet, nicht über `PATH`.
- Ausgabeordner, die Junction/Symlink sind, werden mit Adminrechten abgelehnt.
- Die Ergebnisdatei für den nicht erhöhten Prozess wird nur beschrieben, wenn sie eine leere, reguläre Datei
  ohne Reparse-Point ist.
- Das Aufräumen löscht nur Dateien mit exakt dem LHM-Namensmuster, keine Links, nur direkt im Ordner.
- Der Browser wird vom **nicht** erhöhten Prozess geöffnet.

## Bekannte Einschränkungen

- Das Ereignisprotokoll ist nur aussagekräftig, wenn das Skript auf dem PC läuft, der das Log geschrieben hat.
- Die Zeitumstellung wird nur für die EU-Regel (letzter Sonntag im März/Oktober) erkannt.
- Board-Spannungssensoren sind oft 1–2 % ungenau; aussagekräftig sind Einbrüche unter Last oder Abweichungen,
  die auch die Grafikkarte misst.
- Einige Erklärtexte im Bericht sind noch auf das ursprüngliche System zugeschnitten (7800X3D, 320-W-GPU,
  Ausfall von RAM-Kanal B, 2-s-Messintervall).
- `--neu` liefert Exitcode 0, wenn nichts Neues ausgewertet wurde – unabhängig vom Status früherer Logs.
