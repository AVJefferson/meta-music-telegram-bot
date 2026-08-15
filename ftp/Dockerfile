FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends vsftpd openssl \
    && rm -rf /var/lib/apt/lists/* \
    && mkdir -p /var/run/vsftpd/empty /etc/vsftpd/ssl \
    && chmod 755 /var/run/vsftpd/empty

# vsftpd 3.0.3 has no ssl_tlsv1_1 / ssl_tlsv1_2 options, so the TLS floor is set
# in the OpenSSL system policy instead. Without this, TLS 1.0 stays reachable.
RUN sed -i 's/^\[openssl_init\]$/[openssl_init]\nssl_conf = ssl_sect/' /etc/ssl/openssl.cnf \
    && printf '\n[ssl_sect]\nsystem_default = system_default_sect\n\n[system_default_sect]\nMinProtocol = TLSv1.2\nCipherString = DEFAULT:@SECLEVEL=2\n' >> /etc/ssl/openssl.cnf

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 21 21100-21110

ENTRYPOINT ["/entrypoint.sh"]
