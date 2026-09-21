variable "REGISTRY" {
  default = "ghcr.io/mstroppel"
}

variable "IMAGE_VERSION" {
  default = "dev"
}

# Third-party tool version for the ingest image's rclone build stage, pinned
# by tag and digest so a registry retag cannot swap the copied binary. Find
# the digest with `docker buildx imagetools inspect rclone/rclone:1.75.1`;
# tests/test_dependencies.py validates the pin.
variable "RCLONE_VERSION" {
  default = "1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5"
}

group "default" {
  targets = ["opencode", "ingest", "ingest-webdav", "ingest-paperless"]
}

group "release" {
  targets = ["opencode-release", "ingest-release", "ingest-webdav-release", "ingest-paperless-release"]
}

group "stable" {
  targets = ["opencode-stable", "ingest-stable", "ingest-webdav-stable", "ingest-paperless-stable"]
}

target "common" {
  labels = {
    "org.opencontainers.image.source" = "https://github.com/mstroppel/karpathy-wiki"
    "org.opencontainers.image.licenses" = "MIT"
  }
}

target "opencode" {
  inherits = ["common"]
  context = "."
  dockerfile = "opencode/Dockerfile"
  tags = ["karpathy-wiki-opencode:test"]
}

target "ingest" {
  inherits = ["common"]
  context = "ingest"
  target = "core"
  args = {
    RCLONE_VERSION = RCLONE_VERSION
  }
  tags = ["karpathy-wiki-ingest:test"]
}

target "ingest-webdav" {
  inherits = ["common"]
  context = "ingest"
  target = "webdav"
  args = {
    RCLONE_VERSION = RCLONE_VERSION
  }
  tags = ["karpathy-wiki-ingest-webdav:test"]
}

target "ingest-paperless" {
  inherits = ["common"]
  context = "ingest"
  target = "paperless"
  args = {
    RCLONE_VERSION = RCLONE_VERSION
  }
  tags = ["karpathy-wiki-ingest-paperless:test"]
}

target "opencode-release" {
  inherits = ["opencode"]
  tags = ["${REGISTRY}/karpathy-wiki-opencode:${IMAGE_VERSION}"]
}

target "ingest-release" {
  inherits = ["ingest"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest:${IMAGE_VERSION}"]
}

target "ingest-webdav-release" {
  inherits = ["ingest-webdav"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-webdav:${IMAGE_VERSION}"]
}

target "ingest-paperless-release" {
  inherits = ["ingest-paperless"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-paperless:${IMAGE_VERSION}"]
}

target "opencode-stable" {
  inherits = ["opencode"]
  tags = ["${REGISTRY}/karpathy-wiki-opencode:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-opencode:latest"]
}

target "ingest-stable" {
  inherits = ["ingest"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-ingest:latest"]
}

target "ingest-webdav-stable" {
  inherits = ["ingest-webdav"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-webdav:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-ingest-webdav:latest"]
}

target "ingest-paperless-stable" {
  inherits = ["ingest-paperless"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-paperless:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-ingest-paperless:latest"]
}
