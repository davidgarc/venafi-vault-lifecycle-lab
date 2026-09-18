ARG ALPINE_IMAGE=alpine:3.22.1@sha256:4bcff63911fcb4448bd4fdacec207030997caf25e9bea4045fa6c8c44de311d1
FROM ${ALPINE_IMAGE}
RUN apk add --no-cache busybox-extras openssl jq coreutils
COPY www /www
RUN chmod 755 /www/cgi-bin/cert
USER 1000:1000
EXPOSE 8080
CMD ["busybox-extras", "httpd", "-f", "-p", "8080", "-h", "/www"]
