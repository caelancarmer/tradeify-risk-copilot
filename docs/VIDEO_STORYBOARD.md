# Storyboard — Video Demo 60 Detik
# Direkam di HP, screen recording + voiceover Bahasa Inggris

## Persiapan
- Buka di browser HP (mode desktop):
  1. https://htmlpreview.github.io/?https://github.com/caelancarmer/tradeify-risk-copilot/blob/main/docs/archify-architecture.html
  2. https://htmlpreview.github.io/?https://github.com/caelancarmer/tradeify-risk-copilot/blob/main/docs/archify-precheck-workflow.html
- Nyalakan screen recorder bawaan HP. Rekam dengan voiceover langsung
  (bicara sambil menggerakkan layar). Satu take, tidak perlu editing.

## Skrip (60 detik)

**0:00–0:10 — Masalah** (tampilkan diagram arsitektur, zoom di trust boundary)
> "Prop firm traders lose payouts to rules they can't see. Tradeify's own
> one-star reviews say the same thing: my payout was rejected, and nobody
> told me why."

**0:10–0:25 — Solusi** (scroll ke jalur utama: Trader → precheck → rule engine → decide)
> "I built a Payout Risk Gate. It answers one question: can this trader
> payout today, and if not, why — in three milliseconds, with reasons and
> citations to the actual rulebook."

**0:25–0:40 — Bukti 1: microscalping** (pindah ke tab workflow, tunjukkan langkah microscalping)
> "It catches silent killers like microscalping. Funded accounts need more
> than fifty percent of trades held over ten seconds — a rule that never
> appears as a dashboard violation. Twenty-nine tests, all green."

**0:40–0:52 — Bukti 2: no LLM on money path** (kembali ke arsitektur, tunjukkan boundary)
> "No LLM touches the money path. Every decision is deterministic Python.
> The LLM only explains. Nineteen out of nineteen eval scenarios pass,
> citation precision is one point zero."

**0:52–1:00 — Penutup** (tampilkan repo GitHub)
> "Two hundred forty-one tests green, CI passing. I'm a futures trader who
> builds. Link's in the application."

## Catatan
- Bicara pelan dan jelas. Tidak perlu sempurna — satu take yang jujur
  lebih baik dari sepuluh take yang kaku.
- Jika salah bicara, ulangi kalimatnya saja tanpa hentikan rekaman,
  lalu potong bagian salahnya (atau biarkan — keaslian > kesempurnaan).
- Upload ke YouTube (unlisted) atau Google Drive, tempel link di lamaran.
