variable "REGISTRY" {
  default = "ghcr.io/mstroppel"
}

variable "IMAGE_VERSION" {
  default = "dev"
}

# Opt in only inside Actions, where runtime cache credentials are available.
variable "BUILD_CACHE" {
  default = ""
}

# Third-party tool version for the ingest image's rclone build stage, pinned
# by tag and digest so a registry retag cannot swap the copied binary. Find
# the digest with `docker buildx imagetools inspect rclone/rclone:1.75.1`;
# tests/test_dependencies.py validates the pin.
variable "RCLONE_VERSION" {
  default = "1.75.1@sha256:45401ad7410db1d67ffdb58e19059ad20b0d8e0285a60e38bbec55cc1019c7a5"
}

group "default" {
  targets = ["opencode", "openchamber", "ingest", "ingest-webdav", "ingest-audio", "ingest-speech", "ingest-paperless", "ingest-email"]
}

group "release" {
  targets = ["opencode-release", "openchamber-release", "ingest-release", "ingest-webdav-release", "ingest-audio-release", "ingest-speech-release", "ingest-paperless-release", "ingest-email-release"]
}

group "stable" {
  targets = ["opencode-stable", "openchamber-stable", "ingest-stable", "ingest-webdav-stable", "ingest-audio-stable", "ingest-speech-stable", "ingest-paperless-stable", "ingest-email-stable"]
}

target "common" {
  platforms = ["linux/amd64"]
  labels = {
    "org.opencontainers.image.source" = "https://github.com/mstroppel/karpathy-wiki"
    "org.opencontainers.image.licenses" = "MIT"
  }
}

target "opencode" {
  inherits = ["common"]
  cache-from = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-opencode"] : []
  cache-to = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-opencode,mode=max"] : []
  context = "."
  dockerfile = "opencode/Dockerfile"
  target = "opencode"
  tags = ["karpathy-wiki-opencode:test"]
}

target "openchamber" {
  inherits = ["common"]
  cache-from = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-openchamber"] : []
  cache-to = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-openchamber,mode=max"] : []
  context = "."
  dockerfile = "opencode/Dockerfile"
  target = "openchamber"
  tags = ["karpathy-wiki-openchamber:test"]
}

target "ingest" {
  inherits = ["common"]
  cache-from = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-ingest"] : []
  cache-to = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-ingest,mode=max"] : []
  context = "ingest"
  target = "core"
  args = {
    RCLONE_VERSION = RCLONE_VERSION
  }
  tags = ["karpathy-wiki-ingest:test"]
}

target "ingest-webdav" {
  inherits = ["common"]
  cache-from = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-ingest-webdav"] : []
  cache-to = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-ingest-webdav,mode=max"] : []
  context = "ingest"
  target = "webdav"
  args = {
    RCLONE_VERSION = RCLONE_VERSION
  }
  tags = ["karpathy-wiki-ingest-webdav:test"]
}

target "ingest-audio" {
  inherits = ["common"]
  cache-from = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-ingest-audio"] : []
  cache-to = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-ingest-audio,mode=max"] : []
  context = "ingest"
  target = "audio"
  args = {
    RCLONE_VERSION = RCLONE_VERSION
  }
  tags = ["karpathy-wiki-ingest-audio:test"]
}

target "ingest-speech" {
  inherits = ["common"]
  cache-from = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-ingest-speech"] : []
  cache-to = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-ingest-speech,mode=max"] : []
  context = "ingest"
  target = "speech"
  args = {
    RCLONE_VERSION = RCLONE_VERSION
  }
  tags = ["karpathy-wiki-ingest-speech:test"]
}

target "ingest-paperless" {
  inherits = ["common"]
  cache-from = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-ingest-paperless"] : []
  cache-to = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-ingest-paperless,mode=max"] : []
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

target "ingest-email" {
  inherits = ["common"]
  cache-from = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-ingest-email"] : []
  cache-to = BUILD_CACHE == "gha" ? ["type=gha,scope=kw-ingest-email,mode=max"] : []
  context = "ingest"
  target = "email"
  args = {
    RCLONE_VERSION = RCLONE_VERSION
  }
  tags = ["karpathy-wiki-ingest-email:test"]
}

target "ingest-email-release" {
  inherits = ["ingest-email"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-email:${IMAGE_VERSION}"]
}

target "ingest-email-stable" {
  inherits = ["ingest-email"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-email:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-ingest-email:latest"]
}

target "openchamber-release" {
  inherits = ["openchamber"]
  tags = ["${REGISTRY}/karpathy-wiki-openchamber:${IMAGE_VERSION}"]
}

target "ingest-release" {
  inherits = ["ingest"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest:${IMAGE_VERSION}"]
}

target "ingest-webdav-release" {
  inherits = ["ingest-webdav"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-webdav:${IMAGE_VERSION}"]
}

target "ingest-audio-release" {
  inherits = ["ingest-audio"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-audio:${IMAGE_VERSION}"]
}

target "ingest-speech-release" {
  inherits = ["ingest-speech"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-speech:${IMAGE_VERSION}"]
}

target "ingest-paperless-release" {
  inherits = ["ingest-paperless"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-paperless:${IMAGE_VERSION}"]
}

target "opencode-stable" {
  inherits = ["opencode"]
  tags = ["${REGISTRY}/karpathy-wiki-opencode:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-opencode:latest"]
}

target "openchamber-stable" {
  inherits = ["openchamber"]
  tags = ["${REGISTRY}/karpathy-wiki-openchamber:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-openchamber:latest"]
}

target "ingest-stable" {
  inherits = ["ingest"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-ingest:latest"]
}

target "ingest-webdav-stable" {
  inherits = ["ingest-webdav"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-webdav:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-ingest-webdav:latest"]
}

target "ingest-audio-stable" {
  inherits = ["ingest-audio"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-audio:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-ingest-audio:latest"]
}

target "ingest-speech-stable" {
  inherits = ["ingest-speech"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-speech:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-ingest-speech:latest"]
}

target "ingest-paperless-stable" {
  inherits = ["ingest-paperless"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest-paperless:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-ingest-paperless:latest"]
}
