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

## Referenzsystem

Auf dieses System sind die Standard-Grenzwerte abgestimmt:

| Komponente | Modell | relevante Grenze im Tool |
|---|---|---|
| CPU | AMD Ryzen 7 7800X3D (AM5) | `cpu_x3d` 80/85/89 °C, `vcore_max_x3d` |
| Mainboard | Gigabyte B850 AORUS Elite WIFI7, Rev. 1.1 (seit 02.10.2026, vorher ASRock X670E Pro RS) | IT8696E-Profil für LHM, VRM/Chipsatz |
| RAM | 64 GB (2× 32 GB) Corsair Vengeance DDR5-6000 CL30, EXPO 1,40 V | `erwartete_riegel` 2, `ram_gb_erwartet`, `ram_abweichung`, `ram_max`, `soc_max` |
| GPU | Gigabyte GeForce RTX 4080 SUPER Aero OC 16G (320 W, GDDR6X) | `gpu_kern` 90 °C max., `gpu_speicher` |
| SSD (Windows) | SanDisk Ultra 3D 500 GB, SATA (SDSSDH3 500G) | `sata` 55/65/70 °C (Spezifikation bis 70 °C) |
| SSD (Daten) | WD_BLACK SN770 2 TB, NVMe | `nvme` 70/80/85 °C (Spezifikation bis 85 °C) |
| Netzteil | MSI MAG A850GL PCIE5, 850 W | Schienen ±3/4/5 %, 12V-2x6 an der GPU |
| Kühlung | Arctic Liquid Freezer III 240, 3× Arctic P14 PWM PST | Lüfterstillstand (CPU/Pumpe kritisch) |
| Gehäuse | be quiet! Pure Base 500DX | – |

Für andere Hardware die Werte per `hwlog_config.json` anpassen.

## Dateien

| Datei | Zweck |
|---|---|
| `hwlog_check.py` | das eigentliche Tool |
| `hwlog_check.bat` | Drag-&-Drop-Starter für Windows |
| `hwinfo_umschalten.bat` / `.ps1` | LHM sauber beenden, HWiNFO starten, nach dem Schließen von HWiNFO wieder LHM starten (mit Markierungen) |
| `markieren.bat` | eigene Beobachtung mit Uhrzeit festhalten (Bildaussetzer, Fehlermeldung, Absturz, LED-Zustand) |
| `hwlog_config.example.json` | Standard-Grenzwerte als Vorlage (erzeugt mit `--config-schreiben`) |
| `tests/` | automatische Tests (nur Standardbibliothek), Testdaten in `tests/fixtures/` |
| `CHANGELOG.md` | Änderungen je Version |
| `.github/workflows/tests.yml` | GitHub Actions: Tests unter Windows und Linux, Python 3.8 und 3.13 |

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
py hwlog_check.py --markieren "Bildaussetzer auf dem Desktop"
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
| `--markieren TEXT` | Beobachtung mit aktueller Uhrzeit in `markierungen.jsonl` speichern (im `ausgabeordner`, sonst `Dokumente\hwlog_berichte`) und beenden |
| `--version` | Version ausgeben |

### Exitcodes

`0` unauffällig · `1` Hinweise · `2` Warnung · `3` kritisch · `4` Fehler (Datei nicht auswertbar, keine Dateien,
unsicherer Ausgabeordner). Bei mehreren Logs gilt der schlechteste Wert, der Systemzustand zählt mit.

## Ausgabe

Ohne `-o` landet alles in `hwlog_berichte\` neben dem ersten Log. Bei LHM-Logs (die meist im Programmordner
liegen, wo nur Admins schreiben dürfen) stattdessen in `Dokumente\hwlog_berichte`. Ein fester Ordner lässt sich
per Konfiguration (`ausgabeordner`) setzen.

| Datei | Inhalt |
|---|---|
| `<log>_bericht.html` | Bericht zu einem Log: Status, Befunde mit Erklärung, Konfiguration, Diagramme, Kennzahlen, Ereignisse, „Letzte Minute vor Logende/Absturz“ |
| `verlauf.html` | Stabilität mit Absturzmuster, Systemzustand, Absturz-/Ereignis-Chronik mit Markierungen, Systemstand je Start, Arbeitsspeicher je Steckplatz, SMART-Zähler und alle Sitzungen mit Trenddiagrammen (u. a. RAM-VIN, -VDD und -Temperatur je Kanal, nutzbarer RAM) |
| `verlauf.jsonl` | Verlaufsdaten (eine Zeile pro Log: Kennzahlen, Hardware-Fingerabdruck, wichtigste Befunde) |
| `ereignisse.jsonl` | dauerhafte Kopie der relevanten Ereignisse (Abstürze, Bluescreens, Starts, Geräte-/Laufwerksfehler) – bleibt erhalten, auch wenn Windows sein Protokoll überschreibt |
| `system.jsonl` | Systemstand pro Aufruf (BIOS, Microcode, Treiber, Windows, BIOS-Zeit, Dump-Einstellung, Geräte mit Fehler, SMART-Zähler) |
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
| RAM-Module | Anzahl erkannter DIMMs vs. erwartet; nutzbarer Arbeitsspeicher gegen den Sollwert (ein nicht eingemessener Kanal halbiert ihn, auch wenn beide Sensorchips weiter antworten); Vergleich der Module untereinander (VIN, VDD, Temperatur – driftet ein Modul weg, fällt das vor festen Grenzwerten auf); PMIC-Fehlerflags; Sensoren eines Moduls verstummen |
| Laufwerke | SMART-Fehler/-Warnung, Restlebensdauer, Temperatur aller Sensoren einer SSD (bei NVMe bis zu drei; Sensor 2 ist meist der Controller). Meldet die SSD eigene Grenzen (NVMe-Warn-/Kritisch-Temperatur, bei LHM), deckeln diese die konfigurierten Werte. NVMe-SSDs mit mehreren Sensoren bekommen ein eigenes Diagramm mit Grenzlinien; im Verlauf gibt es die Spalte „SSD max“ |
| Lüfter | Stillstand (kritisch bei CPU/Pumpe/AIO unter Wärme), GPU-Lüfter steht bei ≥ 72 °C |
| Frametimes | Spitzen > `frametime_spitze_ms`, getrennt nach Spiel und Lade-/Menüphasen (PresentMon, dwm.exe wird erkannt) |
| Sensor-Aussetzer | zentrale Sensoren liefern zeitweise oder bis Logende keine Werte |
| Logende / Absturz | Zustand beim letzten Messpunkt (Last/Leerlauf); Spannungseinbruch (12/5/3,3 V, GPU-12 V, RAM-VIN) in den 30 s vor Logende und vor jedem Absturz in LHM-Logs; Hinweis, wenn ein Absturz weniger als 2 min nach Start der Aufzeichnung kam (möglicher Auslöser: das Auslesen der Sensoren selbst) |
| Stromverlust | Einschaltzähler der SSDs über eine Lücke oder einen Absturz hinweg: gestiegen = PC war stromlos (Ausschalten, Schutzabschaltung des Netzteils), gleich = Hänger mit Reset (LHM-Logs) |
| Markierungen | eigene Markierungen aus `markieren.bat` im Zeitraum des Logs als Befund und als Linie in den Diagrammen |
| Konfiguration | Fingerabdruck (CPU, Board, GPU, RAM-Module/-Takt/-Timings, FCLK, UCLK-Verhältnis, SoC/VDDIO/VDD, PCIe-Gen, GPU-Limit, Laufwerke, RAM-Profil) und Vergleich mit dem vorherigen Log derselben Quelle – fällt z. B. auf, wenn ein BIOS-Update EXPO zurückgesetzt hat. Das RAM-Profil (EXPO/XMP an oder aus) wird aus VDDIO_MEM, RAM-VDD und SoC-Spannung abgeleitet, weil LHM keinen RAM-Takt liefert |
| Ereignisprotokoll | Kernel-Power 41 (mit BugcheckCode/Einschaltknopf), EventLog 6008, Bluescreens (WER 1001), WHEA-Logger, Display 4101/nvlddmkm/amdkmdag, Datenträgerfehler, Laufwerk unerwartet entfernt (disk 157), Geräteprobleme (Kernel-PnP 411/219/225), CPU-Takt durch Firmware begrenzt (Kernel-Processor-Power 37), Ergebnis der Windows-Speicherdiagnose, Programmabstürze, LiveKernelEvents |

**Absturzerkennung bei HWiNFO:** Fehlt der Logabschluss und folgt nach Logende ein Kernel-Power 41, wird der
Befund auf „Absturz“ hochgestuft. Der Bericht zeigt dann die letzte Minute vor Logende als Tabelle.

**LiveKernelEvents:** Windows meldet alte Berichte wiederholt, bis sie hochgeladen sind. Das Tool gleicht deshalb
mit den Zeitstempeln der Dump-Dateien in `C:\Windows\LiveKernelReports` ab (dafür braucht es Adminrechte) und
trennt echte neue Ereignisse von Wiederholungen.

## Systemzustand (bei jedem Aufruf)

Unabhängig von den Logs prüft das Tool bei jedem Aufruf unter Windows den Zustand des PCs und legt die Ergebnisse
dauerhaft im Ausgabeordner ab. Die Befunde stehen in der Konsole und oben in `verlauf.html`.

- **Ereignis-Chronik:** Das Ereignisprotokoll wird seit der letzten Auswertung durchsucht (beim ersten Mal 14 Tage
  zurück), nicht nur rund um die Logs. So fallen auch Abstürze vor der Anmeldung, beim Herunterfahren oder ohne
  laufendes Messprogramm auf. Kernel-Power 41, EventLog 6008 und BugCheck eines Neustarts werden zu einem Vorfall
  zusammengefasst, mit Bluescreen-Code bzw. „ohne Bluescreen“. Drei oder mehr Systemstarts innerhalb von 5 min
  werden als Bootschleife gemeldet. Gleiche Ereignisse kurz hintereinander (z. B. zehn Grafiktreiber-Resets beim
  Treiberwechsel) erscheinen als eine Zeile mit Anzahl und Zeitraum.
- **Stabilität:** letzter Absturz, Tage ohne Absturz, Abstürze in 7/30 Tagen, Starts seit dem letzten Absturz.
- **Absturzmuster:** Abstürze nach Art gezählt (Bluescreen-Code bzw. „ohne Bluescreen“), speichertypische Codes
  (z. B. 0x1A, 0x50, 0x139) markiert. Mindestens drei Abstürze in 30 Tagen mit wechselnden Arten ergeben eine Warnung:
  Ein fehlerhafter Treiber stürzt meist immer gleich ab, kippende Bits treffen zufällig irgendwo.
- **Arbeitsspeicher laut Windows:** Steckplatz, Kanal, Größe, Seriennummer und Takt jedes Moduls, so wie das BIOS
  sie nach dem Einmessen meldet. Fehlt ein Modul oder Kanal oder liegt der RAM unter dem Sollwert, ist das kritisch
  – auch ohne laufendes Log. Werden Module umgesteckt, zeigt die Tabelle im Verlauf über die Seriennummern, ob ein
  Fehler dem Modul oder dem Steckplatz folgt. Das geht nur mit echten Seriennummern: Viele Module melden
  `00000000` – dann bleibt die Spalte leer und „umgesteckt“ wird nicht geprüft. Ein geänderter RAM-Takt (EXPO an/aus) wird als Systemstand-Änderung
  gemeldet.
- **Windows-Speicherdiagnose** (`mdsched.exe`): das Ergebnis erscheint in der Chronik, „Hardwarefehler“ ist kritisch.
- **Systemstand:** BIOS-Version, Mainboard, CPU, Microcode, Grafiktreiber und Windows-Build – Änderungen gegenüber
  der letzten Auswertung werden gemeldet. Liefert Windows keinen Microcode, entfällt die Spalte
  (er kommt ohnehin mit dem BIOS).
- **BIOS-Zeit beim Start** (wie „Letzte BIOS-Zeit“ im Taskmanager): Über `post_zeit_hinweis_s` (60 s) deutet sie auf
  ein neues Memory Training hin.
- **Speicherabbilder:** neue Dateien in `C:\Windows\Minidump` und `MEMORY.DMP`; Warnung, wenn Windows keine
  Speicherabbilder schreiben darf oder keine Auslagerungsdatei hat.
- **Geräte mit Fehler** im Gerätemanager (z. B. Grafikkarte mit Code 43).
- **SMART-Fehlerzähler** über `smartctl` aus den kostenlosen [smartmontools](https://www.smartmontools.org/), falls
  installiert: Gesamtstatus, kritische Warnung, Medienfehler/ersetzte Sektoren, CRC-Fehler, unsichere Abschaltungen
  (jeder harte Absturz zählt mit), Einschaltvorgänge. Gemeldet werden neue Fehler seit der letzten Auswertung.
  Braucht Adminrechte (`--admin`).

Mit `--kein-verlauf` entfällt dieser Teil, mit `--keine-ereignisse` die Chronik.

### Eigene Beobachtungen markieren

Bildaussetzer, Fehlermeldungen oder ein Absturz mit LED-Zustand stehen in keinem Log. `markieren.bat` per Doppelklick
fragt nach einer Notiz und speichert sie mit Uhrzeit; alternativ `markieren.bat Bildaussetzer auf dem Desktop`. Die
Markierung erscheint im Bericht des passenden Logs als Befund und als Linie in den Diagrammen, außerdem in der
Chronik im Verlauf. Gespeichert wird in `markierungen.jsonl` im konfigurierten `ausgabeordner`, sonst in
`Dokumente\hwlog_berichte` – unabhängig davon, wo das Skript liegt. Markierungen aus älteren Versionen (neben dem
Skript) werden weiter mitgelesen.

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
- LHM liefert keinen RAM-Takt; ob EXPO aktiv ist, schätzt das Tool aus den Spannungen.
- Wird das Log vor der Auswertung aus dem LHM-Ordner kopiert, fehlt `LibreHardwareMonitor.config` daneben; ein von Hand beendetes LHM erscheint dann nur als „Unterbrechung ohne Ereignis“.
- `lhm_aufbewahrung_tage` / `--aufraeumen` löscht alte Tagesdateien.

## HWiNFO oder LHM: was HWiNFO-Logs zusätzlich bringen

Mit HWiNFO-Logs prüft das Tool zusätzlich:

- **WHEA-Zähler** live im Log, nicht nur nachträglich aus dem Ereignisprotokoll
- **GPU-PCIe-Fehler** (Correctable, Replay, NAK, LCRC, Fatal/Non-Fatal) mit Bewertung unter Volllast, dazu
  Link-Geschwindigkeit und PCIe-Generation
- **RAM je Modul:** VDD und VIN der Spannungsregler auf den Modulen (PMIC)
- **RAM-Konfiguration:** gemessener Takt, Timings, FCLK, UCLK:MEMCLK, VDDIO_MEM – EXPO wird sicher erkannt statt
  aus den Spannungen geschätzt
- **GPU-Versorgung:** 12 V am 12VHPWR-Stecker und am PCIe-Slot, also Einbrüche direkt an der Karte
- **Frametimes/FPS** über PresentMon, mit Unterscheidung zwischen Ladebildschirm und echtem Hänger
- **Logabschluss:** fehlt er, ist der Absturz eindeutig belegt, ohne Rückschluss aus Lücken
- **weitere Temperaturen:** GPU-Speicher (Junction) und der Hotspot im I/O-Teil der CPU

LHM läuft dafür dauerhaft ohne manuellen Start, und das Tool ordnet Lücken als Neustart, Standby oder Absturz
ein. Bewährt: LHM als Dauerlogger, HWiNFO zusätzlich beim Spielen und für gezielte Tests (in der Free-Version
das Logging von Hand starten). Laufen beide gleichzeitig, auf unsinnige DIMM-Werte achten (0 °C, 255 °C,
VIN 0 V) – das deutet auf Kollisionen auf dem gemeinsamen SMBus.

### Umschalten zwischen LHM und HWiNFO

LHM und HWiNFO sollten nicht gleichzeitig laufen: Beide lesen die RAM-Sensoren über denselben Bus (SMBus), dabei
gibt es Lesefehler – HWiNFO blendet dann z. B. den Sensor eines RAM-Moduls aus. `hwinfo_umschalten.bat`
1. setzt eine Markierung (damit die Lücke im LHM-Log erklärt ist),
2. beendet LibreHardwareMonitor sauber (wie „Beenden“; nur wenn LHM nach 15 s noch läuft, hart),
3. startet HWiNFO und wartet, bis du HWiNFO schließt,
4. startet LibreHardwareMonitor wieder und setzt eine zweite Markierung.

Mit `hwinfo_umschalten.bat ohne-rueckkehr` bleibt LHM aus. Es fragt per UAC nach Adminrechten. Desktop-Verknüpfung
mit HWiNFO-Symbol anlegen (einmalig):

```
powershell -NoProfile -ExecutionPolicy Bypass -File hwinfo_umschalten.ps1 -VerknuepfungAnlegen
```

Voraussetzungen: HWiNFO liegt unter `C:\Program Files\HWiNFO64\HWiNFO64.EXE` (sonst `-HWiNFO "Pfad"` an die
`.ps1` übergeben), in HWiNFO ist „Auto Start“ aus, und in LHM ist „Minimize On Close“ aus – sonst schließt LHM
beim sauberen Beenden nur ins Tray und wird nach 15 s hart beendet.

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
| `ram_gb_erwartet` | `0` | erwarteter Arbeitsspeicher in GB; 0 = automatisch der größte bisher von Windows gemeldete Wert |
| `ram_abweichung.vin` / `vdd` / `temp` | `[0.08,0.15,0.30]` / `[0.03,0.06,0.10]` / `[8,15,25]` | erlaubte Abweichung zwischen den Modulen im selben Log (V bzw. °C) |
| `temperaturen.cpu` | `[85, 89, 95]` | °C, Tctl/Tdie |
| `temperaturen.cpu_x3d` | `[80, 85, 89]` | °C, gilt statt `cpu` für X3D-Modelle (drosseln bei 89 °C) |
| `temperaturen.gpu_kern` / `gpu_hotspot` / `gpu_speicher` | `[80,87,90]` / `[95,105,110]` / `[90,100,105]` | °C |
| `temperaturen.ram` | `[65, 75, 85]` | °C, SPD-Hub |
| `temperaturen.nvme` / `sata` | `[70,80,85]` / `[55,65,70]` | °C |
| `temperaturen.vrm` / `chipsatz` | `[90,105,115]` / `[75,85,95]` | °C |
| `schienen_toleranz_prozent` | `[3, 4, 5]` | Abweichung der 12/5/3,3-V-Schienen vom Sollwert |
| `spannungen.soc_max` | `[1.28, 1.30, 1.35]` | V; EXPO setzt oft genau 1,25 V, AMD-Obergrenze 1,30 V |
| `spannungen.vcore_max` / `vcore_max_x3d` | `[1.45,1.50,1.55]` / `[1.30,1.35,1.40]` | V, X3D wird am CPU-Namen erkannt |
| `spannungen.ram_max` | `[1.43, 1.45, 1.50]` | V, VDD/VDDQ/VDDIO_MEM; EXPO 6000 CL30 = 1,40 V |
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
| `ereignisse.chronik_tage` | `14` | wie weit die Ereignis-Chronik beim ersten Aufruf zurückschaut |
| `post_zeit_hinweis_s` | `60` | BIOS-Zeit beim Start, ab der ein Hinweis kommt (0 = aus) |
| `smartctl_pfad` | `""` | Pfad zu `smartctl.exe`; leer = `C:\Program Files\smartmontools\bin\smartctl.exe` |

## Empfohlene HWiNFO-Einstellungen

- Sensor „Windows Hardware Errors (WHEA)“ aktivieren
- DIMM-Sensoren (SPD-Hub, PMIC) aktivieren – sonst ist ein Kanalausfall im Log nicht erkennbar
- GPU-PCIe-Fehlerzähler und „PCIe Link Speed“ mitloggen
- für Spiele: PresentMon-Werte (Framerate, Frametime 1 %/0,1 % high)
- Logging regulär beenden, damit der Logabschluss geschrieben wird
- Autostart über die HWiNFO-eigene Option „Auto Start“ (legt eine Aufgabe mit Adminrechten an); der Autostart-Ordner
  blockiert Programme, die Adminrechte brauchen

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
- `smartctl` wird nur von einem festen Pfad gestartet (Standardinstallation oder `smartctl_pfad`), nicht über `PATH`.

## Bekannte Einschränkungen

- Das Ereignisprotokoll ist nur aussagekräftig, wenn das Skript auf dem PC läuft, der das Log geschrieben hat.
- Die Zeitumstellung wird nur für die EU-Regel (letzter Sonntag im März/Oktober) erkannt.
- Board-Spannungssensoren sind oft 1–2 % ungenau; aussagekräftig sind Einbrüche unter Last oder Abweichungen,
  die auch die Grafikkarte misst.
- `--neu` ohne neue Logs liefert nur den Exitcode des Systemzustands, unabhängig vom Status früherer Logs.
- Die BIOS-Zeit speichert Windows nur für den letzten Start; erfasst wird also der Start vor jeder Auswertung.
- Windows überschreibt das System-Ereignisprotokoll ab 20 MB. Die Chronik sichert relevante Einträge, sobald das
  Tool läuft; zusätzlich lässt sich das Protokoll vergrößern (`wevtutil sl System /ms:104857600` = 100 MB).
- Nicht messbar: No-POST, LED-Zustände, Memory Training vor Windows (nur indirekt über die BIOS-Zeit),
  Spannungsspitzen unter ca. 1–2 s und Bitfehler im RAM ohne ECC (zeigen sich nur als Bluescreens mit wechselnden
  Codes). Dafür gibt es die Markierungen und regelmäßige Speichertests.
- PMIC-Spannungen der RAM-Module, GPU-12V-Eingang und PCIe-Fehlerzähler liefert nur HWiNFO, nicht LHM.
- Den RAM-Bestand liest das Tool nur beim Aufruf, also für den aktuellen Start. Ein Kanal, der zwischendurch fehlte
  und nach einem Neustart wieder da ist, fällt nur über ein Log aus dieser Zeit (nutzbarer RAM) oder eine Markierung
  auf. Den Kanal leitet das Tool aus der Steckplatz-Bezeichnung des BIOS ab (`P0 CHANNEL B`, `DIMM_B2` …); kennt es sie
  nicht, steht nur der Steckplatz da.

## Tests

```
py -m unittest discover -s tests -t .
```

Nur Standardbibliothek, kein pytest nötig. Die Tests erzeugen HWiNFO-Logs im echten Format (mit und ohne
Logabschluss, mit NUL-Bytes wie nach einem Absturz) und nutzen einen Ausschnitt eines echten LHM-Logs mit Absturz
(`tests/fixtures/`). Windows-Abfragen werden in den meisten Tests ersetzt, damit sie überall gleich laufen;
`tests/test_windows_live.py` führt sie unter Windows echt aus und prüft die Form der Antworten. GitHub Actions
startet alles bei jedem Push unter Windows und Linux.

| Datei | Prüft |
|---|---|
| `test_hwinfo.py` | Logabschluss, WHEA, 12-V-Einbruch, X3D-Grenzen, RAM: Modulvergleich, halber Speicher trotz zwei Sensoren, fehlendes Modul, PMIC-Flag, Kennzahlen je Kanal |
| `test_lhm.py` | LHM-Erkennung, Board-Profil IT8696E, Kanalzuordnung, Absturz um 17:52 mit und ohne Ereignisprotokoll |
| `test_events.py` | Einordnung von Ereignissen (Kernel-Power 41, Bluescreens, Speicherdiagnose, LiveKernelEvents), Vorfälle, Bootschleifen, Zusammenfassen, Absturzmuster, Microcode, Treiberversion, Kanal aus der BIOS-Bezeichnung |
| `test_system.py` | Systemzustand über zwei Aufrufe: Kanal B fällt aus, Module umgesteckt, Platzhalter-Seriennummern, schwankende Startzeit, neue Abstürze, Speicherdiagnose, SMART, Gerätefehler, BIOS-Wechsel, Markierungen |
| `test_cli.py` | Exitcodes, Berichte, Verlauf und `--neu`, `--config-schreiben`, aktuelle Beispielkonfiguration, `--markieren` |
| `test_windows_live.py` | nur Windows: echte PowerShell-Abfragen und ihre Antwortform |
