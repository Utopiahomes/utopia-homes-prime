# Utopia Lucy: the Utopia Homes business synth. Unmodified Hermes (the same pinned image Personal
# Lucy runs), plus Utopia's profile. Build from the repository root:
#   docker build -f lucy/Dockerfile -t utopia-lucy .
FROM docker.io/nousresearch/hermes-agent:v2026.8.19@sha256:3811ed13da874fba2ac99b6d492db9a203d34cb6dccf90d886948c00d0ccec09

COPY lucy/profile /opt/utopia-lucy/profile
COPY --chmod=0755 lucy/start.sh /opt/utopia-lucy/start.sh

ENTRYPOINT ["/opt/utopia-lucy/start.sh"]
CMD ["gateway", "run"]
