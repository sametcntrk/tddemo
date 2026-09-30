"""AWS Core Services Demo — EC2 üzerinde çalışan tek sayfalık test uygulaması.

Her servis kartı opsiyoneldir: ilgili ortam değişkeni tanımlı değilse kart
"yapılandırılmadı" olarak görünür, uygulama yine de çalışır.
Ayarlar /etc/core-demo.env dosyasından okunur (bkz. deploy/core-demo.env.example).
"""
import json
import os
import socket
import subprocess
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from functools import lru_cache, wraps

import boto3
from botocore.config import Config
from flask import Flask, jsonify, render_template, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024  # S3 upload limiti: 10 MB

UNHEALTHY_FLAG = "/tmp/core-demo-unhealthy"
env = os.environ.get


# ---------------------------------------------------------------- IMDSv2
def _imds(path, timeout=1.0):
    try:
        token_req = urllib.request.Request(
            "http://169.254.169.254/latest/api/token",
            method="PUT",
            headers={"X-aws-ec2-metadata-token-ttl-seconds": "300"},
        )
        token = urllib.request.urlopen(token_req, timeout=timeout).read().decode()
        req = urllib.request.Request(
            f"http://169.254.169.254/latest/meta-data/{path}",
            headers={"X-aws-ec2-metadata-token": token},
        )
        return urllib.request.urlopen(req, timeout=timeout).read().decode()
    except Exception:
        return None


@lru_cache(maxsize=1)
def instance_info():
    return {
        "instance_id": _imds("instance-id") or socket.gethostname(),
        "instance_type": _imds("instance-type"),
        "az": _imds("placement/availability-zone"),
        "region": _imds("placement/region"),
        "private_ip": _imds("local-ipv4"),
        "public_ip": _imds("public-ipv4"),
        "ami_id": _imds("ami-id"),
        "hostname": socket.gethostname(),
    }


def region():
    return env("AWS_REGION") or instance_info()["region"] or "eu-central-1"


def client(name):
    return boto3.client(name, region_name=region(), config=Config(connect_timeout=3, read_timeout=5, retries={"max_attempts": 2}))


def ok(**data):
    return jsonify({"ok": True, **data})


def fail(e, status=500):
    return jsonify({"ok": False, "error": f"{type(e).__name__}: {e}"}), status


def requires(var):
    """Servis ortam değişkeni tanımlı değilse anlaşılır bir hata döner."""
    def deco(fn):
        @wraps(fn)
        def inner(*a, **kw):
            if not env(var):
                return jsonify({"ok": False, "error": f"{var} tanımlı değil (/etc/core-demo.env)"}), 400
            return fn(*a, **kw)
        return inner
    return deco


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- Sayfa + health
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/health")
def health():
    if os.path.exists(UNHEALTHY_FLAG):
        return "UNHEALTHY", 503
    return "OK", 200


@app.route("/api/whoami")
def whoami():
    info = instance_info()
    return ok(
        **info,
        time=now(),
        via_alb="X-Forwarded-For" in request.headers,
        client_ip=request.headers.get("X-Forwarded-For", request.remote_addr),
        healthy=not os.path.exists(UNHEALTHY_FLAG),
        configured={
            "rds": bool(env("DB_HOST")),
            "s3": bool(env("S3_BUCKET")),
            "dynamodb": bool(env("DDB_TABLE")),
            "sqs": bool(env("SQS_QUEUE_URL")),
            "sns": bool(env("SNS_TOPIC_ARN")),
            "redis": bool(env("REDIS_HOST")),
            "efs": bool(env("EFS_PATH")),
            "secrets": bool(env("DEMO_SECRET_ID") or env("SSM_PARAM_NAME")),
            "cloudwatch": True,
        },
    )


@app.route("/api/health-toggle", methods=["POST"])
def health_toggle():
    if os.path.exists(UNHEALTHY_FLAG):
        os.remove(UNHEALTHY_FLAG)
    else:
        open(UNHEALTHY_FLAG, "w").close()
    return ok(healthy=not os.path.exists(UNHEALTHY_FLAG), instance_id=instance_info()["instance_id"])


# ---------------------------------------------------------------- RDS
def db_credentials():
    user, password = env("DB_USER"), env("DB_PASSWORD")
    secret_arn = env("DB_SECRET_ARN")
    if secret_arn:
        secret = json.loads(client("secretsmanager").get_secret_value(SecretId=secret_arn)["SecretString"])
        user, password = secret.get("username", user), secret["password"]
    return user, password


def db_engine():
    return (env("DB_ENGINE") or "mysql").lower()


def db_connect():
    user, password = db_credentials()
    host = env("DB_HOST")
    name = env("DB_NAME")
    if db_engine() in ("postgres", "postgresql"):
        import psycopg

        return psycopg.connect(
            host=host, port=int(env("DB_PORT", "5432")), user=user, password=password,
            dbname=name or "postgres", connect_timeout=5, sslmode=env("DB_SSLMODE", "prefer"),
        )
    import pymysql

    name = name or "demo"
    conn = pymysql.connect(host=host, port=int(env("DB_PORT", "3306")), user=user, password=password, connect_timeout=5, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{name}`")
    conn.select_db(name)
    return conn


def db_init(cur):
    if db_engine() in ("postgres", "postgresql"):
        cur.execute(
            "CREATE TABLE IF NOT EXISTS demo_notes (id SERIAL PRIMARY KEY, note TEXT NOT NULL,"
            " instance_id VARCHAR(64), created_at TIMESTAMPTZ DEFAULT now())"
        )
    else:
        cur.execute(
            "CREATE TABLE IF NOT EXISTS demo_notes (id INT AUTO_INCREMENT PRIMARY KEY, note TEXT NOT NULL,"
            " instance_id VARCHAR(64), created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
        )


def db_rows(cur):
    cur.execute("SELECT id, note, instance_id, created_at FROM demo_notes ORDER BY id DESC LIMIT 50")
    return [{"id": r[0], "note": r[1], "instance_id": r[2], "created_at": str(r[3])} for r in cur.fetchall()]


@app.route("/api/rds/status")
@requires("DB_HOST")
def rds_status():
    try:
        t0 = time.time()
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute("SELECT version()")
            version = cur.fetchone()[0]
            db_init(cur)
            rows = db_rows(cur)
        conn.commit()
        conn.close()
        return ok(engine=db_engine(), host=env("DB_HOST"), resolved_ip=socket.gethostbyname(env("DB_HOST")),
                  version=version, latency_ms=round((time.time() - t0) * 1000), rows=rows)
    except Exception as e:
        return fail(e)


@app.route("/api/rds/notes", methods=["POST"])
@requires("DB_HOST")
def rds_add():
    try:
        note = (request.json or {}).get("note", "").strip()[:500] or f"test kaydı {uuid.uuid4().hex[:6]}"
        conn = db_connect()
        with conn.cursor() as cur:
            db_init(cur)
            cur.execute("INSERT INTO demo_notes (note, instance_id) VALUES (%s, %s)", (note, instance_info()["instance_id"]))
            rows = db_rows(cur)
        conn.commit()
        conn.close()
        return ok(rows=rows)
    except Exception as e:
        return fail(e)


@app.route("/api/rds/notes/<int:note_id>", methods=["DELETE"])
@requires("DB_HOST")
def rds_delete(note_id):
    try:
        conn = db_connect()
        with conn.cursor() as cur:
            cur.execute("DELETE FROM demo_notes WHERE id = %s", (note_id,))
            rows = db_rows(cur)
        conn.commit()
        conn.close()
        return ok(rows=rows)
    except Exception as e:
        return fail(e)


# ---------------------------------------------------------------- S3
@app.route("/api/s3", methods=["GET"])
@requires("S3_BUCKET")
def s3_list():
    try:
        s3 = client("s3")
        bucket = env("S3_BUCKET")
        resp = s3.list_objects_v2(Bucket=bucket, Prefix=env("S3_PREFIX", "demo/"), MaxKeys=50)
        objs = [
            {"key": o["Key"], "size": o["Size"], "modified": o["LastModified"].isoformat(timespec="seconds"),
             "url": s3.generate_presigned_url("get_object", Params={"Bucket": bucket, "Key": o["Key"]}, ExpiresIn=300)}
            for o in resp.get("Contents", [])
        ]
        return ok(bucket=bucket, objects=objs)
    except Exception as e:
        return fail(e)


@app.route("/api/s3", methods=["POST"])
@requires("S3_BUCKET")
def s3_upload():
    try:
        f = request.files.get("file")
        prefix = env("S3_PREFIX", "demo/")
        if f and f.filename:
            key = f"{prefix}{int(time.time())}-{os.path.basename(f.filename)}"
            client("s3").upload_fileobj(f, env("S3_BUCKET"), key, ExtraArgs={"ContentType": f.mimetype or "application/octet-stream"})
        else:
            key = f"{prefix}{int(time.time())}-note.txt"
            body = f"{instance_info()['instance_id']} tarafından {now()} tarihinde yazıldı.\n"
            client("s3").put_object(Bucket=env("S3_BUCKET"), Key=key, Body=body.encode(), ContentType="text/plain")
        return s3_list()
    except Exception as e:
        return fail(e)


@app.route("/api/s3/delete", methods=["POST"])
@requires("S3_BUCKET")
def s3_delete():
    try:
        client("s3").delete_object(Bucket=env("S3_BUCKET"), Key=request.json["key"])
        return s3_list()
    except Exception as e:
        return fail(e)


# ---------------------------------------------------------------- DynamoDB
@app.route("/api/dynamodb", methods=["GET", "POST"])
@requires("DDB_TABLE")
def ddb():
    try:
        ddb = client("dynamodb")
        table = env("DDB_TABLE")
        pk = env("DDB_PK", "pk")
        if request.method == "POST":
            text = (request.json or {}).get("text", "").strip()[:500] or "merhaba DynamoDB"
            ddb.put_item(TableName=table, Item={
                pk: {"S": uuid.uuid4().hex[:12]}, "text": {"S": text},
                "instance_id": {"S": instance_info()["instance_id"]}, "created_at": {"S": now()},
            })
        items = ddb.scan(TableName=table, Limit=50).get("Items", [])
        items = [{k: list(v.values())[0] for k, v in it.items()} for it in items]
        items.sort(key=lambda i: i.get("created_at", ""), reverse=True)
        return ok(table=table, items=items)
    except Exception as e:
        return fail(e)


# ---------------------------------------------------------------- SQS
@app.route("/api/sqs/send", methods=["POST"])
@requires("SQS_QUEUE_URL")
def sqs_send():
    try:
        body = (request.json or {}).get("body", "").strip()[:1000] or f"mesaj @ {now()}"
        r = client("sqs").send_message(QueueUrl=env("SQS_QUEUE_URL"),
                                       MessageBody=json.dumps({"body": body, "from": instance_info()["instance_id"], "at": now()}))
        return ok(message_id=r["MessageId"])
    except Exception as e:
        return fail(e)


@app.route("/api/sqs/receive", methods=["POST"])
@requires("SQS_QUEUE_URL")
def sqs_receive():
    try:
        sqs = client("sqs")
        url = env("SQS_QUEUE_URL")
        msgs = sqs.receive_message(QueueUrl=url, MaxNumberOfMessages=10, WaitTimeSeconds=1).get("Messages", [])
        for m in msgs:
            sqs.delete_message(QueueUrl=url, ReceiptHandle=m["ReceiptHandle"])
        attrs = sqs.get_queue_attributes(QueueUrl=url, AttributeNames=["ApproximateNumberOfMessages"])["Attributes"]
        return ok(messages=[m["Body"] for m in msgs], remaining=attrs.get("ApproximateNumberOfMessages"))
    except Exception as e:
        return fail(e)


# ---------------------------------------------------------------- SNS
@app.route("/api/sns/publish", methods=["POST"])
@requires("SNS_TOPIC_ARN")
def sns_publish():
    try:
        msg = (request.json or {}).get("message", "").strip()[:1000] or "AWS Core Demo test bildirimi"
        r = client("sns").publish(TopicArn=env("SNS_TOPIC_ARN"), Subject="AWS Core Demo",
                                  Message=f"{msg}\n\nGönderen: {instance_info()['instance_id']} @ {now()}")
        return ok(message_id=r["MessageId"])
    except Exception as e:
        return fail(e)


# ---------------------------------------------------------------- ElastiCache (Redis / Valkey)
@app.route("/api/redis", methods=["GET", "POST"])
@requires("REDIS_HOST")
def redis_counter():
    try:
        import redis

        r = redis.Redis(host=env("REDIS_HOST"), port=int(env("REDIS_PORT", "6379")),
                        ssl=env("REDIS_TLS", "false").lower() == "true", socket_timeout=3, decode_responses=True)
        if request.method == "POST":
            r.incr("demo:counter")
            r.hincrby("demo:by_instance", instance_info()["instance_id"], 1)
        t0 = time.time()
        r.ping()
        return ok(counter=int(r.get("demo:counter") or 0), by_instance=r.hgetall("demo:by_instance"),
                  latency_ms=round((time.time() - t0) * 1000, 2))
    except Exception as e:
        return fail(e)


# ---------------------------------------------------------------- EFS
@app.route("/api/efs", methods=["GET", "POST"])
@requires("EFS_PATH")
def efs():
    try:
        path = os.path.join(env("EFS_PATH"), "core-demo.log")
        if request.method == "POST":
            with open(path, "a") as f:
                f.write(f"{now()}  {instance_info()['instance_id']}  ({instance_info()['az']})\n")
        lines = open(path).read().splitlines()[-30:] if os.path.exists(path) else []
        return ok(path=path, lines=list(reversed(lines)))
    except Exception as e:
        return fail(e)


# ---------------------------------------------------------------- Secrets Manager + SSM
@app.route("/api/secrets")
def secrets():
    try:
        out = {}
        if env("DEMO_SECRET_ID"):
            s = client("secretsmanager").get_secret_value(SecretId=env("DEMO_SECRET_ID"))["SecretString"]
            out["secret"] = {"id": env("DEMO_SECRET_ID"), "length": len(s), "masked": s[:2] + "•" * min(len(s) - 2, 12) if len(s) > 2 else "••"}
        if env("SSM_PARAM_NAME"):
            p = client("ssm").get_parameter(Name=env("SSM_PARAM_NAME"), WithDecryption=True)["Parameter"]
            out["parameter"] = {"name": p["Name"], "type": p["Type"], "value": p["Value"] if p["Type"] != "SecureString" else "•••• (SecureString)", "version": p["Version"]}
        return ok(**out)
    except Exception as e:
        return fail(e)


# ---------------------------------------------------------------- CloudWatch + Auto Scaling
@app.route("/api/cloudwatch/metric", methods=["POST"])
def cw_metric():
    try:
        value = float((request.json or {}).get("value", 1))
        client("cloudwatch").put_metric_data(Namespace="CoreDemo", MetricData=[{
            "MetricName": "DemoClicks", "Value": value, "Unit": "Count",
            "Dimensions": [{"Name": "InstanceId", "Value": instance_info()["instance_id"]}],
        }])
        return ok(namespace="CoreDemo", metric="DemoClicks", value=value)
    except Exception as e:
        return fail(e)


@app.route("/api/stress", methods=["POST"])
def stress():
    """ASG hedef-izleme (CPU) politikasını tetiklemek için tüm çekirdeklerde CPU yükü üretir."""
    seconds = max(10, min(int((request.json or {}).get("seconds", 120)), 900))
    cores = os.cpu_count() or 1
    for _ in range(cores):
        subprocess.Popen(["timeout", str(seconds), "sh", "-c", "while :; do :; done"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return ok(seconds=seconds, cores=cores, instance_id=instance_info()["instance_id"])


@app.route("/api/load")
def load():
    l1, l5, l15 = os.getloadavg()
    return ok(load=[round(l1, 2), round(l5, 2), round(l15, 2)], cores=os.cpu_count())


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(env("PORT", "8080")), debug=False)
