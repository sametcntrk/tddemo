#!/bin/bash
# EC2 User Data — Amazon Linux 2023 için.
# Launch instance → Advanced details → User data alanına bu dosyanın tamamını yapıştır.
# Sadece üstteki AYARLAR bloğunu doldurman yeterli. Log: /var/log/cloud-init-output.log
set -euxo pipefail

# ======================= AYARLAR =======================
REPO_URL="https://github.com/sametcntrk/aws-core-demo.git"
BRANCH="main"
# Private repo ise: https://<token>@github.com/... (token yerine public repo önerilir)

cat > /etc/core-demo.env <<'EOF'
DB_ENGINE=mysql
DB_HOST=mydb.xxxxxxxx.eu-central-1.rds.amazonaws.com
DB_USER=admin
DB_PASSWORD=degistir
# DB_SECRET_ARN=
# S3_BUCKET=
# DDB_TABLE=
# SQS_QUEUE_URL=
# SNS_TOPIC_ARN=
# REDIS_HOST=
# EFS_PATH=/mnt/efs
# DEMO_SECRET_ID=
# SSM_PARAM_NAME=
EOF

# EFS kullanacaksan file system ID'sini yaz (örn. fs-0123456789abcdef0), yoksa boş bırak
EFS_ID=""
# =======================================================

chmod 600 /etc/core-demo.env
chown ec2-user /etc/core-demo.env

dnf install -y git python3 python3-pip

if [ -n "$EFS_ID" ]; then
  dnf install -y amazon-efs-utils
  mkdir -p /mnt/efs
  echo "$EFS_ID:/ /mnt/efs efs _netdev,tls 0 0" >> /etc/fstab
  mount -a -t efs
  chmod 777 /mnt/efs
  grep -q '^EFS_PATH=' /etc/core-demo.env || echo "EFS_PATH=/mnt/efs" >> /etc/core-demo.env
fi

rm -rf /opt/core-demo
git clone --depth 1 --branch "$BRANCH" "$REPO_URL" /opt/core-demo
python3 -m venv /opt/core-demo/venv
/opt/core-demo/venv/bin/pip install --upgrade pip
/opt/core-demo/venv/bin/pip install -r /opt/core-demo/requirements.txt
chown -R ec2-user:ec2-user /opt/core-demo

cp /opt/core-demo/deploy/core-demo.service /etc/systemd/system/core-demo.service
systemctl daemon-reload
systemctl enable --now core-demo

# Hızlı doğrulama
sleep 3
curl -fsS http://localhost/health && echo " <- core-demo çalışıyor"
