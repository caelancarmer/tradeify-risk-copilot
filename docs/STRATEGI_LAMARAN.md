# Strategi Lanjutan Sebelum Melamar Role "Agentic Coder" di Tradeify

Tanggal: 3 Oktober 2026
Disusun untuk: pemilik proyek Tradeify Risk Copilot
Status dokumen: analisis internal, bukan klaim pemasaran

Dokumen ini melanjutkan transkrip DeepSeek (pesan 9 sampai 12) dan menutup empat
pertanyaan yang belum dijawab: (9) model MiMo/MiniMax, (10) jev/laya/clef,
(11) paket final sebelum melamar, (12) gunanya company brain, semantic, dan
retrieval. Semua rekomendasi di bawah mengasumsikan profil pemilik proyek:
lulusan SMK non-CS, hanya memakai HP, tanpa laptop, budget nol untuk tool
berbayar, dan perlu melamar secepatnya. Karena itu setiap langkah wajib gratis,
bisa dijalankan dari HP, dan tidak menuntut GPU.

Aturan kejujuran yang dipakai di seluruh dokumen: jika suatu fakta tidak dapat
diverifikasi, ditulis "tidak terkonfirmasi". Tidak ada kredensial, perusahaan,
model, atau angka eval yang dikarang.

---

## Ringkasan Eksekutif

1. Job posting "Agentic Coder" Tradeify berhasil diverifikasi ulang lewat API
   publik Ashby pada 3 Oktober 2026. Isi requirement masih sama dengan ringkasan
   di transkrip. Tidak ada perubahan arah yang mengubah strategi.
2. Model gratis yang benar-benar terverifikasi saat ini: deepseek-v4-flash:free,
   deepseek-v4.1-flash:free, mimo-v2.5:free, mimo-v2.6-flash:free,
   qwen3.8-flash:free, dan cohere/north-mini-code:free. MiMo layak menjadi
   kandidat routing. "laya" dan "clef/clef flash" tidak terkonfirmasi.
3. Company brain belum layak dibangun sekarang. Untuk portfolio lamaran,
   rule engine deterministik plus retrieval hybrid yang terbukti lebih
   meyakinkan daripada klaim company brain tanpa data perusahaan.
4. Semantic atau vector search bukan pembeda utama pada korpus kecil yang
   terstruktur. Yang menang adalah hybrid BM25 + vector + RRF.
5. Paket final sebelum melamar: README dengan angka terverifikasi (sudah ada),
   eval harness (sudah ada), arsitektur routing (sudah ada, perlu diselaraskan),
   demo yang bisa dicoba (belum ada, ini prioritas tertinggi berikutnya),
   dan repo GitHub publik dengan CI gratis (belum ada).
6. Batas jujur: fine-tune belum dijalankan, demo masih memakai mock LLM,
   integrasi broker masih stub, dan belum ada deployment publik.

---

## Bagian 1. Model Routing dengan Model Gratis Saat Ini

### 1.1 Daftar model yang terverifikasi

Verifikasi dilakukan pada 3 Oktober 2026 melalui katalog OpenRouter
(https://openrouter.ai/api/v1/models) dan endpoint per model, serta katalog
model internal DSH yang sedang dipakai sesi ini. Hasilnya:

| ID model gratis | Terverifikasi | Catatan teknis dari deskripsi resmi | Lisensi |
|---|---|---|---|
| deepseek-v4-flash:free | Ya (entri model ada di katalog OpenRouter dan katalog DSH) | MoE 284B total, 13B aktif, konteks 1 juta token, dioptimalkan untuk inferensi cepat | tidak terkonfirmasi di sumber yang dicek |
| deepseek-v4.1-flash:free | Ya (entri model ada di katalog OpenRouter dan katalog DSH) | MoE sparse, arsitektur CED, 8B aktif saat input dan 16B saat output, multimodal teks+gambar | MIT (tercatat di README proyek; belum diverifikasi ulang independen) |
| mimo-v2.5:free | Ya (entri model ada di katalog OpenRouter dan katalog DSH) | Xiaomi MiMo-V2.5, omnimodal (teks, gambar, audio, video), performa agentic kelas Pro dengan biaya inferensi sekitar setengah | tidak terkonfirmasi di sumber yang dicek |
| mimo-v2.6-flash:free | Ya (entri model ada di katalog OpenRouter dan katalog DSH) | MoE 309B total, 15B aktif per token, hybrid attention, disebut open-source | disebut open-source oleh deskripsi penyedia; teks lisensi belum diverifikasi |
| qwen3.8-flash:free | Ya (entri model ada di katalog OpenRouter dan katalog DSH) | Model reasoning multimodal Qwen, untuk coding, alur agentic, analisis dokumen dan codebase, video panjang | Apache 2.0 (keluarga Qwen, tercatat di transkrip; belum diverifikasi ulang) |
| cohere/north-mini-code:free | Ya, termasuk endpoint gratis aktif (harga prompt 0, completion 0, uptime sekitar 97 persen saat dicek) | Model coding agentic pertama Cohere, sparse MoE 30B total dengan 3B aktif, mendukung tools dan tool_choice | tidak terkonfirmasi di sumber yang dicek |

Catatan penting soal ketersediaan: beberapa endpoint gratis (deepseek-v4.1-flash:free,
mimo-v2.5:free, mimo-v2.6-flash:free, qwen3.8-flash:free) saat dicek per model
mengembalikan daftar endpoint yang kosong, meskipun entri modelnya ada di
katalog gratis. Artinya model-model ini terdaftar gratis, tetapi ketersediaan
endpoint dapat berubah dari waktu ke waktu dan perlu dicek saat pemanggilan.
Layanan gratis juga umumnya punya batas laju (rate limit) yang tidak
diverifikasi di sini. Jangan menjanjikan kestabilan endpoint gratis di lamaran.

### 1.2 Pembagian layer dan alasannya

Prinsipnya sama dengan yang sudah tertulis di `src/model_router.py`: jangan
kirim semua tugas ke model terbesar. Namun kode router saat ini menyebut nama
model seperti qwen3.6-35b-a3b, qwen2.5-7b-instruct, kimi-k3, dan glm-5.3, yang
bukan daftar gratis yang terverifikasi. Rekomendasi berikut menyelaraskannya
dengan model gratis yang benar-benar tersedia.

| Layer | Model utama (gratis) | Cadangan | Alasan konkret |
|---|---|---|---|
| Klasifikasi intent, ekstraksi entitas, query rewriting | cohere/north-mini-code:free | deepseek-v4-flash:free | Hanya 3B parameter aktif, tugas sempit, latensi rendah, gratis, mendukung tools dan structured output. Ini layer dengan trafik terbesar, jadi memakai model paling ringan adalah penghematan terbesar. |
| Rule compliance dan penjelasan bersitasi | deepseek-v4.1-flash:free | mimo-v2.6-flash:free | Tugasnya mengubah output rule engine yang sudah deterministik menjadi penjelasan dengan sitasi. Butuh disiplin instruksi dan refusal, bukan penalaran frontier. 8B sampai 16B aktif cukup, konteks besar membantu menyisipkan chunk rulebook. Lisensi MIT lebih aman untuk penggunaan komersial. |
| Penalaran kompleks (interaksi multi aturan, skenario risiko baru) | mimo-v2.6-flash:free | deepseek-v4.1-flash:free | Di daftar gratis yang terverifikasi tidak ada model kelas frontier yang setara kimi-k3 atau glm-5.3. Karena aturan inti sudah dijalankan rule engine deterministik, layer ini jarang dipanggil. MiMo v2.6 dengan 15B aktif adalah kandidat terkuat yang tersedia gratis, tetapi jangan mengklaim kualitas frontier. |
| Coding dan generasi tool | cohere/north-mini-code:free | deepseek-v4.1-flash:free | North Mini Code memang dirancang untuk coding agentic dan tool calling, dengan endpoint gratis yang aktif saat dicek. Ini pilihan paling langsung untuk membuat atau memperbaiki kode pada alur agentic. |
| Multimodal (gambar chart, dokumen, video) | qwen3.8-flash:free | mimo-v2.5:free | Keduanya menerima input gambar dan video. Berguna jika nanti ada skenario membaca screenshot dashboard atau chart trader. Belum dibutuhkan untuk demo inti. |

Biaya: seluruh model di atas gratis, sehingga biaya token untuk portfolio adalah
nol. Ini menghapus satu hambatan utama profil pemilik. Trade-off yang jujur:
gratis berarti batas laju, ketersediaan yang bisa berubah, dan tanpa jaminan
uptime. Untuk demo yang dapat dicoba orang lain, tetap pakai mock LLM
deterministik agar hasil tidak bergantung pada layanan gratis pihak ketiga.

### 1.3 Jawaban Q9: MiMo dan MiniMax

MiMo layak menjadi kandidat routing. Verifikasi menunjukkan bahwa
mimo-v2.5:free dan mimo-v2.6-flash:free memang ada di daftar gratis, dan entri
mimo-v2.6-flash:free juga tercatat di katalog model DSH sesi ini. Untuk MiniMax
sendiri: tidak terkonfirmasi. Pencarian pada katalog gratis OpenRouter yang
berhasil diambil tidak menemukan entri MiniMax, dan tidak ada dokumentasi yang
tersedia di sumber yang dicek. Jadi jangan memasukkan MiniMax ke tabel routing
atau ke lamaran sebelum ada bukti katalog resmi.

### 1.4 Jawaban Q10: jev, laya, clef

- Jev: ditemukan entri "typesafe/jev-router" di katalog OpenRouter. Namun ini
  adalah sebuah router pemilihan model, bukan model gratis mandiri, dan
  harganya bertanda dinamis (nilai pricing -1), sehingga bukan kandidat layer
  yang dapat diandalkan sebagai model gratis. Apakah "jev" yang dimaksud
  pengguna sama dengan entri ini: tidak terkonfirmasi.
- Laya: tidak terkonfirmasi. Endpoint katalog untuk nama ini mengembalikan 404
  dan tidak ada dokumentasi yang ditemukan.
- Clef atau clef flash: tidak terkonfirmasi. Sama seperti laya, katalog
  mengembalikan 404 dan tidak ada dokumentasi.

Sikap yang benar: catat ketiganya sebagai "tidak terkonfirmasi" dan jangan
memakainya sebagai dasar keputusan. Mengarang kemampuan atau lisensi model yang
tidak terverifikasi justru merusak kredibilitas di mata pewawancara teknis.

---

## Bagian 2. Company Brain: Apakah Ada Gunanya

### 2.1 Definisi

Company brain adalah lapisan pengetahuan terpusat yang menyatukan rulebook,
SOP internal, riwayat keputusan, dan data operasional menjadi satu sumber
kebenaran yang dapat ditelusuri. Idealnya ia punya ingest yang konsisten,
versioning, kontrol akses, jejak audit, dan mekanisme umpan balik saat
pengetahuan berubah.

### 2.2 Analisis first principles: kapan berguna, kapan over-engineering

Company brain baru berguna jika minimal beberapa kondisi ini terpenuhi:

1. Banyak konsumen internal membutuhkan pengetahuan yang sama, sehingga
   duplikasi jawaban mahal.
2. Pengetahuan sering berubah dan harus tetap sinkron antar tim.
3. Volume pertanyaan cukup tinggi sampai jawaban manual tidak lagi efisien.
4. Keputusan perlu jejak audit, misalnya untuk kepatuhan.
5. Ada data keputusan masa lalu yang benar-benar dapat dipakai untuk belajar.

Company brain menjadi over-engineering jika kondisi berikut berlaku:

1. Hanya satu orang yang memakainya.
2. Korpusnya kecil dan fakta intinya sudah deterministik, seperti aturan
   Tradeify yang stabil.
3. Belum ada volume tiket atau keputusan nyata untuk dijadikan riwayat.
4. Aturan sudah bisa dikodekan sebagai fungsi murni, sehingga pencarian
   semantik tidak menambah ketepatan.

Untuk Tradeify, kondisi over-engineering lebih dominan saat ini: korpus proyek
hanya 9 chunk, aturan inti deterministik, dan pemilik proyek belum memiliki data
internal perusahaan. Mengklaim telah membangun "company brain" untuk Tradeify
tanpa akses data internal adalah klaim yang tidak dapat dibuktikan dan berisiko
terdengar berlebihan.

### 2.3 Rekomendasi fase

Fase 1 (sekarang, untuk portfolio dan lamaran):

- Pertahankan aturan sebagai kode deterministik di `src/rule_engine.py`.
- Pertahankan pengetahuan sebagai korpus kecil yang di-version-control di
  `data/rulebook_chunks.json`, dengan chunk per aturan, bukan per jumlah
  karakter.
- Tunjukkan retrieval hybrid plus verifikasi sitasi plus refusal. Ini bukti
  yang bisa dijalankan dan diuji, bukan slogan.
- Tulis di README bahwa ini adalah "knowledge layer minimal yang dapat
  diverifikasi", bukan company brain.

Fase 2 (nanti, setelah masuk dan punya data nyata):

- Tambahkan ingest SOP internal, log keputusan, dan versioning sumber.
- Tambahkan kontrol akses dan jejak audit jika sudah ada banyak konsumen.
- Bangun umpan balik dari tiket nyata sebelum menambah retrieval canggih.
- Baru pada tahap ini istilah company brain menjadi akurat dan dapat
  dipertanggungjawabkan.

Kesimpulan jujur untuk Q12: company brain ada gunanya, tetapi bukan untuk
tahap sekarang dan bukan sebagai klaim portfolio. Retrieval yang solid plus rule
engine deterministik jauh lebih meyakinkan bagi perekrut teknis karena dapat
dijalankan, diuji, dan angkanya terukur.

---

## Bagian 3. Semantic Retrieval: Putusan

### 3.1 Fakta dari eval proyek ini

Angka dari `evals/retrieval_results.json` pada harness 30 pertanyaan dan korpus
9 chunk, semuanya hasil eksekusi `python3 src/eval.py`:

| Metode | hit@1 | hit@5 | MRR |
|---|---|---|---|
| BM25 | 0.833 | 1.000 | 0.908 |
| Vector saja | 0.833 | 1.000 | 0.903 |
| Hybrid (BM25 + vector + RRF) | 0.867 | 1.000 | 0.925 |

Catatan kejujuran yang wajib disampaikan: selisih hybrid terhadap BM25 dan
vector pada hit@1 adalah 0.867 dikurangi 0.833, yaitu 0.034, yang setara dengan
satu pertanyaan dari 30. Pada MRR, vector saja justru sedikit di bawah BM25
(0.903 berbanding 0.908). Jadi perbedaan ini bersifat indikatif, bukan
kesimpulan statistik yang kuat. Yang dapat dikatakan dengan aman adalah hybrid
tidak pernah kalah dan secara konsisten sedikit di depan pada harness ini.

### 3.2 Putusan

Untuk korpus kecil yang terstruktur seperti rulebook Tradeify, semantic search
berbasis vector bukan pembeda utama. Alasannya: teks regulasi penuh istilah
eksak yang harus cocok persis, seperti daily loss limit, trailing drawdown, dan
consistency rule. BM25 sangat kuat untuk pencocokan leksikal semacam itu.
Vector search membantu saat parafrasa atau sinonim, tetapi pada korpus 9 chunk
keunggulan itu tidak banyak muncul. Yang benar-benar menang adalah fusi hybrid
BM25 + vector + RRF, dan itu sudah dipakai proyek ini.

Semantic search baru benar-benar dibutuhkan ketika setidaknya salah satu
kondisi berikut berlaku:

1. Korpus besar sehingga pencocokan kata kunci saja tidak cukup menutup
   variasi bahasa.
2. Bahasa tidak terstruktur, misalnya keluhan trader bebas atau tiket support,
   bukan tabel aturan.
3. Banyak sinonim dan parafrasa yang tidak berbagi kata kunci dengan dokumen.
4. Kebutuhan lintas bahasa, misalnya pertanyaan Indonesia terhadap dokumen
   Inggris.
5. Dokumen panjang yang jawabannya tersebar dan perlu pencarian makna.

Selama korpus masih kecil dan terstruktur, menambah semantic search canggih
lebih banyak menambah kompleksitas daripada ketepatan. Tanpa hype.

---

## Bagian 4. Paket Final Sebelum Melamar (Jawaban Q11)

Isi bagian ini adalah checklist bukti nyata yang harus ada sebelum apply. Untuk
setiap item ditulis status saat ini dan langkah berikutnya yang spesifik, lalu
diurutkan berdasarkan dampak terhadap usaha, dengan asumsi hanya memakai HP dan
budget nol.

### Item A. README dengan angka eval terverifikasi

Status: sudah ada.

Bukti: `README.md` memuat tabel hasil (rule engine 29/29, hybrid hit@1 0.867 dan
MRR 0.925), dan `evals/retrieval_results.json` menyimpan angka yang sama.
`AUDIT_REGRESI.md` mencatat asal angka, termasuk regresi 0.800 yang diperbaiki.

Langkah berikutnya:

1. Pastikan satu perintah tunggal mereproduksi seluruh angka, misalnya skrip
   `scripts/verify_all.sh` yang menjalankan `tests/test_rule_engine.py`,
   `src/eval.py`, dan `demo.py`.
2. Beri label eksplisit di README bahwa angka behavior eval berasal dari mock
   LLM deterministik, bukan model nyata, agar tidak terkesan berlebihan.
3. Cantumkan tanggal dan perintah persis yang menghasilkan angka.

### Item B. Demo yang bisa dicoba

Status: belum ada.

Bukti: `demo.py` hanya dapat dijalankan lokal, tanpa URL publik, dan memakai
mock LLM. Tidak ada Gradio, tidak ada Hugging Face Space, tidak ada endpoint
publik. Tidak ada video demo.

Langkah berikutnya, pilih sesuai tenaga yang tersedia:

1. Opsi dengan dampak per usaha tertinggi: rekam video demo 90 detik dari layar
   HP saat menjalankan atau memperagakan alur, lalu unggah ke YouTube unlisted
   atau GitHub Release dan tautkan di README. Ini gratis dan bisa dikerjakan
   dari HP.
2. Opsi kedua, dampak tinggi: buat Space Gradio gratis di Hugging Face yang
   menjalankan `demo.py` dengan `MOCK_LLM=1`, sehingga deterministik, tanpa kunci
   API, dan tanpa GPU. Pembuatan dan deploy dapat dilakukan dari browser HP.
   Tampilkan tiga hal: skenario DLL soft breach, pertanyaan trailing drawdown
   dengan sitasi, dan probe refusal.
3. Opsi ketiga, hanya jika dua opsi di atas sudah selesai: endpoint FastAPI
   publik gratis. Ini paling rapuh karena layanan gratis bisa tidur, jadi bukan
   prioritas.

### Item C. Arsitektur model routing

Status: sudah ada, tetapi perlu diselaraskan.

Bukti: `src/model_router.py` berisi tiga tier (classify, explain, reason) dan
`README.md` menampilkan diagram arsitektur.

Langkah berikutnya:

1. Selaraskan nama model di kode dan dokumen dengan daftar gratis yang benar
   benar terverifikasi di Bagian 1, atau jelaskan bahwa nama lama adalah target
   dan nama gratis adalah realisasi.
2. Tambahkan tabel routing satu halaman di README dengan alasan per layer.
3. Tambahkan tes kecil atau log yang menunjukkan keputusan routing, misalnya
   pertanyaan ambigu naik ke tier reason.

### Item D. Eval harness

Status: sudah ada.

Bukti: `src/eval.py`, `evals/eval_set.json` (30 pertanyaan plus probe refusal),
dan `evals/retrieval_results.json`. Harness mengukur hit@k dan MRR serta
behavior eval (citation precision, refusal, consistency).

Langkah berikutnya:

1. Jalankan satu kali dengan model gratis nyata melalui `default_clients(mock=False)`
   memakai kunci OpenRouter gratis, lalu simpan hasilnya sebagai angka model
   nyata terpisah dari angka mock. Ini mengubah klaim menjadi bukti nyata.
2. Karena pemilik hanya memakai HP, jalankan langkah ini lewat GitHub Actions
   gratis untuk repo publik, sehingga reproducible tanpa laptop.
3. Jangan hapus angka mock. Simpan keduanya berdampingan agar jelas mana yang
   deterministik dan mana yang model nyata.

### Item E. Repo publik dan CI

Status: belum ada.

Bukti: direktori proyek belum menjadi repository git dan tidak punya remote
publik.

Langkah berikutnya:

1. Inisialisasi git dan unggah ke GitHub publik dari browser HP atau terminal
   yang tersedia.
2. Tambahkan GitHub Actions gratis yang menjalankan 29 tes rule engine dan eval
   pada setiap push untuk repo publik. Ini memberi bukti eksternal bahwa angka
   bukan karangan.

### Urutan prioritas (usaha versus dampak)

1. Repo GitHub publik plus CI (Item E): usaha menengah, dampak tertinggi karena
   semua bukti lain menjadi dapat diverifikasi orang lain.
2. Video demo 90 detik (Item B opsi 1): usaha rendah, dampak tinggi.
3. Penyelarasan routing (Item C): usaha rendah, dampak menengah sampai tinggi.
4. Space Gradio mock (Item B opsi 2): usaha menengah, dampak tinggi.
5. Eval dengan model gratis nyata (Item D): usaha menengah, dampak menengah
   sampai tinggi, bergantung ketersediaan endpoint gratis.
6. Fine-tune Colab: tunda. Bisa dijalankan dari browser HP, tetapi perlu 2
   sampai 4 jam dengan tab terbuka dan hasilnya belum tentu stabil. Jangan
   dijadikan syarat melamar.

Kesimpulan Q11: bukti yang paling mengubah permainan bukan model terbesar,
melainkan kombinasi repo publik yang dapat dijalankan, angka eval yang jujur,
dan demo singkat yang dapat dicoba. Itu dapat diselesaikan dari HP dengan biaya
nol.

---

## Bagian 5. Risiko dan Batas Jujur

Bagian ini menyatakan eksplisit apa yang belum ada, agar tidak ada klaim palsu
saat melamar.

1. Fine-tune belum dijalankan. `src/finetune_qlora.py` dan `colab_finetune.ipynb`
   ada, tetapi belum ada bukti eksekusi, belum ada adapter, dan belum ada
   hasil eval model hasil fine-tune. Proses ini membutuhkan GPU. Status: belum.
2. Demo belum memakai LLM hidup. `demo.py` dan eval behavior memakai mock LLM
   deterministik. Angka behavior eval (citation precision 0.967, refusal 1.0,
   consistency 1.0, answer hit 0.833) adalah validasi harness, bukan kinerja
   model nyata. Status: belum ada bukti model nyata.
3. Belum ada integrasi broker live. `src/worker.py::fetch_broker_snapshot`
   masih stub, belum tersambung ke Tradovate atau Rithmic. Status: belum.
4. Belum ada deployment publik. Tidak ada URL Space, tidak ada endpoint API
   publik, tidak ada demo online. Status: belum.
5. Belum ada repository git publik dan belum ada CI. Status: belum.
6. Korpus kecil dan harness kecil. Korpus 9 chunk dan eval 30 pertanyaan.
   Selisih hit@1 antara hybrid dan vector adalah satu pertanyaan, sehingga harus
   disebut indikatif. Jangan menyajikannya sebagai keunggulan besar.
7. Ketersediaan model gratis tidak stabil. Endpoint gratis dapat berubah, punya
   batas laju, dan sebagian lisensinya tidak terkonfirmasi. Jangan menjanjikan
   uptime atau lisensi yang belum diperiksa.
8. Profil pemilik. Lulusan SMK non-CS, tanpa laptop, hanya HP, budget nol.
   Jangan mengarang gelar, pengalaman kerja, perusahaan, atau kredensial lain.
   Kekuatan lamaran harus bertumpu pada artefak yang dapat dijalankan dan angka
   yang dapat direproduksi.

Risiko strategis utama: melamar dengan klaim yang tidak dapat dibuktikan akan
langsung terlihat oleh pewawancara teknis, terutama untuk role yang secara
eksplisit mencari orang yang membangun setiap hari. Lebih baik melamar dengan
satu demo jujur yang berjalan daripada dengan daftar klaim besar.

---

## Lampiran. Log Verifikasi

Semua pemeriksaan berikut dilakukan pada 3 Oktober 2026.

1. Job posting "Agentic Coder" Tradeify diambil melalui API publik Ashby
   (https://api.ashbyhq.com/posting-api/job-board/tradeify). Status HTTP 200.
   Judul "Agentic Coder", departemen Product, tipe FullTime, remote, diterbitkan
   25 Agustus 2026. Requirement yang diringkas di transkrip DeepSeek (Python 3.12,
   FastAPI, TypeScript, LangGraph, pgvector, hybrid search, BullMQ, Langfuse,
   eval harness, integrasi Slack/Discord/Intercom) masih sesuai. Tidak ada
   perubahan arah requirement.
2. Katalog model OpenRouter diambil dari https://openrouter.ai/api/v1/models.
   Enam model gratis diverifikasi keberadaannya. Endpoint per model diakses
   untuk memeriksa harga dan status; cohere/north-mini-code:free memiliki
   endpoint aktif dengan harga 0.
3. Katalog model internal DSH pada sesi ini memuat deepseek-v4-flash:free,
   deepseek-v4.1-flash:free, mimo-v2.5:free, mimo-v2.6-flash:free,
   qwen3.8-flash:free, dan cohere/north-mini-code:free.
4. Untuk Q10: "typesafe/jev-router" ditemukan di OpenRouter tetapi merupakan
   router dengan harga dinamis, bukan model gratis mandiri. "laya" dan
   "clef/clef flash" mengembalikan 404 dan tidak terkonfirmasi. MiniMax tidak
   ditemukan pada katalog gratis yang berhasil diambil dan tidak terkonfirmasi.
5. Status proyek (README, AUDIT_REGRESI, eval, demo, router, worker) diperiksa
   langsung dari berkas di `/home/hatch/workspace/dsh/tradeify-work`. Angka eval
   yang dikutip di dokumen ini diambil apa adanya dari
   `evals/retrieval_results.json` dan `AUDIT_REGRESI.md`, tanpa diubah.