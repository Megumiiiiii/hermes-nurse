# hermes-nurse — demo video, 85 detik

Aturan kontes: **≤90 detik**, harus memperlihatkan plugin benar-benar bekerja,
tag @NousResearch di post. Judge tidak menonton 90 detik dua kali — 3 detik
pertama menentukan.

Rekam dengan `asciinema`/OBS/QuickTime, terminal font besar (18pt+), lebar
kolom ~100. **Jangan** rekam layar penuh — judge harus bisa baca.

---

## Shot list

### 0:00–0:06 — Hook (6 detik)
**Layar:** terminal bersih. Ketik pelan-pelan, jangan buru-buru.

```
pm2 list
```

**VO/teks:** "Six services. One is dying every four minutes. Nothing tells you why."

**Kenapa ini:** judge langsung lihat masalah nyata, bukan fitur.

---

### 0:06–0:10 — Nama plugin (4 detik)
```
hermes plugins list | grep nurse
```

**VO:** "nurse. It reads the supervisor you already run."

---

### 0:10–0:26 — Tool 1: satu tabel (16 detik)
```
ask hermes "which of my services are unhealthy?"
```

Model memanggil `service_list` → tabel muncul.

**VO:** "pm2 and systemd in one table. State, restarts, memory, uptime, last exit code.
systemd is not a second-class citizen here — it reads `NRestarts`, not just `ActiveState`."

**Pastikan di frame:** minimal satu baris pm2 dan satu baris systemd.

---

### 0:26–0:50 — Tool 2: diagnosis (24 detik — inti video)
```
ask hermes "why is daytrader down?"
```

Model memanggil `service_triage` → keluar finding `rate_limited` + evidence.

**VO:** "It does not guess. It reads the service's own log — and when pm2 has
already rotated it at midnight, it follows the rotated siblings. That is where
the evidence actually is."

**Kalau ada waktu:** tunjukkan potongan log asli yang jadi bukti.

**VO lanjutan:** "Crash loop is measured per hour, not by restart count. pm2's
counter is cumulative — 47 restarts over 85 minutes is a crash loop; 15 restarts
after a fresh start is not. Totals can't tell those apart. Rate can."

---

### 0:50–1:05 — Tool 3: guard (15 detik)
```
ask hermes "restart daytrader"
```

**Tampilkan penolakan pertama:** butuh konfirmasi.
```
ask hermes "yes, restart daytrader"
```
Baru jalan. Lalu:
```
ask hermes "restart daytrader again"
```
→ **cooldown menolak.**

**VO:** "Repair is the only thing that writes. One verb allowlist, a persisted
cooldown, and it refuses to restart a service that is already crash-looping —
because restarting a crash loop destroys the evidence you need."

---

### 1:05–1:18 — Bukti & penutup (13 detik)
```
ask hermes "restart agentlp; rm -rf /"
```
→ guard menolak (injection).

Lalu:
```
cd ~/hermes-nurse && pytest -q
```
→ `36 passed`

**VO:** "36 tests. 4 of them are the guard trying to be bypassed — including shell
injection through the service name. Install it with `hermes plugins install nurse`."

**End card (2 detik):** `github.com/Megumiiiiii/hermes-nurse`

---

## Checklist sebelum rekam

- [ ] `service_triage('daytrader')` mengembalikan finding nyata (bukan "unknown")
- [ ] Ada satu service yang benar-benar crash-loop untuk didemokan penolakannya
      — kalau tidak ada, buat satu: `pm2 start --name demo-crashloop -- sh -c 'exit 1'`
      lalu tunggu 5 restart
- [ ] Font terminal ≥18pt, tidak ada notifikasi pop-up
- [ ] Tidak ada API key / token / isi `.env` di frame — **cek dua kali**
- [ ] Wallet address / path sensitif tidak muncul
- [ ] Durasi total ≤90 detik

## Untuk post X

```
Hermes Agent plugin hackathon entry: nurse.

pm2 and systemd in one table, crash logs triaged into a real diagnosis
(rotated logs included), and one guarded repair surface — verb allowlist,
cooldown, refuses crash loops.

36 tests. Security scan: safe.

hermes plugins install nurse

@NousResearch
```

Lampirkan video. Jangan taruh link di balasan pertama — X menurunkan jangkauan
post dengan link eksternal; taruh repo di reply.
