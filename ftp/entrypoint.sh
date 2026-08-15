#!/bin/bash
set -euo pipefail

FTP_ROOT="${FTP_ROOT:-/data}"
FTP_PASV_MIN_PORT="${FTP_PASV_MIN_PORT:-21100}"
FTP_PASV_MAX_PORT="${FTP_PASV_MAX_PORT:-21110}"
CERT=/etc/vsftpd/ssl/vsftpd.pem

fail() {
    echo "ftp: $*" >&2
    exit 1
}

[[ -n "${FTP_USER:-}" ]] || fail "FTP_USER is empty"
[[ -n "${FTP_PASSWORD:-}" ]] || fail "FTP_PASSWORD is empty"
[[ -n "${FTP_PASV_ADDRESS:-}" ]] || fail "FTP_PASV_ADDRESS is empty"
[[ "$FTP_USER" != "root" ]] || fail "FTP_USER cannot be root"
[[ "$FTP_USER" =~ ^[a-z_][a-z0-9_-]{0,31}$ ]] || fail "FTP_USER is invalid"

if ! grep -qx '/usr/sbin/nologin' /etc/shells; then
    echo '/usr/sbin/nologin' >> /etc/shells
fi

echo "ftp: waiting for ${FTP_ROOT} contents"
until [[ -d "$FTP_ROOT/library" && -d "$FTP_ROOT/review" && -d "$FTP_ROOT/covers" ]]; do
    sleep 1
done

if id "$FTP_USER" >/dev/null 2>&1; then
    usermod -d "$FTP_ROOT" -s /usr/sbin/nologin "$FTP_USER"
else
    useradd -M -d "$FTP_ROOT" -s /usr/sbin/nologin "$FTP_USER"
fi
echo "${FTP_USER}:${FTP_PASSWORD}" | chpasswd
printf '%s\n' "$FTP_USER" > /etc/vsftpd.userlist

mkdir -p /etc/vsftpd/ssl /var/run/vsftpd/empty
chmod 755 /var/run/vsftpd/empty

if [[ ! -s "$CERT" ]]; then
    openssl req -x509 -nodes -newkey rsa:2048 -sha256 \
        -keyout /tmp/vsftpd.key -out /tmp/vsftpd.crt \
        -days 3650 \
        -subj "/CN=${FTP_PASV_ADDRESS}"
    cat /tmp/vsftpd.key /tmp/vsftpd.crt > "$CERT"
    rm -f /tmp/vsftpd.key /tmp/vsftpd.crt
    chmod 600 "$CERT"
fi

if [[ "$FTP_PASV_ADDRESS" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    PASV_RESOLVE=NO
else
    PASV_RESOLVE=YES
fi

cat > /etc/vsftpd.conf <<EOF
listen=YES
listen_ipv6=NO
background=NO
anonymous_enable=NO
local_enable=YES
write_enable=NO
chroot_local_user=YES
check_shell=NO
userlist_enable=YES
userlist_deny=NO
userlist_file=/etc/vsftpd.userlist
pam_service_name=vsftpd
secure_chroot_dir=/var/run/vsftpd/empty
seccomp_sandbox=NO
ssl_enable=YES
implicit_ssl=NO
allow_anon_ssl=NO
force_local_logins_ssl=YES
force_local_data_ssl=YES
require_ssl_reuse=NO
ssl_tlsv1=YES
ssl_sslv2=NO
ssl_sslv3=NO
ssl_ciphers=HIGH:!aNULL:!eNULL:!MD5:!3DES:!DES:!RC4:!EXPORT
rsa_cert_file=${CERT}
rsa_private_key_file=${CERT}
pasv_enable=YES
pasv_min_port=${FTP_PASV_MIN_PORT}
pasv_max_port=${FTP_PASV_MAX_PORT}
pasv_address=${FTP_PASV_ADDRESS}
pasv_addr_resolve=${PASV_RESOLVE}
cmds_denied=DELE,RMD,MKD,RNFR,RNTO,APPE,STOR,STOU
max_login_fails=3
delay_failed_login=5
idle_session_timeout=300
data_connection_timeout=120
max_clients=10
max_per_ip=4
xferlog_enable=YES
log_ftp_protocol=YES
vsftpd_log_file=/var/log/vsftpd.log
EOF

touch /var/log/vsftpd.log
chmod 644 /var/log/vsftpd.log
echo "ftp: explicit FTPS on :21 pasv ${FTP_PASV_ADDRESS}:${FTP_PASV_MIN_PORT}-${FTP_PASV_MAX_PORT} root ${FTP_ROOT}"
exec vsftpd /etc/vsftpd.conf
