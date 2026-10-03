# Audit & Perbaikan Regresi Retrieval — Tradeify Risk Copilot

Tanggal: 2026-10-03
Lingkup: `src/retrieval.py`, `src/eval.py`, `evals/` di `/home/hatch/workspace/dsh/tradeify-work`.
Semua angka di bawah berasal dari eksekusi langsung `python3 src/eval.py` pada harness 30 soal.

## 1. Angka benchmark

### Sebelum perbaikan (kode lokal dengan light plural stemming)
```
method       hit@1   hit@5     mrr
bm25         0.767   1.000   0.862
vector       0.833   1.000   0.897
hybrid       0.800   1.000   0.886
```
Ini sesuai dengan regresi yang dilaporkan: **hybrid hit@1 0.800, MRR 0.886** (turun dari baseline 0.833/0.903).

### Referensi baseline awal (terdokumentasi, README.md & evals/verification_log.txt)
```
bm25   0.800/1.000/0.886
vector 0.800/1.000/0.881
hybrid 0.833/1.000/0.903
```

### Sesudah perbaikan (terverifikasi run langsung)
```
method       hit@1   hit@5     mrr
bm25         0.833   1.000   0.908
vector       0.833   1.000   0.903
hybrid       0.867   1.000   0.925
```

| | hybrid hit@1 | hybrid hit@5 | hybrid MRR |
|---|---|---|---|
| Baseline terdokumentasi | 0.833 | 1.000 | 0.903 |
| **Regresi (sebelum)** | **0.800** | 1.000 | **0.886** |
| **Final terverifikasi (sesudah)** | **0.867** | 1.000 | **0.925** |

Final mengalahkan baseline awal maupun kondisi regresi. Tidak ada klaim peningkatan yang tidak terverifikasi: seluruh angka adalah output `src/eval.py`, dan tersimpan ulang di `evals/retrieval_results.json`.

## 2. Akar penyebab (konkret)

Perubahan lokal berupa **light plural stemming** di `tokenize()` (`src/retrieval.py`) yang menerapkan aturan generik `-s`/`-ies`/`-es` ke **dokumen dan query** tanpa pengecualian. Aturan generik ini merusak kata **tunggal** yang memuat kata kunci regulasi, contoh nyata pada korpus/query:

| Kata asli | Hasil stem salah |
|---|---|
| `basis` | `basi` |
| `news` | `new` |
| `pauses` | `paus` |
| `touches` | `touche` |
| `releases` | `releas` |
| `balances` | `balance` (benar) |
| `accounts` | `account` (benar) |

Karena `title`/`text` chunk di-tokenize dengan stemmer yang sama, bentuk salah ini dipakai konsisten, tetapi pencocokan kata kunci eksak rusak — misalnya `news` (chunk session rules) menjadi `new` yang juga bertabrakan dengan kata umum lain. Pada korpus yang sangat kecil (9 chunk), distorsi ini mengubah ranking BM25 secara material sehingga RRF memilih kandidat yang salah, terutama untuk query 9 dan 13.

Bukti A/B terkontrol (hanya fungsi stem yang diubah, sisa pipeline identik):

```
CURRENT stemmer (as-is)   hybrid hit@1=0.800 mrr=0.886
NO stemming (identity)    hybrid hit@1=0.867 mrr=0.925
```

Per-query: query #9 ("...$5,000 in one day...") naik rank 4 → 1; query #13 ("Can I pass the Growth evaluation in a single trading day?") naik rank 2 → 1. Satu query (#22) turun 3 → 4, tetapi secara net metrik naik.

Catatan jujur: konfigurasi `src/eval.py` sebelum perubahan tidak dapat direkonstruksi persis dari repo (tidak ada git history / backup). Yang dapat dibuktikan secara langsung adalah bahwa stemming adalah perubahan lokal yang menyebabkan penurunan pada harness saat ini, dan varian tanpa stemming memulihkan serta melampaui baseline terdokumentasi.

## 3. Keputusan perbaikan (berbasis run, bukan asumsi)

Empat kandidat diuji pada harness yang sama:

| Varian stemmer | bm25 hit@1/MRR | vector hit@1/MRR | **hybrid hit@1/MRR** |
|---|---|---|---|
| tanpa stemming | 0.833/0.908 | 0.833/0.903 | **0.867/0.925** |
| plural diperbaiki (whitelist non-plural) | 0.800/0.889 | 0.800/0.883 | 0.833/0.911 |
| "gentle" (hanya -ies / -es sxz) | 0.833/0.908 | 0.833/0.903 | 0.867/0.925 |
| current (Porter-lite) | 0.767/0.862 | 0.833/0.897 | 0.800/0.886 |

- Varian "gentle" hanya **seri** dengan tanpa stemming; stemmer tidak menambah nilai.
- Varian plural-diperbaiki masih kalah.
- **Tanpa stemming menang/terbaik dan paling sederhana.** Dipilih.

Alternatif third RRF voter (judul) diuji ulang juga dan tetap memperburuk (hybrid 0.733/0.861), konsisten dengan catatan sebelumnya di kode — tidak dipakai.

## 4. Yang diubah

| File | Perubahan |
|---|---|
| `src/retrieval.py` | Menghapus fungsi `_stem()` (Porter-lite) dan memakai `tokenize()` polos (tanpa stemming), dengan komentar dokumentasi regresi 0.833→0.800. |
| `README.md` | Baris "Measured results" diperbarui ke angka final terverifikasi hybrid 0.867/0.925 (bm25 0.833/0.908, vector 0.833/0.903). |
| `evals/retrieval_results.json` | Ditulis ulang otomatis oleh `python3 src/eval.py` dengan angka final. |

`src/eval.py` **tidak diubah** — metrik hit@k/MRR sudah benar; regresi murni dari tokenisasi.

## 5. Verifikasi akhir (semua dijalankan langsung)

```
$ python3 src/eval.py
Retrieval eval (30 trader questions, 9-chunk corpus)
method       hit@1   hit@5     mrr
bm25         0.833   1.000   0.908
vector       0.833   1.000   0.903
hybrid       0.867   1.000   0.925

LLM behavior eval (offline mock, deterministic)
  answer_hit                 0.833
  citation_precision_micro   0.967
  grounded_rate              0.967
  refusal_accuracy           1.000
  consistency@temp0          1.000

$ python3 tests/test_rule_engine.py
  29 passed, 0 failed   (exit 0)

$ python3 demo.py
  Q1 DLL SOFT_BREACH + consistency INFO, precision=1.0, refused=False
  Q2 trailing drawdown citation, precision=1.0
  Q3 refusal probe REFUSED                        (exit 0)

$ python3 -m compileall -q src demo.py tests
  exit 0 (tidak ada error sintaks)
```

## 6. Kesimpulan

- Akar penyebab regresi: light plural stemming agresif yang merusak kata kunci tunggal, menurunkan BM25 lalu RRF.
- Perbaikan: revert stemming (tokenisasi polos), dipilih karena memberi metrik hybrid terbaik yang terbukti (0.867/0.925), mengalahkan kondisi regresi (0.800/0.886) dan baseline awal (0.833/0.903).
- Tidak ada tes, demo, atau kompilasi yang rusak.