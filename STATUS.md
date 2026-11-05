# hermes-nurse — status menuju submit

Deadline: **5PM EST, 11 Nov 2026** = 05.00 WIB, 12 Nov.

## Selesai

- Repo publik: https://github.com/Megumiiiiii/hermes-nurse (MIT)
- SHA pin: `0ef06348fd9f7c7bfb7393e51406327c32fb3e4f`
- 45 test lolos · `hermes plugins doctor` bersih · security scan **safe** (0 HIGH/CRITICAL)
- Entry katalog siap: `~/hermes-nurse-catalog-entry.yaml`
  (identik dengan `/tmp/ha-cat/plugin-catalog/nurse.yaml`)
- Skrip PR otomatis: `~/hermes-nurse-catalog-pr.sh` (dry-run default)
- Skrip video: `demo/VIDEO-SCRIPT.md` (85 detik, shot-by-shot)

## Enam bug yang ditemukan saat verifikasi (bukan saat menulis test)

1. **Handler tidak pernah jalan di Hermes.** `tools/registry.py` memanggil
   `handler(args_dict)`, tapi handler dideklarasikan per-parameter — jadi dict
   masuk ke slot pertama dan tiap panggilan nyata melempar `AttributeError`.
   Seluruh suite lolos karena test memanggil fungsi gaya Python biasa.
   → handler sekarang `(args, **kwargs)`, plus 2 test yang meniru dispatch asli.
2. **Setiap repair sukses crash.** `_clean_output()` mengembalikan list, lalu
   dipanggil `.strip()` di atasnya. Diverifikasi dengan restart nyata ke
   `loaf-t8q30`: sukses, lalu panggilan kedua kena cooldown 120s.
3. **`severity` hanya ada di `findings[0]`.** Model harus menggali. Sekarang
   `severity` dan `unstable` jadi field top-level.
4. **Dua definisi "unstable" dalam satu plugin.** `service_list` memfilter
   dengan `restarts >= 10` (kumulatif) tapi baris yang sama mencetak
   `crash_loop: false` (berbasis rate). Sekarang keduanya memanggil
   `repair.is_crash_loop()`.
5. **Satu restart dianggap crash loop.** `hermes-gateway` dengan 1 restart
   setelah 220 detik terbaca 16/jam. Rate saja memfitnah proses muda; sekarang
   butuh minimal 2 restart, dan loop nyata mencapainya dalam hitungan detik.
6. **Baris service duplikat.** `list-unit-files` bisa melaporkan unit yang sama
   dua kali; `collect()` sekarang dedupe `(supervisor, name)`.

## Sisa langkah

### 1. Reload gateway (WAJIB sebelum rekam video)

> Status: sudah dijalankan 2026-11-05 12:32. Tiga tool terbukti hidup di sesi
> nyata (`headline: unstable_restarts`, `severity: critical`, `unstable: true`).
> Jalankan ulang setelah SHA plugin berubah.

Gateway hidup sejak 3 Nov dan memegang modul plugin di memori. `hermes plugins
install` dan edit di disk **tidak** sampai ke proses itu, jadi tool masih
melempar error lama. Agen tidak bisa restart gateway dari dalam dirinya sendiri
(SIGTERM membunuh shell-nya).

Jalankan dari terminal biasa / SSH:

```
~/bin/reload-nurse
```

Lalu verifikasi:

```
hermes chat -q 'Call service_triage with service=loaf-t8q30 once, then print only headline, severity, unstable.'
```

Harus keluar `headline`/`severity`/`unstable`, bukan `[TOOL_ERROR]`.

### 2. PR katalog

```
bash ~/hermes-nurse-catalog-pr.sh            # lihat dulu
APPLY=1 bash ~/hermes-nurse-catalog-pr.sh    # fork + branch + push + PR
```

Butuh `gh` terautentikasi sebagai pemilik repo (catalog rule 5: hanya owner).
Kalau tidak mau pasang `gh`, buka PR manual: fork → branch → tambah
`plugin-catalog/nurse.yaml` → PR. Isi body ada di dalam skrip.

### 3. Video

Ikuti `demo/VIDEO-SCRIPT.md`. Untuk adegan penolakan crash-loop, bikin service
crash-loop dulu:

```
pm2 start /bin/sh --name demo-crashloop -- -c 'echo "Error: ECONNREFUSED 127.0.0.1:6379" >&2; exit 1'
# tunggu ~20s sampai restarts > 30
pm2 delete demo-crashloop     # bersihkan setelah rekam
```

### 4. Post X + form typeform

Caption ada di akhir `VIDEO-SCRIPT.md`. Taruh link repo di reply, bukan di post
utama. Lalu submit form.

## Catatan

- `tools.tool_search.enabled: off` masih diperlukan agar tool plugin non-core
  terlihat model. Konsekuensi: tool MCP tidak lagi di-defer (~+6k token/turn).
  Backup config: `~/.hermes/config.yaml.bak-nurse`.
- `nurse` hanya terdaftar di `platform_toolsets.cli`, belum `telegram`. Kalau
  mau dipakai dari Telegram, tambahkan di sana lalu restart gateway.
