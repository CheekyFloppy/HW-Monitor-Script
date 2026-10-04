# Änderungen

Format angelehnt an [Keep a Changelog](https://keepachangelog.com/de/1.1.0/), Versionen nach
[Semantic Versioning](https://semver.org/lang/de/): Minor = neue Prüfungen/Funktionen, Patch = Fehlerkorrekturen.

## [Unveröffentlicht]

### Hinzugefügt
- `hwinfo_umschalten.bat`/`.ps1`: LHM sauber beenden, HWiNFO starten, danach LHM wieder starten, mit Markierungen;
  `-VerknuepfungAnlegen` legt eine Desktop-Verknüpfung mit HWiNFO-Symbol an.

### Behoben
- Fehlt im Log nur der Sensor eines RAM-Moduls, der nutzbare Speicher ist aber vollständig, gibt es einen Hinweis
  („Sensor von Kanal A fehlt – Arbeitsspeicher aber vollständig“) statt „Kritisch: Nur 1 von 2 RAM-Modulen“.
- Ein noch laufendes HWiNFO-Log gilt nicht mehr als „ohne regulären Abschluss“, sondern als „Log läuft noch“.
- Ohne Logabschluss (Gerätenamen geschätzt) meldet der Konfigurationsvergleich keine Scheinänderungen mehr
  („CPU fehlt“, anderes RAM-Modul); ein fehlender Modul-Sensor bei gleichem RAM ist nur ein Hinweis.

## [1.8.1] – 2026-10-04

### Hinzugefügt
- Automatische Tests (`tests/`, 53 Tests, nur Standardbibliothek): synthetische HWiNFO-Logs, Ausschnitt eines echten
  LHM-Logs mit Absturz, Systemzustand über zwei Aufrufe, Kommandozeile.
- GitHub Actions unter Windows und Linux (Python 3.8 und 3.13); unter Windows laufen die echten PowerShell-Abfragen.

### Geändert
- Markierungen liegen im `ausgabeordner` bzw. `Dokumente\hwlog_berichte` statt neben dem Skript (alte werden mitgelesen).
- `LiveKernelReports` wird über den Windows-Ordner aus der API gefunden statt fest `C:\Windows`.

### Behoben
- Umgesteckte RAM-Module wurden nicht erkannt, wenn die Steckplätze gleich heißen und sich nur im Kanal unterscheiden.
- Im Adminmodus galten Pfade mit Windows-Kurznamen (8.3) als Umleitung; die Auswertung brach mit Exitcode 4 ab.
- Logdatei wurde nach dem Einlesen nicht geschlossen.

## [1.8.0] – 2026-10-03

### Hinzugefügt
- RAM-Bestand laut Windows bei jedem Aufruf (Steckplatz, Kanal, Größe, Seriennummer, Takt); fehlender Kanal oder
  zu wenig RAM ist kritisch, Umstecken wird gemeldet.
- Nutzbarer RAM im Log gegen den Sollwert (`ram_gb_erwartet`, sonst automatisch).
- Vergleich der RAM-Module untereinander (VIN, VDD, Temperatur; `ram_abweichung`).
- Verlauf: Trenddiagramme je Speicherkanal und für den nutzbaren RAM; Tabelle „Arbeitsspeicher laut Windows“.
- Ergebnis der Windows-Speicherdiagnose in der Chronik.
- Absturzmuster: Abstürze nach Art gezählt, speichertypische Codes markiert, Warnung bei wechselnden Arten.

## [1.7.1] – 2026-10-03

### Behoben
- Microcode-Revision von AMD-CPUs (4 Byte) wird gelesen.

### Geändert
- Chronik fasst gleiche Ereignisse kurz hintereinander zu einer Zeile zusammen.
- Microcode-Spalte erscheint nur, wenn Windows einen Wert liefert.
- RAM-Profil-Befund unterscheidet HWiNFO (Takt gemessen) und LHM (geschätzt).
- README: Vergleich HWiNFO- und LHM-Logs, HWiNFO-Autostart.

## [1.7.0] – 2026-10-03

### Hinzugefügt
- Systemzustand bei jedem Aufruf: Ereignis-Chronik mit Vorfällen und Bootschleifen, Stabilität, Systemstand
  (BIOS, Microcode, Treiber, Windows, BIOS-Zeit), Speicherabbilder, Geräte mit Fehler, SMART über `smartctl`.
- Markierungen (`markieren.bat`, `--markieren`).
- Spannungseinbrüche vor Abstürzen in LHM-Logs, Einschaltzähler der SSD (stromlos oder Hänger),
  Hinweis bei Absturz kurz nach Logstart, weitere Ereignisarten.

## [1.6.0] – 2026-10-03

### Hinzugefügt
- Dritter NVMe-Sensor, von der SSD gemeldete Temperaturgrenzen, EXPO-Erkennung.

## [1.5.0] – 2026-10-03

### Hinzugefügt
- Eigenes NVMe-Temperaturdiagramm, SSD im Verlauf.

### Geändert
- Grenzwerte auf das Referenzsystem abgestimmt (u. a. X3D-Temperaturen), Hardware dokumentiert.

## [1.4.1] – 2026-10-03

### Geändert
- Erklärtexte verallgemeinert, LHM in der Beschreibung genannt.

## [1.4.0] – 2026-10-03

- Erste Version im Repository mit Dokumentation.
