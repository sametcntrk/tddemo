# AWS Core Services Demo

EC2 üzerinde çalışan tek sayfalık test uygulaması. Tarayıcıdan tıklayarak şunları test edersin:

| Kart | Ne gösterir |
|---|---|
| **EC2 (üst şerit)** | Sayfayı sunan instance ID, AZ, IP, AMI — IMDSv2 ile. Her instance'ın kendi rengi var. |
| **ALB** | N istek atıp hangi instance/AZ'nin cevap verdiğini çubuk grafikle gösterir; `/health`'i 503'e çevirip ALB'nin trafiği kaydırmasını izletir. |
| **RDS** (MySQL/MariaDB/PostgreSQL/Aurora) | Bağlantı, sürüm, gecikme; `demo_notes` tablosunda ekle/listele/sil (kaydı hangi instance yazdı). |
| **S3** | Dosya yükle, listele, presigned URL ile indir, sil. |
| **DynamoDB** | PutItem + Scan. |
| **ElastiCache (Redis/Valkey)** | Instance'lar arası paylaşılan sayaç. |
| **SQS / SNS** | Kuyruğa gönder/al, topic'e publish (e-posta veya SQS fan-out). |
| **EFS** | Tüm instance'ların ortak yazdığı log dosyası. |
| **Secrets Manager / SSM** | IAM rolüyle secret/parametre okuma (maskeli). |
| **CloudWatch + Auto Scaling** | Özel metrik gönder; CPU yükü üretip ASG scale-out'u tetikle. |

Sadece RDS'i tanımlasan da çalışır; diğer kartlar "yapılandırılmadı" görünür.

## Kurulum (en kolay yol: GitHub + User Data)

1. **Bu klasörü GitHub'a public repo olarak push et** (secret içermez; tüm ayarlar instance'taki `/etc/core-demo.env` dosyasında).
2. **IAM rolü**: EC2 için bir rol oluştur → `AmazonSSMManagedInstanceCore` (SSH'sız Session Manager erişimi) + kullanacağın servisler için `deploy/iam-policy.json` (placeholder'ları doldur, kullanmadığın statement'ları sil).
   RDS secret'ı customer-managed KMS key ile şifreliyse o key için `kms:Decrypt` da ekle.
3. **Security group'lar**

   | SG | Inbound |
   |---|---|
   | `alb-sg` | TCP 80 ← 0.0.0.0/0 (veya kendi IP'n) |
   | `app-sg` (EC2) | TCP 80 ← `alb-sg` (ALB'siz direkt test için geçici olarak kendi IP'n) |
   | `rds-sg` | TCP 3306/5432 ← `app-sg` |
   | `redis-sg` | TCP 6379 ← `app-sg` |
   | `efs-sg` | TCP 2049 ← `app-sg` |

4. **EC2 Launch**: Amazon Linux 2023, `t3.micro`/`t3.small`, kendi VPC'n, IAM rolü, `app-sg`.
   *Advanced details → User data* alanına `deploy/user-data.sh` içeriğini yapıştır, üstteki `REPO_URL` ve env bloğunu doldur.
5. 2–3 dk sonra `http://<instance-ip>/` (public subnet) veya ALB DNS adıyla aç.

### Ağ notu (önemli)
User data `github.com`, `pypi.org` ve `dnf` repolarına çıkar. Instance **private subnet'teyse NAT Gateway** gerekir.
NAT yoksa: public subnet'te public IP ile başlat, ya da kodu S3'e koyup (S3 Gateway Endpoint ile) oradan çek.
AWS servislerine NAT'sız erişim için Interface/Gateway VPC Endpoint'leri (S3, DynamoDB gateway; SQS, SNS, Secrets Manager, SSM, CloudWatch, STS interface) ekleyebilirsin.

## ALB + Auto Scaling demosu

1. Instance'tan (veya aynı user data ile) **Launch Template** oluştur.
2. **Target group**: HTTP 80, health check path `/health`, healthy threshold 2, interval 10 sn, **stickiness kapalı**.
3. **ALB**: internet-facing, 2 public subnet (farklı AZ), `alb-sg`, listener 80 → target group.
4. **Auto Scaling Group**: launch template, 2 private subnet (farklı AZ), min 2 / desired 2 / max 4, target group'a bağla, ELB health check açık, *Target tracking: Average CPU 50%*.
5. ALB DNS adını aç:
   - "İstek gönder" → istekler instance'lar/AZ'ler arasında dağılır.
   - "Bu instance'ı unhealthy yap" → ~20 sn sonra target group'ta unhealthy olur, ALB trafiği diğerine yollar. ASG ELB health check açıksa instance'ı değiştirir.
   - "CPU yükü" (300 sn) → CloudWatch alarmı → ASG yeni instance açar; ALB testinde yeni renk görünür.

## Yerel / manuel çalıştırma

```bash
python3 -m venv venv && . venv/bin/activate
pip install -r requirements.txt
export DB_ENGINE=mysql DB_HOST=... DB_USER=... DB_PASSWORD=...
python app.py   # http://localhost:8080
```

## Sorun giderme (instance üzerinde)

```bash
sudo tail -100 /var/log/cloud-init-output.log   # user data çıktısı
sudo systemctl status core-demo
sudo journalctl -u core-demo -f
sudo vi /etc/core-demo.env && sudo systemctl restart core-demo
cd /opt/core-demo && sudo -u ec2-user git pull && sudo systemctl restart core-demo   # kod güncelle
```

RDS kartı timeout veriyorsa neredeyse her zaman `rds-sg` inbound kuralı veya RDS'in farklı VPC'de olmasıdır.

## Maliyet hatırlatması
ALB saatlik + LCU ücreti, public IPv4 adresleri (ALB'ninkiler dahil) ve NAT Gateway saatlik + GB ücreti alır. Demo bitince ALB, NAT, ASG, RDS ve ElastiCache'i sil.
