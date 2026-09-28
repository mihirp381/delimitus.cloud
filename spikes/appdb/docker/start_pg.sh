#!/usr/bin/env bash
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
CERTS="$HERE/certs"
NAME=ssc-appdb-spike
PORT=55418
ADMIN_PW="${PGADMIN_PW:-adminpw}"

mkdir -p "$CERTS"
if [ ! -f "$CERTS/ca.crt" ]; then
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=ssc-spike-ca" \
    -keyout "$CERTS/ca.key" -out "$CERTS/ca.crt" 2>/dev/null
  openssl req -newkey rsa:2048 -nodes -subj "/CN=localhost" \
    -keyout "$CERTS/server.key" -out "$CERTS/server.csr" 2>/dev/null
  printf "subjectAltName=DNS:localhost,IP:127.0.0.1\n" > "$CERTS/san.ext"
  openssl x509 -req -in "$CERTS/server.csr" -CA "$CERTS/ca.crt" -CAkey "$CERTS/ca.key" \
    -CAcreateserial -days 30 -extfile "$CERTS/san.ext" -out "$CERTS/server.crt" 2>/dev/null
  openssl req -x509 -newkey rsa:2048 -nodes -days 30 -subj "/CN=wrong-ca" \
    -keyout "$CERTS/wrong-ca.key" -out "$CERTS/wrong-ca.crt" 2>/dev/null
  chmod 644 "$CERTS"/*.key
fi

cat > "$HERE/pg_hba.conf" <<'EOF'
local   all all                 trust
hostssl all all 0.0.0.0/0       scram-sha-256
hostssl all all ::0/0           scram-sha-256
EOF

docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" -p 127.0.0.1:$PORT:5432 \
  -e POSTGRES_PASSWORD="$ADMIN_PW" -e POSTGRES_USER=ssc_admin \
  -v "$CERTS/server.crt:/certs/server.crt:ro" \
  -v "$CERTS/server.key:/certs/server.key.src:ro" \
  -v "$HERE/pg_hba.conf:/pg_hba.conf:ro" \
  --entrypoint /bin/sh postgres:18 -c '
    cp /certs/server.key.src /tmp/server.key && chown postgres:postgres /tmp/server.key && chmod 600 /tmp/server.key
    exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/certs/server.crt -c ssl_key_file=/tmp/server.key -c hba_file=/pg_hba.conf
  ' >/dev/null

for i in $(seq 1 30); do
  if docker exec "$NAME" pg_isready -U ssc_admin >/dev/null 2>&1; then
    echo "postgres:18 ready on 127.0.0.1:$PORT (ssl on, hostssl only)"
    echo "ADMIN_URL=postgresql://ssc_admin:$ADMIN_PW@localhost:$PORT/postgres?sslmode=verify-full&sslrootcert=$CERTS/ca.crt"
    exit 0
  fi
  sleep 1
done
docker logs "$NAME" | tail -20
exit 1
