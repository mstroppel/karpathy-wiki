variable "REGISTRY" {
  default = "ghcr.io/mstroppel"
}

variable "IMAGE_VERSION" {
  default = "dev"
}

group "default" {
  targets = ["opencode", "paperless-ingest", "session-export"]
}

group "release" {
  targets = ["opencode-release", "paperless-ingest-release", "session-export-release"]
}

group "stable" {
  targets = ["opencode-stable", "paperless-ingest-stable", "session-export-stable"]
}

target "common" {
  labels = {
    "org.opencontainers.image.source" = "https://github.com/mstroppel/karpathy-wiki"
    "org.opencontainers.image.licenses" = "MIT"
  }
}

target "opencode" {
  inherits = ["common"]
  context = "opencode"
  tags = ["karpathy-wiki-opencode:test"]
}

target "paperless-ingest" {
  inherits = ["common"]
  context = "paperless-ingest"
  tags = ["karpathy-wiki-paperless-ingest:test"]
}

target "session-export" {
  inherits = ["common"]
  context = "session-export"
  tags = ["karpathy-wiki-session-export:test"]
}

target "opencode-release" {
  inherits = ["opencode"]
  tags = ["${REGISTRY}/karpathy-wiki-opencode:${IMAGE_VERSION}"]
}

target "paperless-ingest-release" {
  inherits = ["paperless-ingest"]
  tags = ["${REGISTRY}/karpathy-wiki-paperless-ingest:${IMAGE_VERSION}"]
}

target "session-export-release" {
  inherits = ["session-export"]
  tags = ["${REGISTRY}/karpathy-wiki-session-export:${IMAGE_VERSION}"]
}

target "opencode-stable" {
  inherits = ["opencode"]
  tags = ["${REGISTRY}/karpathy-wiki-opencode:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-opencode:latest"]
}

target "paperless-ingest-stable" {
  inherits = ["paperless-ingest"]
  tags = ["${REGISTRY}/karpathy-wiki-paperless-ingest:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-paperless-ingest:latest"]
}

target "session-export-stable" {
  inherits = ["session-export"]
  tags = ["${REGISTRY}/karpathy-wiki-session-export:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-session-export:latest"]
}
