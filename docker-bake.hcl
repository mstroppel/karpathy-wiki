variable "REGISTRY" {
  default = "ghcr.io/mstroppel"
}

variable "IMAGE_VERSION" {
  default = "dev"
}

# Centralized third-party tool version for the ingest image. Keep the rclone
# version in .env.example in sync; tests/test_dependencies.py validates it.
variable "RCLONE_VERSION" {
  default = "1.75.1"
}

group "default" {
  targets = ["opencode", "ingest"]
}

group "release" {
  targets = ["opencode-release", "ingest-release"]
}

group "stable" {
  targets = ["opencode-stable", "ingest-stable"]
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
  args = {
    RCLONE_VERSION = RCLONE_VERSION
  }
  tags = ["karpathy-wiki-ingest:test"]
}

target "opencode-release" {
  inherits = ["opencode"]
  tags = ["${REGISTRY}/karpathy-wiki-opencode:${IMAGE_VERSION}"]
}

target "ingest-release" {
  inherits = ["ingest"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest:${IMAGE_VERSION}"]
}

target "opencode-stable" {
  inherits = ["opencode"]
  tags = ["${REGISTRY}/karpathy-wiki-opencode:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-opencode:latest"]
}

target "ingest-stable" {
  inherits = ["ingest"]
  tags = ["${REGISTRY}/karpathy-wiki-ingest:${IMAGE_VERSION}", "${REGISTRY}/karpathy-wiki-ingest:latest"]
}
