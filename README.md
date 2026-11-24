# Ruang — Presensi Kafe

Aplikasi frontend **HTML, CSS, JavaScript murni**, backend **Node.js murni**, dan database **SQLite** bawaan Node. Tanpa framework, layanan pihak ketiga, build step, atau paket npm. Memerlukan Node.js **24 atau lebih baru**. Tampilan responsif berbahasa Indonesia.

## Jalankan lokal

Di PowerShell, dari direktori proyek:

```powershell
$env:ADMIN_EMAIL = 'pj@kafe.com'
$env:ADMIN_PASSWORD = Read-Host 'Kata sandi awal admin (minimal 12 karakter)' -MaskInput
node server.js --init
Remove-Item Env:ADMIN_PASSWORD
node server.js
```

Buka **http://localhost:3000**. Masuk dengan akun yang baru dibuat. Tidak ada kredensial demo atau kata sandi bawaan. `init` hanya bisa dijalankan ketika belum ada admin.

1. Buka **Pengaturan kafe**, isi nama, koordinat pusat lokasi, radius, akurasi maksimum GPS, dan zona waktu. Titik lokasi awal sengaja kosong agar tidak menerima presensi di lokasi yang keliru.
2. Buka **Anggota tim** untuk menambahkan karyawan atau PJ cabang lain. Setiap akun wajib memiliki kata sandi minimal 12 karakter.
3. Karyawan login melalui perangkat masing-masing, membuka **Presensi saya**, mengizinkan kamera/GPS, mengambil selfie, memeriksa lokasi, lalu mengirim.
4. PJ membuka **Tinjauan presensi** untuk memeriksa foto/lokasi dan menyetujui atau menandai masalah dengan catatan.
5. Karyawan membuka **Riwayat presensi → Detail** untuk mengirim klarifikasi. PJ kemudian meninjau ulang.
6. Klik inisial akun di kanan atas untuk mengganti kata sandi. Riwayat dapat difilter dan diunduh sebagai CSV.

Kamera dan geolocation bekerja pada **HTTPS atau localhost**. Mengakses IP LAN melalui HTTP dari ponsel biasanya tidak dapat menggunakan kamera/GPS; gunakan domain HTTPS untuk penggunaan nyata.

## Pemetaan BPMN

| Tahap BPMN | Implementasi |
| --- | --- |
| Pegawai memverifikasi foto berbasis GPS | Kamera depan langsung, selfie JPEG, geolocation akurasi tinggi |
| Sistem memvalidasi radius | Server menghitung Haversine, memeriksa koordinat dan akurasi |
| Tidak lolos → ulangi | Presensi ditolak dengan alasan; karyawan memperbarui GPS/selfie |
| Lolos → simpan foto, GPS, waktu server | Transaksi insert SQLite dengan timestamp UTC dan tanggal kerja lokal |
| PJ memeriksa selfie dan lokasi | Antrian tinjauan, foto privat, koordinat dan tautan peta |
| Sesuai → selesai | Status `approved` |
| Tidak sesuai → tandai bermasalah | Status `flagged`, catatan PJ wajib |
| Pegawai klarifikasi ke PJ | Status `clarified`, PJ memeriksa kembali |

Masuk dan pulang dicatat terpisah. Pulang harus mengikuti masuk; shift lintas tengah malam mengikuti tanggal masuk. Maksimal satu pasangan masuk/pulang per tanggal kerja. Sistem tidak mengasumsikan jadwal shift atau menghitung keterlambatan karena aturan tersebut tidak terdapat pada BPMN. Riwayat UI/CSV memuat maksimal 2.000 peristiwa terakhir; data sebelumnya tetap tersimpan di SQLite.

## Deploy ke VPS Ubuntu/Debian

Siapkan Node.js 24+ (`node --version`), Nginx, Certbot, domain yang sudah mengarah ke VPS, dan akses SSH. Aplikasi ini **belum di-deploy ke VPS**; konfigurasi berikut siap digunakan di server Anda.

Salin source ke `/opt/ruang-presensi` (jangan salin database lokal atau data pengujian). Buat user dan penyimpanan:

```bash
sudo useradd --system --home /opt/ruang-presensi --shell /usr/sbin/nologin presensi
sudo install -d -o presensi -g presensi -m 700 /var/lib/ruang-presensi
sudo chown -R root:presensi /opt/ruang-presensi
sudo chmod -R g+rX /opt/ruang-presensi
```

Buat `/etc/ruang-presensi.env` dengan isi:

```ini
NODE_ENV=production
HOST=127.0.0.1
PORT=3000
DATA_DIR=/var/lib/ruang-presensi
```

Lindungi file, inisialisasi akun tanpa menyimpan kata sandi dalam source, lalu pasang service:

```bash
sudo chmod 600 /etc/ruang-presensi.env
cd /opt/ruang-presensi
read -r -p 'Email admin: ' ADMIN_EMAIL
read -r -s -p 'Kata sandi admin (minimal 12 karakter): ' ADMIN_PASSWORD
echo
export ADMIN_EMAIL ADMIN_PASSWORD
sudo --preserve-env=ADMIN_EMAIL,ADMIN_PASSWORD -u presensi env DATA_DIR=/var/lib/ruang-presensi /usr/bin/node server.js --init
unset ADMIN_PASSWORD ADMIN_EMAIL
sudo cp deploy/ruang-presensi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ruang-presensi
sudo cp deploy/nginx.conf /etc/nginx/sites-available/ruang-presensi
```

Ubah `presensi.example.com` di konfigurasi Nginx menjadi domain Anda. Jika Node dipasang di lokasi lain, sesuaikan `/usr/bin/node` pada service dan perintah init.

```bash
sudo ln -s /etc/nginx/sites-available/ruang-presensi /etc/nginx/sites-enabled/ruang-presensi
sudo nginx -t
sudo systemctl reload nginx
sudo certbot --nginx -d presensi.example.com
sudo systemctl status ruang-presensi
```

Gunakan **https://domain-anda** setelah TLS aktif. Cookie sesi production hanya dikirim melalui HTTPS. Buka port 80/443 dan SSH sesuai konfigurasi firewall Anda; port 3000 hanya terikat ke loopback. Log service tersedia melalui `journalctl -u ruang-presensi`.

## Data dan operasional

- Data, selfie, akun, sesi, serta audit tersimpan dalam satu database `presensi.sqlite` di `DATA_DIR` (default `./data`). Foto tidak pernah menjadi file publik.
- Gunakan satu proses server. SQLite memakai WAL, foreign keys, dan batas unik untuk mencegah presensi ganda.
- Kata sandi menggunakan scrypt dan salt acak. Cookie HttpOnly/SameSite, CSRF token, pemeriksaan peran, query terparameter, CSP dan pembatasan percobaan login sudah diterapkan. Sesi berakhir setelah 12 jam. Login throttle in-memory 25 percobaan/15 menit per alamat peer; di belakang Nginx batas ini dibagi seluruh pengguna. Untuk kafe besar tambahkan rate limit per IP di Nginx dan sesuaikan kebijakan backend.
- GPS browser dapat dipalsukan dan selfie bukan verifikasi biometrik/liveness. Sistem membantu pemeriksaan sesuai BPMN; PJ tetap bertanggung jawab memeriksa kesesuaian foto/lokasi.
- Cadangkan secara konsisten menggunakan SQLite CLI: `sudo sqlite3 /var/lib/ruang-presensi/presensi.sqlite ".backup '/lokasi-backup/presensi.sqlite'"`. Lindungi backup karena berisi foto/lokasi karyawan. Jangan menyalin hanya file utama saat server aktif karena data bisa berada dalam WAL.
- Untuk restore, hentikan service, pulihkan backup beserta kepemilikan/izin file, pastikan tidak ada WAL/SHM lama dari database sebelumnya, lalu mulai service. Uji pemulihan pada lingkungan terpisah terlebih dahulu.
- Tetapkan kebijakan retensi foto/lokasi bersama pengelola kafe; tidak ada penghapusan data otomatis dalam versi ini.

## Pengujian

```bash
node --test test/app.test.js
```

Pengujian memakai database sementara dan port 3197: password hashing, radius/akurasi, login/CSRF, pembatasan peran dan foto, masuk/pulang, penolakan duplikat, review → klarifikasi → persetujuan, persistensi sesudah restart, ganti kata sandi, dan penonaktifan akun. Kamera dan GPS perangkat nyata perlu diuji manual pada domain HTTPS di lokasi kafe.

