variable "REGISTRY" {
  default = "ghcr.io/mstroppel"
}

variable "IMAGE_VERSION" {
  default = "dev"
}

group "default" {
  targets = ["opencode", "ingest", "session-export"]
}

group "release" {
  targets = ["opencode-release", "ingest-release", "session-export-release"]
}

group "stable" {
  targets = ["opencode-stable", "ingest-stable", "session-export-stable"]
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
  tags = ["karpathy-wiki-ingest:test"]
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

target "ingest-release" {
  inherits = ["ingest"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest:${IMAGE_VERSION}"]
}

target "session-export-release" {
  inherits = ["session-export"]
  tags = ["${REGISTRY}/karpathy-wiki-session-export:${IMAGE_VERSION}"]
}

target "opencode-stable" {
  inherits = ["opencode"]
  tags = ["${REGISTRY}/karpathy-wiki-opencode:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-opencode:latest"]
}

target "ingest-stable" {
  inherits = ["ingest"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-ingest:latest"]
}

target "session-export-stable" {
  inherits = ["session-export"]
  tags = ["${REGISTRY}/karpathy-wiki-session-export:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-session-export:latest"]
}
