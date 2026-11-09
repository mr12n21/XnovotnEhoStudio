# Novotného Sound Studio

Smyčkový mixer pro skládání techna a elektroniky ze zvuků z [freesound.org](https://freesound.org).

Princip je stejný jako s otevřenými taby v prohlížeči: **každý tab = jedna kategorie**.
Aplikace z tabu stáhne zvuky. Každý zvuk pak běží v nekonečné smyčce srovnané na takty
a zvuky i celé kategorie se dají libovolně zapínat, vypínat a míchat. Výsledek se nahraje
živě nebo vyexportuje do WAV.

## Spuštění

```bash
./run.sh
```

Při prvním spuštění se vytvoří `.venv` a nainstaluje se `numpy`, `soundfile`, `PySide6` a `sounddevice`.

**Zvuk:** aplikace funguje hned přes Qt Multimedia (latence ~60 ms). Pro nejnižší latenci
nainstaluj PortAudio a aplikace ho použije automaticky:

```bash
sudo dnf install portaudio
```

## Jak se s tím pracuje

1. **Stáhnout:** vlevo vlož URL tabů z freesound.org, jednu na řádek. Funguje cokoli, kde
   freesound zobrazuje seznam zvuků:
   - tag: `https://freesound.org/browse/tags/?f=tag:%22short%22`
   - hledání: `https://freesound.org/search/?q=techno+kick`
   - pack: `https://freesound.org/people/<autor>/packs/<id>/`

   Každá URL se uloží jako kategorie (název se odvodí z URL, nebo si ho zadáš sám).
   *Stránek* určuje, kolik stránek výsledků se stáhne (15 zvuků na stránku). Zvuky delší
   než *Max. délka* se přeskočí. Stahuje se HQ náhled (OGG ~192 kbit/s) a už stažené
   zvuky se přeskakují, takže stejnou URL jde stahovat opakovaně a přidávat další stránky.
2. **Mixovat:** každá kategorie je ve mixeru jeden sloupec. Kliknutím na zvuk ho zapneš nebo
   vypneš. Zelená = hraje, žlutá = čeká na další dobu/takt, modrá = načítá se.
3. **Play** (mezerník) spustí transport. Všechny smyčky běží synchronně podle BPM.

### Ovládání

| Prvek | Co dělá |
|---|---|
| **BPM** | tempo; všechny smyčky se srovnají na beat grid |
| **Na dobu / Na takt / Okamžitě** | kdy se zapnutí nebo vypnutí zvuku projeví (kvantizace) |
| **M / S** u kategorie | mute / solo celé kategorie |
| **slider** u kategorie / zvuku | hlasitost (až 150 %) |
| **Náh.** | náhodně zapne 3 zvuky z kategorie (rychlé objevování kombinací) |
| **Vyp** | vypne celou kategorii |
| **a1, a4, …** (délka) | délka smyčky v dobách. `auto` zaokrouhlí nahoru na 1/4, 1/2, 1, 2, 4 … 64 dob, `free` = přirozená délka (polyrytmy), nebo pevně N dob |
| **⇔ (fit)** | přetáhne zvuk přesně na délku smyčky, takže loop s jiným tempem sedí (mění výšku tónu jako gramofon). U zvuků s tempem v názvu („…128bpm“) je zapnuté automaticky |
| **Scény 1–8** | klik nebo klávesa 1–8 vyvolá scénu, Ctrl+klik nebo pravé tlačítko uloží aktuální kombinaci zapnutých zvuků |
| **Vše vypnout** (Esc) | panika |
| **● REC** | nahrává výstup živě do `exports/rec_*.wav` (24 bit) i s credits |
| **Export…** | vyrenderuje aktuální stav na N taktů: mix, stopy po kategoriích (stems) a jednotlivé smyčky pro DAW (Ableton, Bitwig, Reaper…) |

Pravé tlačítko na zvuku: otevřít na freesound.org, panorama, **kopírovat do jiné kategorie**
(vlastní výběry typu „kicks“, „perc“, „atmo“), smazat.

Session (zapnuté zvuky, hlasitosti, scény, otevřené sloupce) se ukládá při zavření do
`session.json` a při dalším spuštění se obnoví. *Soubor → Uložit session jako…* ukládá
různé „tracky“.

## Data

```
library/<kategorie>/sounds.json      metadata: autor, licence, URL
library/<kategorie>/<id>_<název>.ogg
exports/                             nahrávky a exporty (+ CREDITS.txt)
```

Do složky kategorie můžeš nakopírovat i vlastní WAV, FLAC nebo MP3 (nebo použít *Importovat soubory…*).

## Licence zvuků

Zvuky na freesound.org mají licence CC0, CC-BY nebo CC-BY-NC. U **CC-BY** musíš při publikaci
uvést autora a u **NC** se zvuk nesmí použít komerčně. Proto každý export i nahrávka obsahuje
`CREDITS.txt` se seznamem použitých zvuků, autorů a licencí.

## Struktura kódu

- `studio/scraper.py` – čtení stránek freesound.org a stahování náhledů
- `studio/library.py` – knihovna na disku, načítání audia
- `studio/engine.py` – mixovací engine (smyčky, kvantizace, limiter, nahrávání, offline render)
- `studio/audio_out.py` – výstup do zvukovky (PortAudio / Qt / null)
- `studio/gui.py` – rozhraní (PySide6)

Proměnné prostředí: `NSS_HOME` (kde jsou `library/` a `exports/`), `NSS_BACKEND=auto|sd|qt|null`.
